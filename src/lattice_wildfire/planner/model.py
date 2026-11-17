"""Planner data model: the snapshot the agents read, the plan they produce, and the
state the executor carries between cycles.

Everything here is plain data. Planning agents (`agents.py`) read a `Snapshot` and a
`PlannerState` and return a `Plan`; only the executor (`dispatch.py`, `monitor.py`)
mutates `PlannerState`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

INTEGRATION_NAME = "lattice-wildfire-planner"
INVESTIGATE_URL = "type.googleapis.com/anduril.tasks.v2.Investigate"

CANDIDATE_DATA_TYPE = "ember-candidate"
RED_FLAG_DATA_TYPE = "nws-red-flag"
STATION_DATA_TYPE = "lacofd-station"

STATUS_DONE_OK = "STATUS_DONE_OK"
STATUS_DONE_NOT_OK = "STATUS_DONE_NOT_OK"
# Task closed because its candidate entity was retired or disappeared. Terminal, and neither
# a success nor a failure for retry accounting.
STATUS_CANCELLED = "STATUS_CANCELLED"
# Terminal task states. REPLACED means another task superseded this one: closed, but
# neither a success nor a failure for retry accounting.
TERMINAL_STATUSES = frozenset({STATUS_DONE_OK, STATUS_DONE_NOT_OK, "STATUS_REPLACED", STATUS_CANCELLED})
# Marker in a candidate description that means the candidate is retired and must not be tasked.
RETIRED_MARKER = "retired:"
# Non-terminal states that `query_tasks` is asked for when rebuilding the open-task view.
OPEN_STATUSES = (
    "STATUS_CREATED",
    "STATUS_SCHEDULED_IN_MANAGER",
    "STATUS_SENT",
    "STATUS_MACHINE_RECEIPT",
    "STATUS_ACK",
    "STATUS_WILCO",
    "STATUS_EXECUTING",
    "STATUS_WAITING_FOR_UPDATE",
    "STATUS_CANCEL_REQUESTED",
    "STATUS_COMPLETE_REQUESTED",
)


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


# --------------------------------------------------------------------------- snapshot


@dataclass(frozen=True)
class Candidate:
    """An `ember-candidate` track as the planner sees it."""

    entity_id: str
    name: str
    lat: float
    lon: float
    score: float | None  # 0..1 from tracked.track_quality_wrapper / 15
    observed_at: datetime | None
    description: str = ""


@dataclass(frozen=True)
class Asset:
    """A TEMPLATE_ASSET entity. `task_types` is its advertised task catalog.

    `capacity` is how many Investigate tasks the asset can run at once, read from the
    entity when it advertises one (`capacity=N` in its description). None means the
    entity says nothing; `Policy.default_capacity` then applies.
    """

    entity_id: str
    name: str
    lat: float | None
    lon: float | None
    task_types: frozenset[str]
    connection_status: str | None = None
    health_status: str | None = None
    capacity: int | None = None

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class Zone:
    """A polygon context layer (red flag warning). `ring` is [(lat, lon), ...]."""

    entity_id: str
    name: str
    ring: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Site:
    """A point we care about protecting (fire station)."""

    entity_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class OpenTask:
    """A non-terminal Investigate task seen in Lattice (any author)."""

    task_id: str
    candidate_id: str | None
    assignee_id: str | None
    status: str


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    candidates: tuple[Candidate, ...] = ()
    assets: tuple[Asset, ...] = ()
    red_flags: tuple[Zone, ...] = ()
    stations: tuple[Site, ...] = ()
    open_tasks: tuple[OpenTask, ...] = ()
    # Raw SDK entities by id, used only by the dispatcher for `initial_entities`.
    entities: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def is_retired(description: str | None) -> bool:
    """True when a candidate description carries the ``retired:`` marker."""
    return RETIRED_MARKER in (description or "").lower()


def candidate_is_gone(entities: Mapping[str, Any], candidate_id: str) -> bool:
    """True when the candidate entity is missing (expired or deleted) or marked ``retired:``.

    ``entities`` is the snapshot's current-entity map, so an expired entity is absent from it.
    """
    entity = entities.get(candidate_id)
    if entity is None:
        return True
    return is_retired(entity.description)


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    priority: float
    factors: dict[str, float]


@dataclass(frozen=True)
class Assignment:
    candidate_id: str
    candidate_name: str
    asset_id: str
    asset_name: str
    distance_m: float | None
    priority: float
    attempt: int

    @property
    def display_name(self) -> str:
        return f"Investigate {self.candidate_name}"


@dataclass(frozen=True)
class Plan:
    """Pure planning output. `reasons` explains every candidate, assigned or not.
    `timeouts` maps planner task ids to cancel (no terminal status in time) to the reason."""

    assignments: tuple[Assignment, ...]
    deferred: tuple[str, ...]
    reasons: dict[str, str]
    timeouts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignments": [asdict(a) | {"display_name": a.display_name} for a in self.assignments],
            "deferred": list(self.deferred),
            "reasons": dict(self.reasons),
            "timeouts": dict(self.timeouts),
        }


@dataclass(frozen=True)
class Policy:
    max_retries: int = 2  # retries after the first DONE_NOT_OK; 2 -> at most 3 attempts
    retry_backoff_s: float = 60.0  # wait after DONE_NOT_OK before retrying
    cooldown_s: float = 3600.0  # wait after DONE_OK before re-investigating
    max_range_m: float | None = None  # None: any distance
    min_priority: float = 0.0
    # Cancel a planner task with no terminal status this long after its creation or last
    # observed status change; the timeout counts as a failed attempt. None disables.
    task_timeout_s: float | None = 900.0
    # Concurrent Investigate tasks an asset may hold when the asset entity advertises no
    # capacity of its own.
    default_capacity: int = 1
    # Explicit per-asset capacity, keyed by entity id (CLI `--capacity ASSET_ID=N`). Beats
    # the capacity the asset entity advertises.
    asset_capacity: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- state


@dataclass
class TaskRecord:
    task_id: str
    candidate_id: str
    asset_id: str
    attempt: int
    status: str
    created_at: str  # ISO-8601 UTC
    updated_at: str
    error: str | None = None
    # True when the planner closed this record by timeout (cancel_task was sent); Lattice
    # may still report CANCEL_REQUESTED until the assignee answers.
    timed_out: bool = False


@dataclass
class CandidateHistory:
    attempts: int = 0
    failures: int = 0
    last_status: str | None = None
    last_terminal_at: str | None = None
    failed_assets: list[str] = field(default_factory=list)
    last_error: str | None = None


@dataclass
class PlannerState:
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    history: dict[str, CandidateHistory] = field(default_factory=dict)

    def open_records(self) -> list[TaskRecord]:
        return [r for r in self.tasks.values() if not is_terminal(r.status)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": {k: asdict(v) for k, v in self.tasks.items()},
            "history": {k: asdict(v) for k, v in self.history.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannerState:
        return cls(
            tasks={k: TaskRecord(**v) for k, v in data.get("tasks", {}).items()},
            history={k: CandidateHistory(**v) for k, v in data.get("history", {}).items()},
        )


# --------------------------------------------------------------------------- transitions


def apply_status(
    state: PlannerState, task_id: str, status: str, now: datetime, error: str | None = None
) -> dict[str, Any] | None:
    """Fold one observed task status into state. Returns a change dict, or None if
    nothing changed. Pure apart from mutating `state`."""
    rec = state.tasks.get(task_id)
    if rec is None or rec.status == status or is_terminal(rec.status):
        return None
    prev = rec.status
    rec.status = status
    rec.updated_at = now.isoformat()
    if error:
        rec.error = error
    change: dict[str, Any] = {
        "task_id": task_id,
        "candidate_id": rec.candidate_id,
        "asset_id": rec.asset_id,
        "attempt": rec.attempt,
        "status": status,
        "prev_status": prev,
    }
    if error:
        change["error"] = error
    if is_terminal(status):
        hist = state.history.setdefault(rec.candidate_id, CandidateHistory(attempts=rec.attempt))
        hist.last_status = status
        hist.last_terminal_at = now.isoformat()
        if status == STATUS_DONE_OK:
            hist.failures = 0
            hist.failed_assets = []
            hist.last_error = None
        elif status == STATUS_DONE_NOT_OK:
            hist.failures += 1
            if rec.asset_id not in hist.failed_assets:
                hist.failed_assets.append(rec.asset_id)
            hist.last_error = error
        change["terminal"] = True
    return change


def stale_task_ids(state: PlannerState, now: datetime, timeout_s: float | None) -> dict[str, str]:
    """Open planner tasks whose last change (creation or observed status) is at least
    `timeout_s` old, mapped to the timeout reason. Pure."""
    if timeout_s is None:
        return {}
    out = {}
    for rec in state.open_records():
        since = datetime.fromisoformat(rec.updated_at)
        if (now - since).total_seconds() >= timeout_s:
            out[rec.task_id] = f"timeout: {rec.status} for {timeout_s:.0f} s without a terminal status"
    return out


def apply_timeout(state: PlannerState, task_id: str, now: datetime, reason: str) -> dict[str, Any] | None:
    """Close a timed-out record as a failed attempt (same accounting as DONE_NOT_OK)."""
    change = apply_status(state, task_id, STATUS_DONE_NOT_OK, now, reason)
    if change:
        state.tasks[task_id].timed_out = True
    return change


def apply_candidate_gone(
    state: PlannerState, task_id: str, now: datetime, reason: str
) -> dict[str, Any] | None:
    """Close a task whose candidate disappeared. No failure, no retry backoff, no cooldown.

    Pure apart from mutating `state`. The record is terminal, so the candidate's retry history
    is untouched and the record stops blocking its asset.
    """
    rec = state.tasks.get(task_id)
    if rec is None or is_terminal(rec.status):
        return None
    prev = rec.status
    rec.status = STATUS_CANCELLED
    rec.updated_at = now.isoformat()
    return {
        "task_id": task_id,
        "candidate_id": rec.candidate_id,
        "asset_id": rec.asset_id,
        "attempt": rec.attempt,
        "status": STATUS_CANCELLED,
        "prev_status": prev,
        "error": reason,
        "terminal": True,
    }
