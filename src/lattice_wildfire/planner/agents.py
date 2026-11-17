"""Planning agents. Each is a pure function object over one `Snapshot` (plus the
executor's `PlannerState`); `plan()` runs them in order and returns a `Plan`.

Patterned on anduril sample-app-auto-reconnaissance `Arbiter.arbitrate_isr`: find an
asset for each non-friendly track, skip pairs that already have a task in progress,
then task. Here the steps are split into separate agents so each decision is testable
and explained:

1. Prioritizer: rank candidates by score, red flag, exposure, recency.
2. Deconflictor: one open task per candidate, cooldown, retry budget on DONE_NOT_OK.
3. AssetMatcher: nearest asset advertising Investigate with a free slot for every active task.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from lattice_wildfire.core.geo import haversine_m, point_in_polygon

from .model import (
    INVESTIGATE_URL,
    STATUS_DONE_NOT_OK,
    STATUS_DONE_OK,
    Asset,
    Assignment,
    Plan,
    PlannerState,
    Policy,
    Ranked,
    Snapshot,
    apply_timeout,
    is_terminal,
    stale_task_ids,
)

OFFLINE_CONNECTION = "CONNECTION_STATUS_OFFLINE"
UNAVAILABLE_HEALTH = frozenset({"HEALTH_STATUS_FAIL", "HEALTH_STATUS_OFFLINE"})


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# --------------------------------------------------------------------------- Prioritizer


@dataclass(frozen=True)
class Prioritizer:
    """Rank candidates. priority = sum(weight * factor), every factor in 0..1."""

    w_score: float = 0.5
    w_red_flag: float = 0.2
    w_exposure: float = 0.2
    w_recency: float = 0.1
    exposure_near_m: float = 5_000.0  # station within this -> exposure 1.0
    exposure_far_m: float = 10_000.0  # station within this -> exposure 0.5
    recency_horizon_s: float = 24 * 3600.0  # age at which recency reaches 0

    def factors(self, snapshot: Snapshot, cand) -> dict[str, float]:
        score = cand.score if cand.score is not None else 0.0
        red_flag = (
            1.0
            if any(point_in_polygon(cand.lat, cand.lon, list(z.ring)) for z in snapshot.red_flags)
            else 0.0
        )
        nearest = min(
            (haversine_m(cand.lat, cand.lon, s.lat, s.lon) for s in snapshot.stations),
            default=None,
        )
        if nearest is not None and nearest <= self.exposure_near_m:
            exposure = 1.0
        elif nearest is not None and nearest <= self.exposure_far_m:
            exposure = 0.5
        else:
            exposure = 0.0
        if cand.observed_at is None:
            recency = 0.0
        else:
            age = max(0.0, (snapshot.now - cand.observed_at).total_seconds())
            recency = max(0.0, 1.0 - age / self.recency_horizon_s)
        return {"score": score, "red_flag": red_flag, "exposure": exposure, "recency": recency}

    def rank(self, snapshot: Snapshot) -> list[Ranked]:
        ranked = []
        for cand in snapshot.candidates:
            f = self.factors(snapshot, cand)
            priority = (
                self.w_score * f["score"]
                + self.w_red_flag * f["red_flag"]
                + self.w_exposure * f["exposure"]
                + self.w_recency * f["recency"]
            )
            ranked.append(Ranked(cand, round(priority, 6), f))
        ranked.sort(key=lambda r: (-r.priority, r.candidate.entity_id))
        return ranked


# --------------------------------------------------------------------------- Deconflictor


@dataclass(frozen=True)
class Actionable:
    ranked: Ranked
    attempt: int
    avoid_assets: frozenset[str]


@dataclass(frozen=True)
class Deconflictor:
    """Decide which ranked candidates may be tasked this cycle."""

    policy: Policy = Policy()

    def screen(
        self, ranked: list[Ranked], snapshot: Snapshot, state: PlannerState
    ) -> tuple[list[Actionable], dict[str, str]]:
        open_by_candidate: dict[str, str] = {}
        for rec in state.open_records():
            open_by_candidate.setdefault(rec.candidate_id, f"open task {rec.task_id} ({rec.status})")
        for task in snapshot.open_tasks:
            if task.candidate_id:
                open_by_candidate.setdefault(task.candidate_id, f"open task {task.task_id} ({task.status})")

        actionable: list[Actionable] = []
        reasons: dict[str, str] = {}
        for r in ranked:
            cid = r.candidate.entity_id
            if cid in open_by_candidate:
                reasons[cid] = open_by_candidate[cid]
                continue
            hist = state.history.get(cid)
            if hist is not None:
                last = _parse_ts(hist.last_terminal_at)
                if hist.failures > self.policy.max_retries:
                    reasons[cid] = (
                        f"retries exhausted: {hist.failures} DONE_NOT_OK"
                        f" (max {self.policy.max_retries} retries); last error: {hist.last_error or 'none given'}"
                    )
                    continue
                if hist.last_status == STATUS_DONE_NOT_OK and last is not None:
                    until = last + timedelta(seconds=self.policy.retry_backoff_s)
                    if snapshot.now < until:
                        reasons[cid] = f"retry backoff until {until.isoformat()}"
                        continue
                if hist.last_status == STATUS_DONE_OK and last is not None:
                    until = last + timedelta(seconds=self.policy.cooldown_s)
                    if snapshot.now < until:
                        reasons[cid] = f"cooldown after DONE_OK until {until.isoformat()}"
                        continue
            if r.priority < self.policy.min_priority:
                reasons[cid] = f"priority {r.priority:.3f} below minimum {self.policy.min_priority:.3f}"
                continue
            attempt = (hist.attempts if hist else 0) + 1
            avoid = frozenset(hist.failed_assets) if hist else frozenset()
            actionable.append(Actionable(r, attempt, avoid))
        return actionable, reasons


# --------------------------------------------------------------------------- AssetMatcher


def is_capable(asset: Asset) -> bool:
    return INVESTIGATE_URL in asset.task_types


def is_available(asset: Asset) -> bool:
    return asset.connection_status != OFFLINE_CONNECTION and asset.health_status not in UNAVAILABLE_HEALTH


def capacity_of(asset: Asset, policy: Policy) -> int:
    """Concurrent Investigate tasks this asset may hold.

    Precedence: the `Policy` override for the asset id, then the capacity the asset
    entity advertises, then `policy.default_capacity`. Never below 1.
    """
    override = policy.asset_capacity.get(asset.entity_id)
    if override is not None:
        return max(1, override)
    if asset.capacity is not None:
        return max(1, asset.capacity)
    return max(1, policy.default_capacity)


@dataclass(frozen=True)
class AssetMatcher:
    """Greedy, priority-ordered: each candidate gets the nearest asset with free capacity.

    An asset holds up to `capacity_of(asset, policy)` active tasks. Assets without a
    location (an AI analyst) are eligible but rank after every located asset. Assets that
    already failed this candidate rank after the rest.
    """

    policy: Policy = Policy()

    def busy_assets(self, snapshot: Snapshot, state: PlannerState) -> dict[str, tuple[str, ...]]:
        """Open task ids per asset, from planner state and from Lattice (deduplicated)."""
        busy: dict[str, list[str]] = {}
        for r in state.open_records():
            busy.setdefault(r.asset_id, []).append(r.task_id)
        for t in snapshot.open_tasks:
            if not t.assignee_id:
                continue
            ids = busy.setdefault(t.assignee_id, [])
            if t.task_id not in ids:
                ids.append(t.task_id)
        return {k: tuple(v) for k, v in busy.items()}

    def match(
        self, actionable: list[Actionable], snapshot: Snapshot, state: PlannerState
    ) -> tuple[list[Assignment], dict[str, str]]:
        capable = [a for a in snapshot.assets if is_capable(a)]
        healthy = [a for a in capable if is_available(a)]
        busy = {k: list(v) for k, v in self.busy_assets(snapshot, state).items()}

        def free_slots(asset: Asset) -> int:
            return capacity_of(asset, self.policy) - len(busy.get(asset.entity_id, ()))

        assignments: list[Assignment] = []
        reasons: dict[str, str] = {}
        for act in actionable:
            cand = act.ranked.candidate
            free = [a for a in healthy if free_slots(a) > 0]
            options: list[tuple[tuple, Asset, float | None]] = []
            for a in free:
                dist = haversine_m(cand.lat, cand.lon, a.lat, a.lon) if a.located else None
                if (
                    dist is not None
                    and self.policy.max_range_m is not None
                    and dist > self.policy.max_range_m
                ):
                    continue
                key = (a.entity_id in act.avoid_assets, dist is None, dist or 0.0, a.entity_id)
                options.append((key, a, dist))
            if not options:
                if not capable:
                    reasons[cand.entity_id] = "no asset advertises Investigate"
                elif not healthy:
                    reasons[cand.entity_id] = (
                        f"all {len(capable)} Investigate-capable assets offline or failed"
                    )
                elif not free:
                    reasons[cand.entity_id] = (
                        f"all {len(healthy)} available Investigate-capable assets at capacity"
                    )
                else:
                    reasons[cand.entity_id] = (
                        f"no free Investigate-capable asset within {self.policy.max_range_m:.0f} m"
                    )
                continue
            _, asset, dist = min(options, key=lambda o: o[0])
            busy.setdefault(asset.entity_id, []).append(f"planned:{cand.entity_id}")
            assignments.append(
                Assignment(
                    candidate_id=cand.entity_id,
                    candidate_name=cand.name,
                    asset_id=asset.entity_id,
                    asset_name=asset.name,
                    distance_m=round(dist, 1) if dist is not None else None,
                    priority=act.ranked.priority,
                    attempt=act.attempt,
                )
            )
            where = f"{dist / 1000:.1f} km" if dist is not None else "no location (remote asset)"
            retry = f", retry {act.attempt - 1}" if act.attempt > 1 else ""
            reasons[cand.entity_id] = (
                f"assigned {asset.entity_id} ({where}), priority {act.ranked.priority:.3f}{retry}"
            )
        return assignments, reasons


# --------------------------------------------------------------------------- plan


def effective_view(
    snapshot: Snapshot, state: PlannerState, policy: Policy
) -> tuple[Snapshot, PlannerState, dict[str, str]]:
    """What the agents plan against: planner tasks past `policy.task_timeout_s` count as
    failed attempts (on a copy of state), and Lattice open tasks the planner has already
    closed locally (timed out, e.g. still CANCEL_REQUESTED for a dead assignee) no longer
    block their candidate or asset. Pure: inputs are not modified."""
    timeouts = stale_task_ids(state, snapshot.now, policy.task_timeout_s)
    if timeouts:
        state = copy.deepcopy(state)
        for tid, reason in timeouts.items():
            apply_timeout(state, tid, snapshot.now, reason)
    closed = {tid for tid, rec in state.tasks.items() if is_terminal(rec.status)}
    open_tasks = tuple(t for t in snapshot.open_tasks if t.task_id not in closed)
    if open_tasks != snapshot.open_tasks:
        snapshot = replace(snapshot, open_tasks=open_tasks)
    return snapshot, state, timeouts


def plan(snapshot: Snapshot, state: PlannerState, policy: Policy | None = None) -> Plan:
    """Timeouts, then Prioritizer -> Deconflictor -> AssetMatcher over one snapshot. Pure."""
    policy = policy or Policy()
    snapshot, state, timeouts = effective_view(snapshot, state, policy)
    ranked = Prioritizer().rank(snapshot)
    actionable, reasons = Deconflictor(policy).screen(ranked, snapshot, state)
    assignments, match_reasons = AssetMatcher(policy).match(actionable, snapshot, state)
    reasons.update(match_reasons)
    assigned = {a.candidate_id for a in assignments}
    deferred = tuple(r.candidate.entity_id for r in ranked if r.candidate.entity_id not in assigned)
    return Plan(tuple(assignments), deferred, reasons, timeouts)
