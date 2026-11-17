"""One planner cycle: monitor open tasks, snapshot Lattice, plan, dispatch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anduril import Lattice

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import now_utc

from .agents import is_capable
from .agents import plan as make_plan
from .execute import Dispatcher, Monitor, Timeline
from .model import Plan, PlannerState, Policy, Snapshot, TaskRecord
from .snapshot import build_snapshot, fetch_entities, fetch_open_tasks

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    snapshot: Snapshot
    plan: Plan
    created: list[TaskRecord] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "candidates": len(self.snapshot.candidates),
            "assets": len(self.snapshot.assets),
            "investigate_capable": sum(1 for a in self.snapshot.assets if is_capable(a)),
            "open_tasks_in_lattice": len(self.snapshot.open_tasks),
            "status_changes": self.changes,
            "plan": self.plan.to_dict(),
            "created": [r.task_id for r in self.created],
        }


def take_snapshot(client: Lattice, aoi: AOI | None) -> Snapshot:
    return build_snapshot(fetch_entities(client), fetch_open_tasks(client), now_utc(), aoi)


def run_cycle(
    client: Lattice,
    state: PlannerState,
    timeline: Timeline,
    policy: Policy | None = None,
    aoi: AOI | None = None,
    dry_run: bool = False,
) -> CycleResult:
    """Monitor open tasks, then snapshot, plan and (unless dry_run) dispatch.

    Dry run reads Lattice only: no status bookkeeping, no tasks, no timeline rows.
    """
    changes = [] if dry_run else Monitor(client, timeline).poll(state)
    snapshot = take_snapshot(client, aoi)
    dispatcher = Dispatcher(client, timeline)
    if not dry_run:
        # A candidate that expired or retired frees its task and its asset before this cycle plans.
        dispatcher.cancel_gone(snapshot, state)
    plan = make_plan(snapshot, state, policy)
    result = CycleResult(snapshot, plan, changes=changes)
    if dry_run:
        return result
    plan_row = {
        "assigned": [a.candidate_id for a in plan.assignments],
        "deferred": {cid: plan.reasons[cid] for cid in plan.deferred},
    }
    if plan.timeouts:
        plan_row["timeouts"] = dict(plan.timeouts)
    # Record a plan row when something is assigned or timed out, or the deferrals changed.
    if plan.assignments or plan.timeouts or plan_row["deferred"] != timeline.last_deferred:
        timeline.record("plan", snapshot.now, **plan_row)
        timeline.last_deferred = plan_row["deferred"]
    result.created = dispatcher.dispatch(plan, snapshot, state)
    return result
