"""lw-run supervisor tests: the pure restart policy, CLI parsing, and one real harness run.

The integration test starts the supervisor as a process, watches the analyst entity appear
through the SDK on the harness the supervisor started, then sends SIGTERM and checks the
shutdown: exit code 0, no live child pids left.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from anduril import Lattice
from anduril.core import ApiError

from lattice_wildfire.analyst.agent import INVESTIGATE_URL, default_entity_id
from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.run import (
    APP_ORDER,
    GATE_TIMEOUT_S,
    Decision,
    RestartPolicy,
    Supervisor,
    decide_restart,
    feeds_ready,
)
from lattice_wildfire.run.__main__ import parse_args as run_args

REPO = Path(__file__).resolve().parents[1]
LATTICE_VARS = (
    "LATTICE_BASE_URL",
    "LATTICE_ENDPOINT",
    "ENVIRONMENT_TOKEN",
    "SANDBOXES_TOKEN",
    "LATTICE_CLIENT_ID",
    "LATTICE_CLIENT_SECRET",
)


# --------------------------------------------------------------------------- restart policy


def test_first_restart_uses_base_delay_then_doubles_up_to_the_cap():
    policy = RestartPolicy(base_delay_s=1.0, max_delay_s=8.0, max_restarts=10)
    assert decide_restart(policy, [], 100.0) == Decision("restart", 1.0, "restart 1/10 in 1 s")
    assert decide_restart(policy, [50.0], 100.0).delay_s == 2.0
    assert decide_restart(policy, [40.0, 50.0], 100.0).delay_s == 4.0
    assert decide_restart(policy, [30.0, 40.0, 50.0], 100.0).delay_s == 8.0
    assert decide_restart(policy, [20.0, 30.0, 40.0, 50.0], 100.0).delay_s == 8.0  # capped


def test_five_restarts_inside_the_window_then_stays_down():
    policy = RestartPolicy(max_restarts=5, window_s=600.0)
    history = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert decide_restart(policy, history[:-1], 100.0).restart is True
    decision = decide_restart(policy, history, 100.0)
    assert decision.action == "stay-down"
    assert decision.restart is False
    assert decision.delay_s == 0.0
    assert "5 restarts within 10 min" in decision.reason


def test_restarts_older_than_the_window_are_forgotten():
    policy = RestartPolicy(max_restarts=1, window_s=600.0)
    assert decide_restart(policy, [0.0], 601.0).restart is True
    assert decide_restart(policy, [0.0], 599.0).restart is False


# --------------------------------------------------------------------------- cli


def test_cli_parses_apps_and_rejects_unknown():
    args = run_args(["--apps", "feeds,analyst", "--with-harness", "18990", "--state-dir", "var/test"])
    assert args.apps == ("feeds", "analyst")
    assert args.with_harness == 18990
    assert str(args.state_dir) == "var/test"
    assert args.status_interval == 60.0
    assert run_args([]).apps == APP_ORDER
    with pytest.raises(SystemExit):
        run_args(["--apps", "feeds,map"])


# --------------------------------------------------------------------------- supervisor wiring


def _supervisor(tmp_path: Path, **kw) -> Supervisor:
    sup = Supervisor(("feeds", "analyst"), state_dir=tmp_path, out=io.StringIO(), **kw)
    sup._started = time.monotonic()
    sup._make_children()
    return sup


def test_status_line_is_one_line_with_every_app(tmp_path):
    sup = _supervisor(tmp_path)
    line = sup.status_line()
    assert "\n" not in line
    assert line.startswith("lw-run t+")
    assert "feeds=" in line and "analyst=" in line
    assert "apps=0/2 up" in line


def test_crash_goes_to_backoff_then_stays_down_after_the_budget(tmp_path):
    sup = _supervisor(tmp_path, policy=RestartPolicy(max_restarts=2, window_s=600.0, base_delay_s=1.0))
    child = sup.children[0]
    now = 1000.0
    child.state = "running"
    child.started_at = now - 5.0  # crashed after 5 s: shorter than healthy_s
    sup._on_exit(child, 1, now)
    assert child.state == "backoff"
    assert child.next_start == pytest.approx(now + 1.0)
    child.restarts = [now]
    child.state = "running"
    child.started_at = now - 5.0
    sup._on_exit(child, 1, now + 10)
    assert child.state == "backoff"
    child.restarts = [now, now + 10]
    child.state = "running"
    child.started_at = now - 5.0
    sup._on_exit(child, 2, now + 20)
    assert child.state == "down"
    assert child.exit_code == 2


def test_long_healthy_run_resets_the_restart_budget(tmp_path):
    sup = _supervisor(tmp_path, policy=RestartPolicy(max_restarts=1, window_s=600.0, healthy_s=60.0))
    child = sup.children[0]
    child.restarts = [900.0]
    child.state = "running"
    child.started_at = 1000.0 - 120.0
    sup._on_exit(child, 1, 1000.0)
    assert child.restarts == []  # the 120 s healthy run cleared the old budget
    assert child.state == "backoff"
    assert child.next_start == pytest.approx(1001.0)  # back to the base delay


def test_shutdown_remembers_an_app_that_stayed_down(tmp_path):
    sup = _supervisor(tmp_path)
    sup.children[0].state = "down"
    sup.children[0].exit_code = 3
    sup._shutdown()
    assert sup.had_down is True
    assert all(c.state == "stopped" for c in sup.children)


# --------------------------------------------------------------------------- feeds readiness gate


def _gated_supervisor(monkeypatch, tmp_path: Path, apps=APP_ORDER) -> Supervisor:
    monkeypatch.delenv("LW_STATE_DIR", raising=False)
    sup = Supervisor(apps, state_dir=tmp_path, out=io.StringIO(), tick_s=0.01)
    sup._started = time.monotonic()
    sup._make_children()
    return sup


def test_fusion_and_planner_wait_for_the_feeds_marker(monkeypatch, tmp_path):
    sup = _gated_supervisor(monkeypatch, tmp_path)
    line = sup.status_line()
    assert "fusion=waiting(feeds)" in line
    assert "planner=waiting(feeds)" in line
    assert "analyst=pending" in line, "the analyst starts immediately"
    assert "feeds=pending" in line

    marker = tmp_path / "feeds" / "ready.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}\n", encoding="utf-8")
    assert feeds_ready(tmp_path)

    sup._release_gates(sup._started + 1.0)
    assert all(c.waiting_for is None for c in sup.children)
    assert "feeds ready; starting fusion" in sup.out.getvalue()
    assert "waiting(" not in sup.status_line()


def test_tick_does_not_start_a_gated_app_before_the_marker(monkeypatch, tmp_path):
    sup = _gated_supervisor(monkeypatch, tmp_path)
    started: list[str] = []

    def fake_start(child, now):
        started.append(child.name)
        child.state = "running"
        child.started_at = now

    monkeypatch.setattr(sup, "_start_child", fake_start)
    now = sup._started + 1.0
    for child in sup.children:
        child.next_start = now
    sup._tick(now)
    assert started == ["feeds", "analyst"]

    (tmp_path / "feeds").mkdir()
    (tmp_path / "feeds" / "ready.json").write_text("{}\n", encoding="utf-8")
    started.clear()
    sup._tick(now + 1.0)
    assert started == ["fusion", "planner"]


def test_gated_apps_start_with_a_warning_after_180s(monkeypatch, tmp_path):
    sup = _gated_supervisor(monkeypatch, tmp_path)
    sup._release_gates(sup._started + GATE_TIMEOUT_S - 1.0)
    assert sup.children[1].waiting_for == "feeds", "still waiting just before the deadline"

    sup._release_gates(sup._started + GATE_TIMEOUT_S)
    assert all(c.waiting_for is None for c in sup.children)
    logged = sup.out.getvalue()
    assert "warning" in logged and "180" in logged


def test_no_gate_when_feeds_is_not_supervised(monkeypatch, tmp_path):
    sup = _gated_supervisor(monkeypatch, tmp_path, apps=("analyst", "fusion"))
    assert all(c.waiting_for is None for c in sup.children)
    assert "fusion=pending" in sup.status_line()


def test_a_stale_ready_marker_is_removed_at_start(monkeypatch, tmp_path):
    marker = tmp_path / "feeds" / "ready.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"at": "old"}\n', encoding="utf-8")
    sup = _gated_supervisor(monkeypatch, tmp_path)
    sup._clear_ready_marker()
    assert not marker.exists()
    assert "removed stale" in sup.out.getvalue()


def test_harness_stays_reachable_while_apps_stop(monkeypatch, tmp_path):
    """The harness is in its own session and gets SIGTERM only after every app has exited, so an
    app can finish its last status update."""
    monkeypatch.delenv("LW_STATE_DIR", raising=False)
    port = _free_port()
    sup = Supervisor(("feeds",), state_dir=tmp_path, harness_port=port, out=io.StringIO(), grace_s=10.0)
    sup._started = time.monotonic()
    sup._make_children()
    assert sup._start_harness()
    child = sup.children[0]
    child.argv = [
        sys.executable,
        "-c",
        "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: (time.sleep(3), sys.exit(0))); time.sleep(60)",
    ]
    sup._start_child(child, time.monotonic())

    stopper = threading.Thread(target=sup._shutdown)
    stopper.start()
    reachable_while_stopping = False
    deadline = time.monotonic() + 30.0
    while stopper.is_alive() and time.monotonic() < deadline:
        if child.process is not None and child.process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    reachable_while_stopping = True
            except OSError:
                pass
        time.sleep(0.05)
    stopper.join(timeout=90)
    assert not stopper.is_alive(), "supervisor shutdown did not finish in 90 s"
    assert reachable_while_stopping, "harness went down while the app was still stopping"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


# --------------------------------------------------------------------------- harness integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in LATTICE_VARS}


def _client(port: int) -> Lattice:
    settings = LatticeSettings(
        base_url=f"http://127.0.0.1:{port}",
        environment_token="harness-token",
        sandboxes_token=None,
        client_id=None,
        client_secret=None,
    )
    return make_client(settings, timeout=10)


def _wait_entity(client: Lattice, entity_id: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            entity = client.entities.get_entity(entity_id)
        except ApiError:
            entity = None
        if entity is not None and entity.is_live:
            return entity
        time.sleep(0.5)
    return None


def test_harness_plus_analyst_run_stops_cleanly(tmp_path):
    port = _free_port()
    state = tmp_path / "run"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lattice_wildfire.run",
            "--with-harness",
            str(port),
            "--apps",
            "analyst",
            "--state-dir",
            str(state),
            "--status-interval",
            "5",
        ],
        cwd=REPO,
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    entity = _wait_entity(_client(port), default_entity_id(), 90.0)
    try:
        assert entity is not None, "analyst entity never became live:\n" + "\n".join(lines[-20:])
        assert entity.ontology.template == "TEMPLATE_ASSET"
        catalog = entity.task_catalog.task_definitions if entity.task_catalog else []
        assert INVESTIGATE_URL in {d.task_specification_url for d in catalog}
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=60)
        reader.join(timeout=10)

    assert code == 0, "\n".join(lines[-20:])
    assert any(line.startswith("lw-run t+") for line in lines)
    assert any(line.startswith("[analyst] ") for line in lines)
    assert any("[lw-run] stopped" in line for line in lines)

    log = state / "logs" / "analyst.log"
    assert log.exists()
    assert "live; anchor" in log.read_text(encoding="utf-8")  # analyst's own startup line, unprefixed
    assert (state / "logs" / "harness.log").exists()

    pids = json.loads((state / "pids.json").read_text(encoding="utf-8"))
    assert set(pids) == {"analyst", "harness"}
    for name, pid in pids.items():
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not Path(f"/proc/{pid}").exists(), f"{name} pid {pid} still running"
