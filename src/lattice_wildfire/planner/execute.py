"""Execution side of the planner: Dispatcher (cancel timed-out tasks, create tasks),
Monitor (follow tasks to a terminal state), the JSONL timeline, and state persistence.

Patterned on anduril sample-app-auto-reconnaissance `Tasker.orbit` (create_task with
author, assignee relation and initial entities) and `Tasker.check_executing` (get_task
status). State transitions (`apply_status`, `apply_timeout`) are pure, in `model.py`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anduril import GoogleProtobufAny, Lattice, Principal, Relations, System, TaskEntity
from anduril.core import ApiError

from lattice_wildfire.core.entities import now_utc

from .model import (
    INTEGRATION_NAME,
    INVESTIGATE_URL,
    Assignment,
    CandidateHistory,
    Plan,
    PlannerState,
    Snapshot,
    TaskRecord,
    apply_candidate_gone,
    apply_status,
    apply_timeout,
    candidate_is_gone,
    is_terminal,
)

PLANNER_PRINCIPAL = Principal(system=System(service_name=INTEGRATION_NAME))

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- timeline


@dataclass
class Timeline:
    """Append-only JSONL log of planner events; `events` keeps this run's copy."""

    path: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    last_deferred: dict[str, str] | None = None

    def record(self, event: str, now: datetime, **fields: Any) -> dict[str, Any]:
        row = {"ts": now.isoformat(), "event": event, **fields}
        self.events.append(row)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return row


# --------------------------------------------------------------------------- state io


