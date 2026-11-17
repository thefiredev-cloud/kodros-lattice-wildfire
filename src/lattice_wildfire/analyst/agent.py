"""Kodros Analyst: a taskable Lattice agent that answers Investigate tasks with intel briefs.

Lifecycle invariants (lattice-sdk-ai ``task-lifecycle.md``):

* The agent entity is published before the task stream opens and re-published every
  ``heartbeat_s`` seconds with a short expiry, a task catalog advertising Investigate and
  health that reflects the task stream and the local model.
* Tasks arrive on ``tasks.stream_as_agent`` (reconnect loop with capped backoff). If the
  server does not offer the stream (404/405/501) the agent falls back to the
  ``tasks.listen_as_agent`` long poll.
* A fixed pool of ``capacity`` worker threads (default 3, ``LW_ANALYST_CAPACITY`` / ``--capacity``).
  ``capacity=N`` is published in the entity description for the planner. Exactly ``capacity`` tasks
  run at once; further execute requests wait in a bounded queue and stay ``SENT`` until a slot
  frees. A request that would overrun the queue ends ``DONE_NOT_OK`` (``ERROR_CODE_REJECTED``).
* ``EXECUTING`` is reported when a worker picks the task up, not when it is queued.
* ``status_version`` strictly increases: it starts from the version delivered with the
  task, is raised to every version the server reports back, and is re-read on cancel.
* Terminal states (``DONE_OK``, ``DONE_NOT_OK``) are final; nothing is sent afterwards.
* Cancel sets a per-task event that the worker checks between every fetch, before and
  after the model call and around the object upload. A cancelled task ends
  ``DONE_NOT_OK`` (``ERROR_CODE_CANCELLED``) and leaves no brief objects behind.
* Any error ends the task ``DONE_NOT_OK``; a task never stays ``EXECUTING``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from anduril import (
    Aliases,
    ComponentHealth,
    ComponentMessage,
    EntityIdsSelector,
    Health,
    MilView,
    Ontology,
    Principal,
    Sensors,
    System,
    TaskCatalog,
    TaskDefinition,
    TaskError,
    TaskStatus,
)
from anduril.core.api_error import ApiError

from lattice_wildfire.core.entities import now_utc, point_location, provenance, symbology, unclassified
from lattice_wildfire.core.llm import ChatResult

from .brief import object_paths
from .facts import target_from_entity
from .gather import Brief, LiveSources, Sources, make_brief

log = logging.getLogger("lattice_wildfire.analyst")

PKG = "analyst"
DATA_TYPE = "kodros-analyst"
PLATFORM_TYPE = "AI Analyst"
INVESTIGATE_URL = "type.googleapis.com/anduril.tasks.v2.Investigate"

STATUS_EXECUTING = "STATUS_EXECUTING"
STATUS_DONE_OK = "STATUS_DONE_OK"
STATUS_DONE_NOT_OK = "STATUS_DONE_NOT_OK"
TERMINAL = frozenset({STATUS_DONE_OK, STATUS_DONE_NOT_OK})

HEALTHY = "HEALTH_STATUS_HEALTHY"
WARN = "HEALTH_STATUS_WARN"
NOT_READY = "HEALTH_STATUS_NOT_READY"
OFFLINE = "HEALTH_STATUS_OFFLINE"

# Status codes that mean "this server has no agent stream"; switch to the long poll.
_STREAM_UNSUPPORTED = frozenset({404, 405, 501})
_FINISHED_MEMORY = 1024

# Shutdown is bounded: the supervisor SIGTERMs the process and kills whatever outlives its grace
# period (10 s in production), so every Lattice call on the stop path gets a short timeout and no
# retries. SDK 5.4.0 RequestOptions has ``timeout`` (float, preferred) as well as the deprecated
# ``timeout_in_seconds`` (int); ``anduril.core.http_client`` reads ``timeout`` first.
DEFAULT_STOP_TIMEOUT_S = 8.0
SHUTDOWN_REQUEST_TIMEOUT_S = 2.0
STOP_REASON = "agent stopping"

# Concurrent Investigate tasks the agent runs before it queues the rest. Published in the entity
# description as `capacity=N`; the planner reads it to avoid over-tasking this analyst.
DEFAULT_CAPACITY = 3
CAPACITY_ENV = "LW_ANALYST_CAPACITY"


def capacity_from_env(default: int = DEFAULT_CAPACITY) -> int:
    """``${LW_ANALYST_CAPACITY:-3}`` as a positive integer; a bad value raises ``ValueError``."""
    raw = (os.environ.get(CAPACITY_ENV) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{CAPACITY_ENV} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{CAPACITY_ENV} must be at least 1, got {value}")
    return value


def default_queue_max(capacity: int) -> int:
    """Bound on waiting tasks: four slots per running slot, never fewer than eight."""
    return max(4 * capacity, 8)


def _shutdown_request_options(deadline: float) -> dict[str, Any] | None:
    """RequestOptions for one stop-path call, or ``None`` once ``deadline`` (monotonic) has passed.

    ``max_retries: 0`` is the point: an unreachable Lattice made the default retry backoff outlive
    the supervisor's grace period, so one attempt that the deadline can still cover is all we send.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return {"timeout": min(SHUTDOWN_REQUEST_TIMEOUT_S, remaining), "max_retries": 0}


