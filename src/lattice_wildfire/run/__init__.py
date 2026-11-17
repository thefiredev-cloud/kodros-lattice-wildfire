"""`lw-run`: run the wildfire apps as supervised child processes.

One supervisor process starts each app (`feeds`, `fusion`, `analyst`, `planner`) with the
shared environment, tees every output line, prefixed with the app name, into
``{state}/logs/<app>.log``, restarts a crashed app with exponential backoff up to
``RestartPolicy.max_restarts`` in ``RestartPolicy.window_s`` and then leaves it down, and
forwards SIGINT/SIGTERM to the whole set for an orderly shutdown.

`--with-harness PORT` starts the local Lattice contract harness first and points every
child at it, so a whole fleet can be exercised without Lattice credentials.

The restart decision is a pure function (`decide_restart`); `Supervisor` only acts on it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

# Module argv for one app; every app is started as ``python -u -m <module> ...``.
APP_MODULES: dict[str, tuple[str, ...]] = {
    "feeds": ("lattice_wildfire.feeds",),
    "fusion": ("lattice_wildfire.fusion",),
    "analyst": ("lattice_wildfire.analyst", "run"),
    "planner": ("lattice_wildfire.planner",),
}
# Data-flow order: feeds publish, fusion perceives, the analyst answers, the planner tasks.
APP_ORDER: tuple[str, ...] = ("feeds", "fusion", "analyst", "planner")
HARNESS_MODULE = "lattice_wildfire.harness"
HARNESS_TOKEN = "harness-token"
# Feeds writes this marker (under `$LW_STATE_DIR`) after its first clean cycle. Fusion and the
# planner wait for it so their first cycle already sees incidents, perimeters and stations.
READY_MARKER = Path("feeds") / "ready.json"
# After this long without the marker, start the waiting apps anyway and say so.
GATE_TIMEOUT_S = 180.0
# Apps that consume feeds output and therefore wait for the marker. The analyst reads tasks
# rather than entities, so it starts immediately.
GATED_APPS: dict[str, str] = {"fusion": "feeds", "planner": "feeds"}


def feeds_ready(state_dir: Path) -> bool:
    """True when feeds has written its readiness marker under `state_dir`."""
    return (Path(state_dir) / READY_MARKER).exists()


@dataclass(frozen=True)
class RestartPolicy:
    """When to start a crashed app again, and when to leave it down.

    At most `max_restarts` restarts inside any `window_s`; the delay doubles from
    `base_delay_s` up to `max_delay_s`; a run that lasted `healthy_s` clears the budget.
    """

    max_restarts: int = 5
    window_s: float = 600.0
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    healthy_s: float = 60.0


@dataclass(frozen=True)
class Decision:
    """What to do with a crashed child. `action` is ``"restart"`` or ``"stay-down"``."""

    action: str
    delay_s: float = 0.0
    reason: str = ""

    @property
    def restart(self) -> bool:
        return self.action == "restart"


def decide_restart(policy: RestartPolicy, restarts: Sequence[float], now: float) -> Decision:
    """Pure restart decision at monotonic time `now` given past restart times."""
    recent = [t for t in restarts if now - t < policy.window_s]
    if len(recent) >= policy.max_restarts:
        return Decision(
            "stay-down",
            reason=(
                f"{len(recent)} restarts within {policy.window_s / 60:.0f} min "
                f"(max {policy.max_restarts}); staying down"
            ),
        )
    delay = min(policy.base_delay_s * (2.0 ** len(recent)), policy.max_delay_s)
    return Decision("restart", delay, f"restart {len(recent) + 1}/{policy.max_restarts} in {delay:.0f} s")


@dataclass
class Child:
    """One supervised process."""

    name: str
    argv: list[str]
    log_path: Path
    restart: bool = True
    new_session: bool = False  # own process group: a group signal cannot reach it
    # Name of the app whose readiness marker this child waits for before its first start.
    waiting_for: str | None = None
    gate_deadline: float = 0.0
    process: subprocess.Popen | None = None
    state: str = "pending"  # pending | running | backoff | down | stopped
    started_at: float = 0.0
    runs: int = 0
    restarts: list[float] = field(default_factory=list)
    next_start: float = 0.0
    exit_code: int | None = None
    log: IO[str] | None = None
    reader: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None


class Supervisor:
    """Start, tee, restart and stop the app children plus an optional contract harness."""

    def __init__(
        self,
        apps: Sequence[str] = APP_ORDER,
        *,
        state_dir: Path = Path("var/run"),
        harness_port: int | None = None,
        policy: RestartPolicy | None = None,
        status_interval_s: float = 60.0,
        tick_s: float = 0.2,
        grace_s: float = 10.0,
        out: IO[str] | None = None,
    ) -> None:
        unknown = [a for a in apps if a not in APP_MODULES]
        if unknown:
            raise ValueError(f"unknown app(s) {unknown}; choose from {', '.join(APP_ORDER)}")
        self.apps = tuple(dict.fromkeys(apps))
        self.state_dir = Path(state_dir)
        self.log_dir = self.state_dir / "logs"
        self.harness_port = harness_port
        self.policy = policy or RestartPolicy()
        self.status_interval_s = status_interval_s
        self.tick_s = tick_s
        self.grace_s = grace_s
        self.out = out if out is not None else sys.stdout
        self.children: list[Child] = []
        self.harness: Child | None = None
        self.env = self._child_env()
        # Children see LW_STATE_DIR (theirs or ours); gates read the marker at that same root.
        self.state_root = Path(self.env.get("LW_STATE_DIR") or self.state_dir).expanduser()
        self.ready_marker = self.state_root / READY_MARKER
        self._stop = threading.Event()
        self._log_lock = threading.Lock()
        self._started = 0.0
        self._shut_down = False
        self.had_down = False  # an app used up its restart budget
        self._last_pids: dict[str, int] = {}

    # ------------------------------------------------------------------ setup

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("LW_STATE_DIR", str(self.state_dir.resolve()))
        env["PYTHONUNBUFFERED"] = "1"
        if self.harness_port is not None:
            env["LATTICE_BASE_URL"] = f"http://127.0.0.1:{self.harness_port}"
            env.setdefault("ENVIRONMENT_TOKEN", HARNESS_TOKEN)
        return env

    @staticmethod
    def _argv(module_argv: Sequence[str]) -> list[str]:
        return [sys.executable, "-u", "-m", *module_argv]

    def _make_children(self) -> None:
        self.children = [
            Child(
                name=name,
                argv=self._argv(APP_MODULES[name]),
                log_path=self.log_dir / f"{name}.log",
            )
            for name in self.apps
        ]
        for child in self.children:
            dependency = GATED_APPS.get(child.name)
            if dependency is not None and dependency in self.apps:
                child.waiting_for = dependency
                child.gate_deadline = self._started + GATE_TIMEOUT_S

    # ------------------------------------------------------------------ output

    def _log(self, message: str) -> None:
        with self._log_lock:
            print(message, file=self.out, flush=True)

    def _pump(self, child: Child) -> None:
        """Tee one child's merged stdout/stderr into its log file and the supervisor's stdout."""
        process = child.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            text = line.rstrip("\n")
            try:
                if child.log is not None:
                    child.log.write(text + "\n")
                    child.log.flush()
            except (OSError, ValueError):  # log closed at shutdown: keep draining the pipe
                pass
            with self._log_lock:
                print(f"[{child.name}] {text}", file=self.out, flush=True)

    def _write_pids(self) -> None:
        pids = dict(self._last_pids)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "pids.json").write_text(
                json.dumps(pids, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:  # the fleet keeps running even if bookkeeping fails
            self._log(f"[lw-run] could not write pids.json: {exc}")

    def status_line(self) -> str:
        now = time.monotonic()
        parts = []
        for c in self.children:
            if c.state == "running":
                parts.append(f"{c.name}=up({now - c.started_at:.0f}s)")
            elif c.state == "pending" and c.waiting_for is not None:
                parts.append(f"{c.name}=waiting({c.waiting_for})")
            elif c.state == "backoff":
                parts.append(f"{c.name}=retry-in({max(0.0, c.next_start - now):.0f}s)")
            elif c.state == "down":
                parts.append(f"{c.name}=down(exit {c.exit_code})")
            else:
                parts.append(f"{c.name}={c.state}")
        up = sum(1 for c in self.children if c.state == "running")
        down = sum(1 for c in self.children if c.state == "down")
        restarts = sum(len(c.restarts) for c in self.children)
        head = f"lw-run t+{now - self._started:.0f}s"
        if self.harness is not None:
            state = "up" if self.harness.state == "running" else self.harness.state
            head += f" harness={state}"
        return f"{head} apps={up}/{len(self.children)} up, {down} down, {restarts} restarts | " + " ".join(
            parts
        )

    # ------------------------------------------------------------------ processes

    def _start_child(self, child: Child, now: float) -> None:
        child.log_path.parent.mkdir(parents=True, exist_ok=True)
        if child.runs > 0:
            child.restarts = [t for t in child.restarts if now - t < self.policy.window_s] + [now]
        child.log = child.log_path.open("a", encoding="utf-8")
        child.process = subprocess.Popen(
            child.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.env,
            start_new_session=child.new_session,
        )
        child.started_at = now
        child.runs += 1
        child.exit_code = None
        child.state = "running"
        child.reader = threading.Thread(target=self._pump, args=(child,), daemon=True)
        child.reader.start()
        self._last_pids[child.name] = child.process.pid
        self._write_pids()
        self._log(f"[lw-run] started {child.name} pid {child.process.pid}")

    def _on_exit(self, child: Child, code: int, now: float) -> None:
        ran = now - child.started_at
        child.process = None
        child.exit_code = code
        if child.reader is not None:
            child.reader.join(timeout=2.0)
            child.reader = None
        if child.log is not None:
            child.log.close()
            child.log = None
        if self._stop.is_set() or not child.restart:
            child.state = "stopped"
            return
        if ran >= self.policy.healthy_s:
            child.restarts.clear()  # a long healthy run earns a fresh budget
        decision = decide_restart(self.policy, child.restarts, now)
        self._log(f"[lw-run] {child.name} exited {code} after {ran:.0f} s; {decision.reason}")
        if decision.restart:
            child.state = "backoff"
            child.next_start = now + decision.delay_s
        else:
            child.state = "down"

    def _release_gates(self, now: float) -> None:
        """Start gated apps once feeds is ready; after `GATE_TIMEOUT_S`, start them anyway."""
        for child in self.children:
            if child.waiting_for is None:
                continue
            if feeds_ready(self.state_root):
                child.waiting_for = None
                self._log(f"[lw-run] feeds ready; starting {child.name}")
            elif now >= child.gate_deadline:
                child.waiting_for = None
                self._log(
                    f"[lw-run] warning: feeds did not write {self.ready_marker} within "
                    f"{GATE_TIMEOUT_S:.0f} s; starting {child.name} anyway"
                )

    def _clear_ready_marker(self) -> None:
        """Drop a marker left by an earlier run so this run's gated apps wait for this feeds."""
        if not any(child.waiting_for is not None for child in self.children):
            return
        try:
            self.ready_marker.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._log(f"[lw-run] could not remove stale {self.ready_marker}: {exc}")
            return
        self._log(f"[lw-run] removed stale {self.ready_marker}")

    def _tick(self, now: float) -> None:
        self._release_gates(now)
        for child in [*self.children, *([self.harness] if self.harness else [])]:
            if child.process is not None:
                code = child.process.poll()
                if code is not None:
                    self._on_exit(child, code, now)
            elif (
                child.state in ("pending", "backoff")
                and child.waiting_for is None
                and now >= child.next_start
            ):
                self._start_child(child, now)

    def _wait_port(self, timeout_s: float = 20.0) -> bool:
        assert self.harness_port is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.harness is not None and self.harness.process is not None:
                if self.harness.process.poll() is not None:
                    return False
            try:
                with socket.create_connection(("127.0.0.1", self.harness_port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def _start_harness(self) -> bool:
        self.harness = Child(
            name="harness",
            argv=self._argv((HARNESS_MODULE, "--port", str(self.harness_port))),
            log_path=self.log_dir / "harness.log",
            restart=False,
            new_session=True,
        )
        self._start_child(self.harness, time.monotonic())
        if self._wait_port():
            self._log(f"[lw-run] harness listening on 127.0.0.1:{self.harness_port}")
            return True
        self._log(f"[lw-run] harness did not listen on 127.0.0.1:{self.harness_port}")
        return False

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self, *_: Any) -> None:
        self._stop.set()

    def run(self) -> int:
        """Supervise until SIGINT/SIGTERM. Returns 0 on a clean stop, 1 if an app stayed down."""
        self._started = time.monotonic()
        self._make_children()
        self._clear_ready_marker()
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, self.request_stop)
            except ValueError:  # not the main thread (tests drive the supervisor directly)
                pass
        try:
            if self.harness_port is not None and not self._start_harness():
                return 0 if self._stop.is_set() else 2  # interrupted startup is not a failure
            now = time.monotonic()
            for child in self.children:
                child.next_start = now
            self._log(self.status_line())
            next_status = now + self.status_interval_s
            while not self._stop.is_set():
                now = time.monotonic()
                self._tick(now)
                if now >= next_status:
                    self._log(self.status_line())
                    next_status = now + self.status_interval_s
                self._stop.wait(self.tick_s)
        finally:
            self._shutdown()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 1 if self.had_down else 0

    def _shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self._stop.set()
        self.had_down = any(c.state == "down" for c in self.children)
        if any(c.state == "running" for c in self.children):
            self._log(self.status_line())  # final live state before the children go away
        # Apps first (planner last to stop tasking), then the harness: a stopping app can still
        # finish its final status update, and the harness outlives every child that talks to it.
        apps = [c for c in reversed(self.children) if c.process is not None and c.process.poll() is None]
        for child in apps:
            child.process.terminate()
        self._wait_stopped(apps)
        if self.harness is not None and self.harness.process is not None:
            if self.harness.process.poll() is None:
                self.harness.process.terminate()
            self._wait_stopped([self.harness])
        for child in [*self.children, *([self.harness] if self.harness else [])]:
            if child.process is not None:
                child.exit_code = child.process.poll()
                child.process = None
            child.state = "stopped"
            if child.reader is not None:
                child.reader.join(timeout=2.0)
            if child.log is not None:
                child.log.close()
                child.log = None
        self._write_pids()
        self._log(f"[lw-run] stopped {len(self.apps)} app(s)")

    def _wait_stopped(self, children: Sequence[Child]) -> None:
        """Wait up to `grace_s` for every child in one group, killing the stragglers."""
        deadline = time.monotonic() + self.grace_s
        for child in children:
            assert child.process is not None
            try:
                child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._log(f"[lw-run] {child.name} did not stop in {self.grace_s:.0f} s; killing it")
                child.process.kill()
                child.process.wait(timeout=5.0)