def load_state(path: Path) -> PlannerState:
    if not path.exists():
        return PlannerState()
    return PlannerState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_state(state: PlannerState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- Dispatcher


def build_task_request(assignment: Assignment, candidate_entity: Any | None = None) -> dict[str, Any]:
    """`tasks.create_task` kwargs for one assignment. Pure."""
    kwargs: dict[str, Any] = {
        "display_name": assignment.display_name,
        "description": (
            f"{INTEGRATION_NAME}: investigate new-start candidate {assignment.candidate_id}"
            f" (priority {assignment.priority:.3f}, attempt {assignment.attempt})"
        ),
        "specification": GoogleProtobufAny(
            type=INVESTIGATE_URL, objective={"entity_id": assignment.candidate_id}
        ),
        "author": PLANNER_PRINCIPAL,
        "relations": Relations(assignee=Principal(system=System(entity_id=assignment.asset_id))),
        "is_executed_elsewhere": False,
    }
    if candidate_entity is not None:
        kwargs["initial_entities"] = [TaskEntity(entity=candidate_entity, snapshot=False)]
    return kwargs


@dataclass
class Dispatcher:
    client: Lattice
    timeline: Timeline

    def cancel_gone(self, snapshot: Snapshot, state: PlannerState) -> list[dict[str, Any]]:
        """cancel_task every open planner task whose candidate entity is no longer live.

        A candidate is gone when its entity is missing (expired or deleted) or its description
        carries ``retired:``. The task is closed locally as CANCELLED: it does not count as a
        failed attempt, so it neither spends the retry budget nor starts a backoff.
        """
        changes: list[dict[str, Any]] = []
        for rec in list(state.open_records()):
            if not candidate_is_gone(snapshot.entities, rec.candidate_id):
                continue
            now = now_utc()
            try:
                task = self.client.tasks.cancel_task(rec.task_id, author=PLANNER_PRINCIPAL)
            except ApiError as exc:
                log.error("cancel_task failed for gone candidate %s: %s", rec.candidate_id, exc.status_code)
                self.timeline.record(
                    "cancel_failed",
                    now,
                    task_id=rec.task_id,
                    candidate_id=rec.candidate_id,
                    asset_id=rec.asset_id,
                    error=f"HTTP {exc.status_code}: {exc.body}",
                )
                continue
            change = apply_candidate_gone(
                state, rec.task_id, now, f"candidate {rec.candidate_id} is no longer live"
            )
            if change:
                lattice_status = task.status.status if task.status else None
                self.timeline.record("candidate_gone", now, **change, lattice_status=lattice_status)
                changes.append(change)
        return changes

    def cancel_timeouts(self, plan: Plan, state: PlannerState) -> set[str]:
        """cancel_task every timed-out task in the plan and fold the timeout into state as a
        failed attempt. Returns candidate and asset ids whose cancel failed (the task may
        have finished meanwhile); the Monitor resolves those next cycle."""
        blocked: set[str] = set()
        for tid, reason in plan.timeouts.items():
            rec = state.tasks.get(tid)
            if rec is None or is_terminal(rec.status):
                continue
            now = now_utc()
            try:
                task = self.client.tasks.cancel_task(tid, author=PLANNER_PRINCIPAL)
            except ApiError as exc:
                log.error("cancel_task failed for timed-out task %s: %s", tid, exc.status_code)
                self.timeline.record(
                    "cancel_failed",
                    now,
                    task_id=tid,
                    candidate_id=rec.candidate_id,
                    asset_id=rec.asset_id,
                    error=f"HTTP {exc.status_code}: {exc.body}",
                )
                blocked.update((rec.candidate_id, rec.asset_id))
                continue
            change = apply_timeout(state, tid, now, reason)
            if change:
                lattice_status = task.status.status if task.status else None
                self.timeline.record("timeout", now, **change, lattice_status=lattice_status)
        return blocked

    def dispatch(self, plan: Plan, snapshot: Snapshot, state: PlannerState) -> list[TaskRecord]:
        """Cancel timed-out tasks, then create one Investigate task per assignment."""
        blocked = self.cancel_timeouts(plan, state)
        created = []
        for a in plan.assignments:
            if a.candidate_id in blocked or a.asset_id in blocked:
                continue
            now = now_utc()
            try:
                task = self.client.tasks.create_task(
                    **build_task_request(a, snapshot.entities.get(a.candidate_id))
                )
            except ApiError as exc:
                log.error("create_task failed for %s -> %s: %s", a.candidate_id, a.asset_id, exc.status_code)
                self.timeline.record(
                    "dispatch_failed",
                    now,
                    candidate_id=a.candidate_id,
                    asset_id=a.asset_id,
                    error=f"HTTP {exc.status_code}: {exc.body}",
                )
                continue
            status = (task.status.status if task.status else None) or "STATUS_CREATED"
            rec = TaskRecord(
                task_id=task.version.task_id,
                candidate_id=a.candidate_id,
                asset_id=a.asset_id,
                attempt=a.attempt,
                status=status,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            state.tasks[rec.task_id] = rec
            state.history.setdefault(a.candidate_id, CandidateHistory()).attempts += 1
            self.timeline.record(
                "created",
                now,
                task_id=rec.task_id,
                candidate_id=a.candidate_id,
                asset_id=a.asset_id,
                attempt=a.attempt,
                status=status,
                display_name=a.display_name,
                distance_m=a.distance_m,
                priority=a.priority,
            )
            created.append(rec)
        return created


# --------------------------------------------------------------------------- Monitor


def task_error_text(task: Any) -> str | None:
    err = task.status.task_error if task.status else None
    if err is None:
        return None
    return ": ".join(p for p in (err.code, err.message) if p) or None


@dataclass
class Monitor:
    client: Lattice
    timeline: Timeline

    def poll(self, state: PlannerState, task_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """get_task every open record (or the given ids); record status changes."""
        ids = list(task_ids) if task_ids is not None else [r.task_id for r in state.open_records()]
        changes = []
        for tid in ids:
            rec = state.tasks.get(tid)
            if rec is None or is_terminal(rec.status):
                continue
            now = now_utc()
            try:
                task = self.client.tasks.get_task(task_id=tid)
            except ApiError as exc:
                self.timeline.record("poll_failed", now, task_id=tid, error=f"HTTP {exc.status_code}")
                continue
            status = (task.status.status if task.status else None) or rec.status
            change = apply_status(state, tid, status, now, task_error_text(task))
            if change:
                self.timeline.record("terminal" if change.get("terminal") else "status", now, **change)
                changes.append(change)
        return changes

    def wait_terminal(
        self,
        state: PlannerState,
        task_ids: Iterable[str],
        timeout_s: float,
        interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Poll until every given task is terminal or `timeout_s` passes."""
        ids = list(task_ids)
        deadline = clock() + timeout_s
        while True:
            self.poll(state, ids)
            if all(is_terminal(state.tasks[t].status) for t in ids if t in state.tasks):
                return True
            if clock() >= deadline:
                return False
            sleep(interval_s)