def default_entity_id(host: str | None = None) -> str:
    """CONTRACT id ``lw-analyst-{host}``, host lowercased to [a-z0-9-]."""
    raw = (host or socket.gethostname()).lower()
    clean = "".join(c if c.isalnum() else "-" for c in raw).strip("-")
    return f"lw-analyst-{clean or 'host'}"


def objective_entity_id(spec: Any) -> str | None:
    """``objective.entity_id`` from an Investigate specification (proto JSON or snake case)."""
    if spec is None:
        return None
    extra = dict(getattr(spec, "model_extra", None) or {})
    objective = extra.get("objective")
    if hasattr(objective, "model_dump"):
        objective = objective.model_dump(by_alias=False)
    if not isinstance(objective, dict):
        return None
    value = objective.get("entity_id") or objective.get("entityId")
    return value if isinstance(value, str) and value else None


def specification_type(spec: Any) -> str | None:
    return getattr(spec, "type", None) if spec is not None else None


@dataclass(frozen=True)
class AgentHealth:
    """Health inputs; ``build_agent_entity`` maps them onto Lattice components."""

    stream: str  # "connected" | "connecting" | "disconnected" | "stopped"
    llm: str  # "ok" | "unavailable" | "rejected" | "unused"
    running: bool = True


def _health(state: AgentHealth, now: datetime) -> Health:
    stream_health = {"connected": HEALTHY, "connecting": NOT_READY, "disconnected": WARN, "stopped": OFFLINE}[
        state.stream
    ]
    llm_health = {"ok": HEALTHY, "unavailable": WARN, "rejected": WARN, "unused": NOT_READY}[state.llm]
    llm_note = {
        "ok": "Last brief narrative written by the local cluster model",
        "unavailable": "Local cluster model down at last brief; briefs ship facts only",
        "rejected": "Last model output broke the brief format; brief shipped facts only",
        "unused": "No brief written yet",
    }[state.llm]
    overall = stream_health if state.running else OFFLINE
    return Health(
        health_status=overall,
        connection_status="CONNECTION_STATUS_ONLINE" if state.running else "CONNECTION_STATUS_OFFLINE",
        update_time=now,
        components=[
            ComponentHealth(
                id="task-stream",
                name="Lattice task stream",
                health=stream_health,
                messages=[ComponentMessage(status=stream_health, message=f"Task stream {state.stream}")],
                update_time=now,
            ),
            ComponentHealth(
                id="local-llm",
                name="Kodros cluster model",
                health=llm_health,
                messages=[ComponentMessage(status=llm_health, message=llm_note)],
                update_time=now,
            ),
        ],
    )


def build_agent_entity(
    *,
    entity_id: str,
    name: str,
    lat: float,
    lon: float,
    now: datetime,
    expiry_s: float,
    health: AgentHealth,
    capacity: int = DEFAULT_CAPACITY,
) -> dict[str, Any]:
    """``publish_entity`` kwargs for the analyst asset. Pure: all time comes from ``now``.

    ``capacity`` is carried as ``capacity=N`` in the description: SDK 5.4.0 has no structured
    capacity field on an entity, and the planner reads that token (``planner.snapshot``).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not 0 < expiry_s < 30 * 24 * 3600:
        raise ValueError("expiry_s must be positive and under 30 days")
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    return {
        "entity_id": entity_id,
        "is_live": True,
        "expiry_time": now + timedelta(seconds=expiry_s),
        "description": (
            "Kodros Analyst: software agent that answers Investigate tasks with wildfire intel briefs "
            "(objects lattice-wildfire/briefs/{task_id}.md|.json). "
            f"capacity={capacity}: it runs up to {capacity} Investigate tasks at once and queues the rest, "
            "which stay SENT until a slot frees. Location is the AOI coverage anchor, "
            "not a physical position."
        ),
        "aliases": Aliases(name=name),
        "location": point_location(lat, lon),
        "ontology": Ontology(template="TEMPLATE_ASSET", platform_type=PLATFORM_TYPE),
        "mil_view": MilView(disposition="DISPOSITION_FRIENDLY", environment="ENVIRONMENT_LAND"),
        "provenance": provenance(PKG, DATA_TYPE, now),
        "data_classification": unclassified(),
        "symbology": symbology("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND"),
        "task_catalog": TaskCatalog(
            task_definitions=[TaskDefinition(task_specification_url=INVESTIGATE_URL)]
        ),
        "health": _health(health, now),
        # A software analyst carries no physical sensor; declare that explicitly.
        "sensors": Sensors(sensors=[]),
    }


class TaskCancelled(Exception):
    """Raised at a worker checkpoint after the operator cancelled the task."""


class _ActiveTask:
    """Bookkeeping for one in-flight task; the lock guards version and terminal flag."""

    def __init__(self, task_id: str, status_version: int | None) -> None:
        self.task_id = task_id
        self.status_version = status_version or 0
        self.cancel = threading.Event()
        self.cancel_reason = "cancelled by operator"
        self.terminal = False
        self.lock = threading.Lock()

    def observe(self, status_version: int | None) -> None:
        if status_version is None:
            return
        with self.lock:
            if status_version > self.status_version:
                self.status_version = status_version

    def checkpoint(self) -> None:
        if self.cancel.is_set():
            raise TaskCancelled()


class AnalystAgent:
    """Publishes the analyst asset and executes Investigate tasks routed to it."""

    def __init__(
        self,
        client: Any,
        *,
        entity_id: str,
        anchor: tuple[float, float],
        name: str = "Kodros Analyst",
        sources: Sources | None = None,
        chat_result: Callable[..., ChatResult] | None = None,
        lattice_url: str | None = None,
        capacity: int = DEFAULT_CAPACITY,
        queue_max: int | None = None,
        heartbeat_s: float = 5.0,
        expiry_s: float = 30.0,
        stream_heartbeat_ms: int = 5000,
        reconnect_base_s: float = 1.0,
        reconnect_cap_s: float = 30.0,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if queue_max is not None and queue_max < 1:
            raise ValueError("queue_max must be at least 1")
        self.client = client
        self.entity_id = entity_id
        self.anchor = anchor
        self.name = name
        self.sources: Sources = sources or LiveSources()
        self.chat_result = chat_result
        self.lattice_url = lattice_url
        self.capacity = capacity
        self.queue_max = queue_max if queue_max is not None else default_queue_max(capacity)
        self.heartbeat_s = heartbeat_s
        self.expiry_s = expiry_s
        self.stream_heartbeat_ms = stream_heartbeat_ms
        self.reconnect_base_s = reconnect_base_s
        self.reconnect_cap_s = reconnect_cap_s
        self.author = Principal(system=System(entity_id=entity_id))
        self.mode = "stream"  # "stream" | "poll"
        self._stream_state = "connecting"
        self._llm_state = "unused"
        self._state_lock = threading.Lock()
        self._active: dict[str, _ActiveTask] = {}
        self._active_lock = threading.Lock()
        self._finished: deque[str] = deque(maxlen=_FINISHED_MEMORY)
        # Waiting execute requests; at most `capacity` workers take from it at a time, so exactly
        # that many tasks run and the rest stay SENT on the server until a slot frees.
        self._pending: queue.Queue[tuple[_ActiveTask, Any]] = queue.Queue(maxsize=self.queue_max)
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- entity heartbeat ---------------------------------------------------------------

    def health(self) -> AgentHealth:
        with self._state_lock:
            return AgentHealth(
                stream=self._stream_state, llm=self._llm_state, running=not self._stop.is_set()
            )

    def publish(self, *, running: bool = True, request_options: dict[str, Any] | None = None) -> None:
        """Publish the asset. ``request_options`` bounds the call on the shutdown path."""
        state = self.health()
        if not running:
            state = AgentHealth(stream="stopped", llm=state.llm, running=False)
        self.client.entities.publish_entity(
            **build_agent_entity(
                entity_id=self.entity_id,
                name=self.name,
                lat=self.anchor[0],
                lon=self.anchor[1],
                now=now_utc(),
                expiry_s=self.expiry_s,
                health=state,
                capacity=self.capacity,
            ),
            request_options=request_options,
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            try:
                self.publish()
            except Exception as exc:  # keep beating; the next publish may succeed
                log.warning("heartbeat publish failed: %s", exc)

    def _set_stream(self, state: str) -> None:
        with self._state_lock:
            self._stream_state = state

    # -- task stream -------------------------------------------------------------------

    def _selector(self) -> EntityIdsSelector:
        return EntityIdsSelector(entity_ids=[self.entity_id])

    def run_tasks(self, stop: threading.Event) -> None:
        """Consume task requests until ``stop`` is set, reconnecting on any error."""
        delay = self.reconnect_base_s
        while not stop.is_set():
            try:
                if self.mode == "stream":
                    for event in self.client.tasks.stream_as_agent(
                        agent_selector=self._selector(), heartbeat_interval_ms=self.stream_heartbeat_ms
                    ):
                        self._set_stream("connected")
                        delay = self.reconnect_base_s
                        if stop.is_set():
                            break
                        if getattr(event, "event", None) == "agent_request":
                            self.handle_request(event)
                    if stop.is_set():
                        break
                    log.info("task stream ended; reconnecting")
                else:
                    started = time.monotonic()
                    request = self.client.tasks.listen_as_agent(agent_selector=self._selector())
                    self._set_stream("connected")
                    delay = self.reconnect_base_s
                    handled = request is not None and self.handle_request(request)
                    if not handled and time.monotonic() - started < 0.5:
                        stop.wait(1.0)  # server answered an empty poll at once; do not spin
                    continue
            except ApiError as exc:
                if self.mode == "stream" and exc.status_code in _STREAM_UNSUPPORTED:
                    log.warning(
                        "agent stream unsupported (HTTP %s); using listen_as_agent long poll", exc.status_code
                    )
                    self.mode = "poll"
                    continue
                log.warning("task stream error HTTP %s; reconnecting", exc.status_code)
            except Exception as exc:
                if stop.is_set():
                    break
                log.warning("task stream error: %s; reconnecting", exc)
            self._set_stream("disconnected")
            stop.wait(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, self.reconnect_cap_s)

    def handle_request(self, request: Any) -> bool:
        """Dispatch one agent request (execute, cancel or complete). True if it carried one."""
        execute = getattr(request, "execute_request", None)
        cancel = getattr(request, "cancel_request", None)
        complete = getattr(request, "complete_request", None)
        if execute is not None and execute.task is not None:
            self._on_execute(execute.task)
        elif cancel is not None and cancel.task_id:
            self._on_cancel(cancel.task_id)
        elif complete is not None and complete.task_id:
            self._on_complete(complete.task_id)
        else:
            return False
        return True

    def _on_execute(self, task: Any) -> None:
        version = task.version
        task_id = version.task_id if version is not None else None
        if not task_id:
            log.warning("execute request without a task id; ignored")
            return
        status = task.status.status if task.status is not None else None
        if status in TERMINAL:
            log.info("task %s already %s; ignored", task_id, status)
            return
        with self._active_lock:
            if task_id in self._active or task_id in self._finished:
                log.info("task %s already handled; duplicate ignored", task_id)
                return
            active = _ActiveTask(task_id, version.status_version)
            self._active[task_id] = active
        if self._stop.is_set():
            self._report_stopping(active, SHUTDOWN_REQUEST_TIMEOUT_S)
            return
        self._ensure_workers()
        try:
            self._pending.put_nowait((active, task))
        except queue.Full:
            log.warning("task %s rejected: %s queued at capacity %s", task_id, self.queue_max, self.capacity)
            self._finish(active, terminal_reason=f"analyst busy: {self.queue_max} tasks already queued",
                         terminal_code="ERROR_CODE_REJECTED")  # fmt: skip
            return
        log.info("task %s accepted (running %s, queued %s)", task_id, self.capacity, self._pending.qsize())

    def _on_cancel(self, task_id: str) -> None:
        with self._active_lock:
            active = self._active.get(task_id)
        if active is None:
            log.info("cancel for task %s that is not in progress", task_id)
            return
        log.info("cancel requested for task %s", task_id)
        self._resync(active)
        active.cancel.set()

    def _on_complete(self, task_id: str) -> None:
        with self._active_lock:
            active = self._active.get(task_id)
        if active is None:
            return
        log.info("complete request for task %s; server already holds it terminal", task_id)
        self._resync(active)
        with active.lock:
            active.terminal = True
        active.cancel.set()

    # -- workers -----------------------------------------------------------------------

    def _ensure_workers(self) -> None:
        """Start the ``capacity`` workers once. Idempotent; a stopped agent starts no workers."""
        with self._workers_lock:
            if self._workers or self._stop.is_set():
                return
            for index in range(self.capacity):
                worker = threading.Thread(
                    target=self._worker_loop, name=f"analyst-worker-{index}", daemon=True
                )
                worker.start()
                self._workers.append(worker)

    def _worker_loop(self) -> None:
        while True:
            try:
                active, task = self._pending.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self.execute(active, task)
            except Exception:
                log.exception("worker failed on task %s", active.task_id)
            finally:
                self._pending.task_done()

    def _finish(
        self,
        active: _ActiveTask,
        *,
        terminal_reason: str | None = None,
        terminal_code: str | None = None,
        request_options: dict[str, Any] | None = None,
    ) -> None:
        """Report a task the agent will not run and forget it. Never leaves it EXECUTING."""
        if terminal_reason is not None:
            try:
                self._update(
                    active,
                    STATUS_DONE_NOT_OK,
                    error=terminal_reason,
                    code=terminal_code or "ERROR_CODE_FAILED",
                    request_options=request_options,
                )
            except Exception as exc:
                log.error("could not report task %s terminal: %s", active.task_id, exc)
        self._forget(active)

    def _forget(self, active: _ActiveTask) -> None:
        """Drop a task from the bookkeeping so a redelivery is ignored."""
        with self._active_lock:
            self._active.pop(active.task_id, None)
            self._finished.append(active.task_id)

    def _finish_cancelled(self, active: _ActiveTask) -> None:
        options = None
        if self._stop.is_set():
            options = _shutdown_request_options(time.monotonic() + SHUTDOWN_REQUEST_TIMEOUT_S)
        self._finish(
            active,
            terminal_reason=active.cancel_reason,
            terminal_code="ERROR_CODE_CANCELLED",
            request_options=options,
        )

    def _take_pending(self) -> list[_ActiveTask]:
        """Empty the queue so no worker starts a waiting request during shutdown."""
        taken: list[_ActiveTask] = []
        while True:
            try:
                active, _task = self._pending.get_nowait()
            except queue.Empty:
                return taken
            active.cancel_reason = STOP_REASON
            active.cancel.set()
            taken.append(active)

    def _stop_targets(self) -> list[_ActiveTask]:
        """Cancel every task this agent still owes a terminal state (queued and running)."""
        self._take_pending()
        with self._active_lock:
            active = list(self._active.values())
        for task in active:
            task.cancel_reason = STOP_REASON
            task.cancel.set()
        return active

    def _report_stopping(self, task: _ActiveTask, timeout_s: float) -> bool:
        """One best-effort ``DONE_NOT_OK`` for a task the shutdown will not finish.

        Returns False when Lattice is unreachable, so the caller skips the remaining updates and
        the final publish: the supervisor kills the process long before a retry wait would land.
        """
        task.cancel_reason = STOP_REASON
        task.cancel.set()
        options = _shutdown_request_options(time.monotonic() + timeout_s)
        if options is None:
            return False
        try:
            self._update(
                task,
                STATUS_DONE_NOT_OK,
                error=STOP_REASON,
                code="ERROR_CODE_CANCELLED",
                request_options=options,
                resync=False,
            )
        except httpx.TransportError as exc:
            log.warning(
                "Lattice unreachable at shutdown (%s: %s); remaining task updates and the OFFLINE "
                "publish are skipped",
                type(exc).__name__,
                exc,
            )
            return False
        except Exception as exc:
            log.warning("could not report task %s on shutdown: %s", task.task_id, exc)
        self._forget(task)
        return True

    # -- status updates ----------------------------------------------------------------

    def _resync(self, active: _ActiveTask) -> bool:
        """Adopt the server's status version; mark terminal if the server already is. True if terminal."""
        try:
            task = self.client.tasks.get_task(active.task_id)
        except Exception as exc:
            log.warning("could not re-read task %s: %s", active.task_id, exc)
            return False
        if task.version is not None:
            active.observe(task.version.status_version)
        if task.status is not None and task.status.status in TERMINAL:
            with active.lock:
                active.terminal = True
            return True
        return False

    def _update(
        self,
        active: _ActiveTask,
        status: str,
        *,
        error: str | None = None,
        code: str | None = None,
        request_options: dict[str, Any] | None = None,
        resync: bool = True,
    ) -> bool:
        with active.lock:
            if active.terminal:
                return False
            active.status_version += 1
            version = active.status_version
        fields: dict[str, Any] = {"status": status}
        if error is not None:
            fields["task_error"] = TaskError(code=code or "ERROR_CODE_FAILED", message=error[:500])
        try:
            returned = self.client.tasks.update_task_status(
                active.task_id,
                status_version=version,
                new_status=TaskStatus(**fields),
                author=self.author,
                request_options=request_options,
            )
        except Exception:
            if resync and self._resync(active):
                log.info("task %s closed server-side; %s not sent", active.task_id, status)
                return False
            raise
        if returned is not None and returned.version is not None:
            active.observe(returned.version.status_version)
        if status in TERMINAL:
            with active.lock:
                active.terminal = True
        log.info(
            "task %s -> %s (status_version %s)%s",
            active.task_id,
            status,
            version,
            f": {error}" if error else "",
        )
        return True

    # -- execution ---------------------------------------------------------------------

    def produce_brief(self, task_id: str, target_entity_id: str, checkpoint: Callable[[], None]) -> Brief:
        """Read the target from Lattice, then fetch, gather, narrate and render its brief."""
        target = target_from_entity(self.client.entities.get_entity(target_entity_id))
        checkpoint()
        brief = make_brief(
            target,
            task_id=task_id,
            sources=self.sources,
            client=self.client,
            lattice_url=self.lattice_url,
            chat_result=self.chat_result,
            checkpoint=checkpoint,
        )
        with self._state_lock:
            self._llm_state = brief.narrative.status  # "ok" | "unavailable" | "rejected"
        return brief

    def _upload(self, task_id: str, brief: Brief) -> tuple[str, str]:
        """Upload both brief objects, JSON first so a half-written pair never looks complete."""
        md_path, json_path = object_paths(task_id)
        self.client.objects.upload_object(
            json_path, request=json.dumps(brief.document, indent=1, sort_keys=True).encode("utf-8")
        )
        self.client.objects.upload_object(md_path, request=brief.markdown.encode("utf-8"))
        return md_path, json_path

    def _discard(self, paths: tuple[str, str]) -> None:
        for path in paths:
            try:
                self.client.objects.delete_object(path)
            except Exception as exc:
                log.warning("could not delete %s after cancel: %s", path, exc)

    def execute(self, active: _ActiveTask, task: Any) -> None:
        """Run one task to a terminal state (worker thread)."""
        uploaded: tuple[str, str] | None = None
        if active.cancel.is_set():
            # Cancelled while queued: the task never reached EXECUTING and must not now.
            self._finish_cancelled(active)
            return
        try:
            spec_type = specification_type(task.specification)
            if spec_type != INVESTIGATE_URL:
                self._update(
                    active,
                    STATUS_DONE_NOT_OK,
                    error=f"unsupported task type: {spec_type}",
                    code="ERROR_CODE_REJECTED",
                )
                return
            target_id = objective_entity_id(task.specification)
            if target_id is None:
                self._update(
                    active,
                    STATUS_DONE_NOT_OK,
                    error="Investigate task has no objective.entity_id",
                    code="ERROR_CODE_REJECTED",
                )
                return
            if not self._update(active, STATUS_EXECUTING):
                return
            brief = self.produce_brief(active.task_id, target_id, active.checkpoint)
            active.checkpoint()
            uploaded = self._upload(active.task_id, brief)
            active.checkpoint()
            self._update(active, STATUS_DONE_OK)
        except TaskCancelled:
            if uploaded is not None:
                self._discard(uploaded)
            self._finish_cancelled(active)
        except Exception as exc:
            log.exception("task %s failed", active.task_id)
            options = None
            if self._stop.is_set():
                options = _shutdown_request_options(time.monotonic() + SHUTDOWN_REQUEST_TIMEOUT_S)
            try:
                self._update(
                    active,
                    STATUS_DONE_NOT_OK,
                    error=f"{type(exc).__name__}: {exc}",
                    request_options=options,
                    resync=options is None,
                )
            except Exception as update_exc:
                log.error("could not report failure of task %s: %s", active.task_id, update_exc)
        finally:
            self._forget(active)

    # -- process lifecycle ---------------------------------------------------------------

    def start(self) -> AnalystAgent:
        """Publish once (the entity must be live before the stream opens), then start threads."""
        self._stop.clear()
        self.publish()
        self._ensure_workers()
        for target, name in (
            (self._heartbeat_loop, "analyst-heartbeat"),
            (lambda: self.run_tasks(self._stop), "analyst-tasks"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """Shut down within ``timeout`` seconds; never wait on a task or its model call.

        In-flight and queued tasks each get one best-effort ``DONE_NOT_OK`` (reason
        :data:`STOP_REASON`) with :data:`SHUTDOWN_REQUEST_TIMEOUT_S` and no retries, then the entity
        is published OFFLINE with the same options. If Lattice is unreachable the rest is skipped
        after one log line: the supervisor kills anything still alive after its grace period, so
        retrying would only cost the process its exit. Workers are daemon threads; one blocked in
        the local model is abandoned and its brief discarded.
        """
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        deadline = time.monotonic() + timeout
        self._stop.set()
        targets = self._stop_targets()
        reachable = True
        for index, task in enumerate(targets):
            if not self._report_stopping(task, max(0.0, deadline - time.monotonic())):
                skipped = len(targets) - index
                if skipped:
                    log.warning("%s task(s) left without a terminal update at shutdown", skipped)
                reachable = False
                break
        if reachable:
            options = _shutdown_request_options(deadline)
            try:
                if options is not None:
                    self.publish(running=False, request_options=options)
            except Exception as exc:
                log.warning("offline publish failed: %s", exc)
        with self._workers_lock:
            self._workers.clear()  # abandoned workers are daemons; nothing joins them
        for thread in self._threads:
            thread.join(timeout=max(0.0, min(0.2, deadline - time.monotonic())))
        self._threads.clear()

    def in_flight(self) -> list[str]:
        """Task ids currently being worked."""
        with self._active_lock:
            return sorted(self._active)

    def cancel_requested(self, task_id: str) -> bool:
        """True once a cancel for an in-flight task has reached its worker."""
        with self._active_lock:
            active = self._active.get(task_id)
        return active is not None and active.cancel.is_set()
