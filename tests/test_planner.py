"""Recon Planner tests.

Candidate positions are real NOAA-20 VIIRS detections inside LA County from the captured
FIRMS fixture (tests/fixtures/feeds/firms-viirs-noaa20-ca-20261109.csv). Assets are test
doubles placed at known offsets so distances are exact enough to assert ordering.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from anduril import (
    Aliases,
    Entity,
    GoogleProtobufAny,
    Health,
    Location,
    MilView,
    Ontology,
    Position,
    Principal,
    Provenance,
    Relations,
    System,
    Task,
    TaskCatalog,
    TaskDefinition,
    TaskStatus,
    TaskVersion,
    Tracked,
)

from lattice_wildfire.core.config import AOI
from lattice_wildfire.planner import (
    Asset,
    Candidate,
    OpenTask,
    PlannerState,
    Policy,
    Prioritizer,
    Site,
    Snapshot,
    Zone,
    apply_status,
    apply_timeout,
    build_snapshot,
    build_task_request,
    capacity_from_description,
    capacity_of,
    plan,
)
from lattice_wildfire.planner.__main__ import parse_args as planner_args
from lattice_wildfire.planner.execute import Monitor, Timeline, load_state, save_state
from lattice_wildfire.planner.model import (
    INTEGRATION_NAME,
    INVESTIGATE_URL,
    STATUS_CANCELLED,
    CandidateHistory,
    TaskRecord,
    apply_candidate_gone,
    candidate_is_gone,
)
from lattice_wildfire.planner.runner import run_cycle

FIRMS = Path(__file__).parent / "fixtures" / "feeds" / "firms-viirs-noaa20-ca-20261109.csv"
LA = AOI(min_lat=33.70, min_lon=-118.95, max_lat=34.82, max_lon=-117.65)
ORBIT_URL = "type.googleapis.com/anduril.sample_app_auto_reconnaissance.v1.Orbit"
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)


def la_hotspots() -> list[dict]:
    with FIRMS.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        lat, lon = float(r["latitude"]), float(r["longitude"])
        if LA.min_lat <= lat <= LA.max_lat and LA.min_lon <= lon <= LA.max_lon:
            t = datetime.strptime(r["acq_date"] + r["acq_time"], "%Y-%m-%d%H%M").replace(tzinfo=UTC)
            out.append({"lat": lat, "lon": lon, "time": t, "frp": float(r["frp"])})
    return out


HOT = la_hotspots()
# HOT[0] and HOT[1] are ~100 m apart; FAR is the detection farthest from HOT[0].
FAR = max(
    range(len(HOT)), key=lambda i: abs(HOT[i]["lat"] - HOT[0]["lat"]) + abs(HOT[i]["lon"] - HOT[0]["lon"])
)


def _deg(i: int, j: int) -> float:
    return abs(HOT[i]["lat"] - HOT[j]["lat"]) + abs(HOT[i]["lon"] - HOT[j]["lon"])


# MID is the detection farthest from both HOT[0] and HOT[FAR] (a third isolated e2e site).
MID = max(range(len(HOT)), key=lambda i: min(_deg(i, 0), _deg(i, FAR)))


def cand(i: int, score: float = 0.6, name: str | None = None) -> Candidate:
    h = HOT[i]
    return Candidate(
        entity_id=f"lw-cand-test-{i}",
        name=name or f"candidate {i}",
        lat=h["lat"],
        lon=h["lon"],
        score=score,
        observed_at=h["time"],
    )


def asset(
    eid: str,
    near: Candidate | None,
    dlat: float = 0.0,
    *,
    types=(INVESTIGATE_URL,),
    connection="CONNECTION_STATUS_ONLINE",
    health="HEALTH_STATUS_HEALTHY",
    capacity: int | None = None,
) -> Asset:
    return Asset(
        entity_id=eid,
        name=eid,
        lat=None if near is None else near.lat + dlat,
        lon=None if near is None else near.lon,
        task_types=frozenset(types),
        connection_status=connection,
        health_status=health,
        capacity=capacity,
    )


def snap(candidates=(), assets=(), **kw) -> Snapshot:
    return Snapshot(now=kw.pop("now", NOW), candidates=tuple(candidates), assets=tuple(assets), **kw)


def record(state: PlannerState, task_id: str, a, status="STATUS_SENT", attempt=1) -> None:
    """What Dispatcher.dispatch does after create_task succeeds."""
    state.tasks[task_id] = TaskRecord(
        task_id, a.candidate_id, a.asset_id, attempt, status, NOW.isoformat(), NOW.isoformat()
    )
    state.history.setdefault(a.candidate_id, CandidateHistory()).attempts += 1


def test_fixture_has_la_hotspots():
    assert len(HOT) >= 3


# --------------------------------------------------------------------------- AssetMatcher


def test_nearest_capable_asset_wins():
    c = cand(0)
    assets = [
        asset("engine-far", c, 0.20),  # ~22 km
        asset("engine-near", c, 0.02),  # ~2.2 km
        asset("station-closest", c, 0.001, types=()),  # closer but not taskable for Investigate
    ]
    p = plan(snap([c], assets), PlannerState())
    assert [(a.candidate_id, a.asset_id) for a in p.assignments] == [(c.entity_id, "engine-near")]
    assert 2000 < p.assignments[0].distance_m < 2400
    assert p.deferred == ()
    assert "engine-near" in p.reasons[c.entity_id]


def test_asset_without_investigate_in_catalog_never_assigned():
    c = cand(0)
    assets = [
        asset("station", c, 0.0, types=()),
        asset("orbiter", c, 0.001, types=(ORBIT_URL,)),
    ]
    p = plan(snap([c], assets), PlannerState())
    assert p.assignments == ()
    assert p.deferred == (c.entity_id,)
    assert p.reasons[c.entity_id] == "no asset advertises Investigate"


def test_offline_or_failed_asset_not_assigned():
    c = cand(0)
    assets = [
        asset("offline", c, 0.001, connection="CONNECTION_STATUS_OFFLINE"),
        asset("failed", c, 0.002, health="HEALTH_STATUS_FAIL"),
        asset("ok", c, 0.3),
    ]
    p = plan(snap([c], assets), PlannerState())
    assert [a.asset_id for a in p.assignments] == ["ok"]
    p2 = plan(snap([c], assets[:2]), PlannerState())
    assert p2.reasons[c.entity_id] == "all 2 Investigate-capable assets offline or failed"


def test_unlocated_asset_is_eligible_but_ranks_after_located():
    c = cand(0)
    analyst = asset("lw-analyst-host", None)
    p = plan(snap([c], [analyst, asset("engine", c, 0.3)]), PlannerState())
    assert p.assignments[0].asset_id == "engine"
    p2 = plan(snap([c], [analyst]), PlannerState())
    assert p2.assignments[0].asset_id == "lw-analyst-host"
    assert p2.assignments[0].distance_m is None


def test_max_range_excludes_far_located_assets():
    c = cand(0)
    p = plan(snap([c], [asset("far", c, 0.2)]), PlannerState(), Policy(max_range_m=10_000))
    assert p.assignments == ()
    assert p.reasons[c.entity_id] == "no free Investigate-capable asset within 10000 m"


def test_busy_asset_not_double_booked_within_one_plan():
    hi, lo = cand(0, score=0.9), cand(1, score=0.3)
    only = asset("engine", hi, 0.01)
    p = plan(snap([lo, hi], [only]), PlannerState())
    assert [(a.candidate_id, a.asset_id) for a in p.assignments] == [(hi.entity_id, "engine")]
    assert p.deferred == (lo.entity_id,)
    assert p.reasons[lo.entity_id] == "all 1 available Investigate-capable assets at capacity"


def test_busy_asset_not_double_booked_across_cycles():
    a_c, b_c = cand(0), cand(1)
    engine = asset("engine", a_c, 0.01)
    state = PlannerState()
    p1 = plan(snap([a_c], [engine]), state)
    record(state, "task-1", p1.assignments[0])
    # A new candidate appears while engine is still executing task-1.
    p2 = plan(snap([a_c, b_c], [engine]), state)
    assert p2.assignments == ()
    assert p2.reasons[b_c.entity_id] == "all 1 available Investigate-capable assets at capacity"
    # Engine also counts as busy when the open task came from someone else (seen in Lattice).
    p3 = plan(
        snap([b_c], [engine], open_tasks=(OpenTask("ext-1", "other-target", "engine", "STATUS_EXECUTING"),)),
        PlannerState(),
    )
    assert p3.assignments == ()
    # Once task-1 is terminal the engine is free again.
    apply_status(state, "task-1", "STATUS_DONE_OK", NOW)
    p4 = plan(snap([a_c, b_c], [engine]), state)
    assert [a.candidate_id for a in p4.assignments] == [b_c.entity_id]


# --------------------------------------------------------------------------- capacity


def test_capacity_three_asset_takes_three_candidates_in_one_cycle():
    cs = [cand(0, score=0.9), cand(1, score=0.8), cand(2, score=0.7), cand(3, score=0.6)]
    wide = asset("wide", cs[0], 0.01, capacity=3)
    p = plan(snap(cs, [wide]), PlannerState())
    assert [a.candidate_id for a in p.assignments] == [c.entity_id for c in cs[:3]]
    assert {a.asset_id for a in p.assignments} == {"wide"}
    assert p.deferred == (cs[3].entity_id,)
    assert p.reasons[cs[3].entity_id] == "all 1 available Investigate-capable assets at capacity"


def test_default_capacity_one_is_unchanged():
    cs = [cand(0, score=0.9), cand(1, score=0.8)]
    only = asset("engine", cs[0], 0.01)
    p = plan(snap(cs, [only]), PlannerState())
    assert [a.candidate_id for a in p.assignments] == [cs[0].entity_id]
    assert p.deferred == (cs[1].entity_id,)


def test_capacity_is_spent_by_open_tasks_and_counts_each_task_once():
    c = cand(0)
    wide = asset("wide", c, 0.01, capacity=2)
    state = PlannerState()
    p1 = plan(snap([c], [wide]), state)
    record(state, "t1", p1.assignments[0])
    # The same task shows up in planner state and in Lattice: one slot, not two.
    p2 = plan(
        snap([cand(1)], [wide], open_tasks=(OpenTask("t1", c.entity_id, "wide", "STATUS_EXECUTING"),)),
        state,
    )
    assert [a.candidate_id for a in p2.assignments] == [cand(1).entity_id]
    record(state, "t2", p2.assignments[0])
    # Both slots are taken now.
    p3 = plan(snap([cand(2)], [wide]), state)
    assert p3.assignments == ()
    assert p3.reasons[cand(2).entity_id] == "all 1 available Investigate-capable assets at capacity"


def test_policy_override_beats_entity_capacity_which_beats_default():
    cs = [cand(0, score=0.9), cand(1, score=0.8), cand(2, score=0.7)]
    e = asset("e", cs[0], 0.01, capacity=2)
    assert len(plan(snap(cs, [e]), PlannerState(), Policy(asset_capacity={"e": 1})).assignments) == 1
    assert len(plan(snap(cs, [e]), PlannerState(), Policy(asset_capacity={"e": 3})).assignments) == 3
    assert len(plan(snap(cs, [e]), PlannerState(), Policy(default_capacity=3)).assignments) == 2
    assert (
        len(
            plan(
                snap(cs, [asset("plain", cs[0], 0.01)]), PlannerState(), Policy(default_capacity=3)
            ).assignments
        )
        == 3
    )
    assert capacity_of(e, Policy(asset_capacity={"e": 0})) == 1  # never below one


def test_cli_state_defaults_follow_lw_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path))
    args = planner_args([])
    assert args.state == tmp_path / "planner" / "state.json"
    assert args.timeline == tmp_path / "planner" / "timeline.jsonl"
    monkeypatch.delenv("LW_STATE_DIR")
    args = planner_args([])
    assert args.state == Path("var/planner/state.json")
    assert args.timeline == Path("var/planner/timeline.jsonl")
    assert planner_args(["--state", "x.json"]).state == Path("x.json")


def test_capacity_from_description():
    assert capacity_from_description("Fire engine, capacity=3") == 3
    assert capacity_from_description("AI analyst. CAPACITY: 2 concurrent threads") == 2
    assert capacity_from_description("no declaration") is None
    assert capacity_from_description(None) is None
    assert capacity_from_description("capacity=0") is None


def test_cli_parses_capacity_overrides():
    args = planner_args(["--dry-run", "--capacity", "lw-analyst-box=2", "--capacity", "engine-1=3"])
    assert args.capacity == [("lw-analyst-box", 2), ("engine-1", 3)]
    assert args.default_capacity == 1
    with pytest.raises(SystemExit):
        planner_args(["--capacity", "lw-analyst-box"])  # missing =N
    with pytest.raises(SystemExit):
        planner_args(["--capacity", "engine=0"])


# --------------------------------------------------------------------------- Deconflictor


def test_one_open_task_per_candidate():
    c = cand(0)
    e1, e2 = asset("e1", c, 0.01), asset("e2", c, 0.02)
    state = PlannerState()
    p1 = plan(snap([c], [e1, e2]), state)
    assert len(p1.assignments) == 1
    record(state, "task-1", p1.assignments[0])
    # A second free asset exists, but the candidate already has an open task.
    p2 = plan(snap([c], [e1, e2]), state)
    assert p2.assignments == ()
    assert p2.reasons[c.entity_id] == "open task task-1 (STATUS_SENT)"
    # Same rule when the open task is only known from Lattice (e.g. state file lost).
    ext = OpenTask("ext-9", c.entity_id, "e1", "STATUS_EXECUTING")
    p3 = plan(snap([c], [e1, e2], open_tasks=(ext,)), PlannerState())
    assert p3.assignments == ()
    assert p3.reasons[c.entity_id] == "open task ext-9 (STATUS_EXECUTING)"


def test_cooldown_after_done_ok():
    c = cand(0)
    e1 = asset("e1", c, 0.01)
    state = PlannerState()
    record(state, "t1", plan(snap([c], [e1]), state).assignments[0])
    apply_status(state, "t1", "STATUS_DONE_OK", NOW)
    policy = Policy(cooldown_s=600)
    p = plan(snap([c], [e1], now=NOW + timedelta(seconds=599)), state, policy)
    assert p.assignments == ()
    assert p.reasons[c.entity_id].startswith("cooldown after DONE_OK until ")
    p2 = plan(snap([c], [e1], now=NOW + timedelta(seconds=600)), state, policy)
    assert [a.attempt for a in p2.assignments] == [2]


def test_done_not_ok_retries_up_to_two_then_defers_with_reason():
    c = cand(0)
    e1, e2 = asset("e1", c, 0.01), asset("e2", c, 0.05)
    state = PlannerState()
    policy = Policy(max_retries=2, retry_backoff_s=60)
    now = NOW
    used = []
    for attempt in (1, 2, 3):
        p = plan(snap([c], [e1, e2], now=now), state, policy)
        assert len(p.assignments) == 1, p.reasons
        a = p.assignments[0]
        assert a.attempt == attempt
        used.append(a.asset_id)
        record(state, f"t{attempt}", a, attempt=attempt)
        change = apply_status(
            state, f"t{attempt}", "STATUS_DONE_NOT_OK", now, "ERROR_CODE_FAILED: sensor fault"
        )
        assert change["terminal"] is True
        # Inside the backoff window nothing is re-tasked.
        held = plan(snap([c], [e1, e2], now=now + timedelta(seconds=59)), state, policy)
        if attempt < 3:
            assert held.reasons[c.entity_id].startswith("retry backoff until ")
        now += timedelta(seconds=61)
    # Retry moved off the asset that failed first.
    assert used[:2] == ["e1", "e2"]
    final = plan(snap([c], [e1, e2], now=now + timedelta(days=1)), state, policy)
    assert final.assignments == ()
    assert final.deferred == (c.entity_id,)
    assert final.reasons[c.entity_id] == (
        "retries exhausted: 3 DONE_NOT_OK (max 2 retries); last error: ERROR_CODE_FAILED: sensor fault"
    )
    assert state.history[c.entity_id].attempts == 3


def test_task_timeout_is_planned_purely_and_counts_as_failed_attempt():
    c = cand(0)
    e1, e2 = asset("e1", c, 0.01), asset("e2", c, 0.05)
    state = PlannerState()
    record(state, "t1", plan(snap([c], [e1, e2]), state).assignments[0], status="STATUS_SENT")
    policy = Policy(task_timeout_s=900, retry_backoff_s=0)

    # Fresh: 899 s after creation nothing times out and the candidate stays blocked.
    fresh = plan(snap([c], [e1, e2], now=NOW + timedelta(seconds=899)), state, policy)
    assert fresh.timeouts == {}
    assert fresh.assignments == ()
    assert fresh.reasons[c.entity_id] == "open task t1 (STATUS_SENT)"

    # A status change restarts the clock.
    moved = PlannerState.from_dict(state.to_dict())
    apply_status(moved, "t1", "STATUS_EXECUTING", NOW + timedelta(seconds=800))
    assert plan(snap([c], [e1, e2], now=NOW + timedelta(seconds=1000)), moved, policy).timeouts == {}

    # Stale: at 900 s the task times out and the candidate is re-planned to the other asset.
    before = state.to_dict()
    stale = plan(snap([c], [e1, e2], now=NOW + timedelta(seconds=900)), state, policy)
    assert stale.timeouts == {"t1": "timeout: STATUS_SENT for 900 s without a terminal status"}
    assert [(a.asset_id, a.attempt) for a in stale.assignments] == [("e2", 2)]
    assert state.to_dict() == before, "plan() must not mutate state"

    # Executor folds the timeout in: one failed attempt, record closed locally.
    apply_timeout(state, "t1", NOW + timedelta(seconds=900), stale.timeouts["t1"])
    assert state.tasks["t1"].timed_out is True
    assert state.history[c.entity_id].failures == 1
    assert state.history[c.entity_id].failed_assets == ["e1"]
    # Lattice may keep showing the cancelled task (dead assignee never answers): no longer blocks.
    lingering = OpenTask("t1", c.entity_id, "e1", "STATUS_CANCEL_REQUESTED")
    later = snap([c], [e1, e2], now=NOW + timedelta(seconds=901), open_tasks=(lingering,))
    assert [a.asset_id for a in plan(later, state, policy).assignments] == ["e2"]
    # It counts toward the retry budget.
    exhausted = plan(later, state, Policy(max_retries=0, retry_backoff_s=0))
    assert exhausted.assignments == ()
    assert exhausted.reasons[c.entity_id].startswith("retries exhausted: 1 DONE_NOT_OK (max 0 retries)")


def test_task_timeout_disabled():
    c = cand(0)
    state = PlannerState()
    record(state, "t1", plan(snap([c], [asset("e1", c, 0.01)]), state).assignments[0])
    p = plan(
        snap([c], [asset("e1", c, 0.01)], now=NOW + timedelta(days=2)), state, Policy(task_timeout_s=None)
    )
    assert p.timeouts == {}
    assert p.reasons[c.entity_id] == "open task t1 (STATUS_SENT)"


def test_done_ok_resets_retry_budget():
    state = PlannerState()
    state.tasks["t1"] = TaskRecord("t1", "c", "e1", 1, "STATUS_EXECUTING", NOW.isoformat(), NOW.isoformat())
    state.history["c"] = CandidateHistory(attempts=1, failures=2, failed_assets=["e9"])
    apply_status(state, "t1", "STATUS_DONE_OK", NOW)
    h = state.history["c"]
    assert (h.failures, h.failed_assets, h.last_status) == (0, [], "STATUS_DONE_OK")


def test_apply_status_ignores_updates_after_terminal_and_repeats():
    state = PlannerState()
    state.tasks["t1"] = TaskRecord("t1", "c", "e1", 1, "STATUS_SENT", NOW.isoformat(), NOW.isoformat())
    assert apply_status(state, "t1", "STATUS_SENT", NOW) is None
    assert apply_status(state, "t1", "STATUS_EXECUTING", NOW)["prev_status"] == "STATUS_SENT"
    assert apply_status(state, "t1", "STATUS_DONE_OK", NOW)["terminal"] is True
    assert apply_status(state, "t1", "STATUS_DONE_NOT_OK", NOW) is None
    assert state.tasks["t1"].status == "STATUS_DONE_OK"
    assert state.history["c"].failures == 0
    assert apply_status(state, "unknown", "STATUS_DONE_OK", NOW) is None


# --------------------------------------------------------------------------- Prioritizer


def test_prioritizer_factors_and_order():
    a, b = cand(0, score=0.5), cand(FAR, score=0.5)
    # A red flag polygon: a box around candidate b only.
    box = (
        (b.lat - 0.05, b.lon - 0.05),
        (b.lat - 0.05, b.lon + 0.05),
        (b.lat + 0.05, b.lon + 0.05),
        (b.lat + 0.05, b.lon - 0.05),
    )
    station = Site("lw-station-1", "Station 1", a.lat + 0.03, a.lon)  # ~3.3 km from a
    s = snap([a, b], red_flags=(Zone("lw-nws-x", "Red Flag", box),), stations=(station,))
    pr = Prioritizer()
    fa, fb = pr.factors(s, a), pr.factors(s, b)
    assert (fa["red_flag"], fa["exposure"]) == (0.0, 1.0)
    assert (fb["red_flag"], fb["exposure"]) == (1.0, 0.0)
    # Station between 5 and 10 km -> half exposure.
    far = snap([a], stations=(Site("s", "s", a.lat + 0.07, a.lon),))
    assert pr.factors(far, a)["exposure"] == 0.5
    # Recency decays to zero over 24 h.
    old = snap([a], now=a.observed_at + timedelta(hours=30))
    assert pr.factors(old, a)["recency"] == 0.0
    assert pr.factors(snap([a], now=a.observed_at), a)["recency"] == 1.0
    ranked = pr.rank(snap([cand(0, score=0.2), cand(1, score=0.9)]))
    assert [r.candidate.entity_id for r in ranked] == ["lw-cand-test-1", "lw-cand-test-0"]


def test_missing_score_ranks_below_scored():
    unscored = Candidate("lw-cand-u", "u", HOT[0]["lat"], HOT[0]["lon"], None, HOT[0]["time"])
    ranked = Prioritizer().rank(snap([unscored, cand(1, score=0.4)]))
    assert ranked[0].candidate.entity_id == "lw-cand-test-1"
    assert ranked[1].factors["score"] == 0.0


# --------------------------------------------------------------------------- snapshot + dispatch request


def _entity(eid, data_type, template, lat=None, lon=None, **kw) -> Entity:
    return Entity(
        entity_id=eid,
        is_live=True,
        expiry_time=kw.pop("expiry", NOW + timedelta(minutes=10)),
        aliases=Aliases(name=kw.pop("name", eid)),
        ontology=Ontology(template=template, platform_type=kw.pop("platform", "x")),
        provenance=Provenance(integration_name="t", data_type=data_type, source_update_time=NOW),
        location=None
        if lat is None
        else Location(position=Position(latitude_degrees=lat, longitude_degrees=lon)),
        **kw,
    )


def _task(tid, target, assignee, status, url=INVESTIGATE_URL) -> Task:
    return Task(
        version=TaskVersion(task_id=tid),
        specification=GoogleProtobufAny(type=url, objective={"entity_id": target}),
        relations=Relations(assignee=Principal(system=System(entity_id=assignee))),
        status=TaskStatus(status=status),
    )


def test_build_snapshot_classifies_entities_and_tasks():
    h0, h1 = HOT[0], HOT[1]
    entities = [
        _entity(
            "lw-cand-a",
            "ember-candidate",
            "TEMPLATE_TRACK",
            h0["lat"],
            h0["lon"],
            tracked=Tracked(track_quality_wrapper=12),
        ),
        _entity(
            "lw-cand-outside", "ember-candidate", "TEMPLATE_TRACK", 39.62141, -119.2661
        ),  # Nevada row of the fixture
        _entity(
            "lw-cand-expired",
            "ember-candidate",
            "TEMPLATE_TRACK",
            h1["lat"],
            h1["lon"],
            expiry=NOW - timedelta(seconds=1),
        ),
        _entity("lw-firms-x", "firms-viirs-hotspot", "TEMPLATE_TRACK", h1["lat"], h1["lon"]),
        _entity(
            "lw-analyst-box",
            "kodros-analyst",
            "TEMPLATE_ASSET",
            task_catalog=TaskCatalog(
                task_definitions=[TaskDefinition(task_specification_url=INVESTIGATE_URL)]
            ),
            health=Health(
                connection_status="CONNECTION_STATUS_ONLINE", health_status="HEALTH_STATUS_HEALTHY"
            ),
        ),
        _entity("lw-station-11", "lacofd-station", "TEMPLATE_ASSET", h1["lat"], h1["lon"], name="Station 11"),
    ]
    tasks = [
        _task("t-open", "lw-cand-a", "lw-analyst-box", "STATUS_EXECUTING"),
        _task("t-done", "lw-cand-a", "lw-analyst-box", "STATUS_DONE_OK"),
        _task("t-orbit", "lw-cand-a", "uav", "STATUS_EXECUTING", url=ORBIT_URL),
    ]
    s = build_snapshot(entities, tasks, NOW, LA)
    assert [c.entity_id for c in s.candidates] == ["lw-cand-a"]
    assert s.candidates[0].score == pytest.approx(0.8)
    assert {a.entity_id: a.task_types for a in s.assets} == {
        "lw-analyst-box": frozenset({INVESTIGATE_URL}),
        "lw-station-11": frozenset(),
    }
    assert [st.name for st in s.stations] == ["Station 11"]
    assert s.open_tasks == (OpenTask("t-open", "lw-cand-a", "lw-analyst-box", "STATUS_EXECUTING"),)


def test_candidate_is_gone_when_missing_expired_or_retired():
    h = HOT[0]
    entities = [
        _entity(
            "lw-cand-live",
            "ember-candidate",
            "TEMPLATE_TRACK",
            h["lat"],
            h["lon"],
            tracked=Tracked(track_quality_wrapper=12),
        ),
        _entity(
            "lw-cand-expired",
            "ember-candidate",
            "TEMPLATE_TRACK",
            h["lat"],
            h["lon"],
            expiry=NOW - timedelta(seconds=1),
        ),
        _entity(
            "lw-cand-retired",
            "ember-candidate",
            "TEMPLATE_TRACK",
            h["lat"],
            h["lon"],
            description="retired: duplicate of Peavine",
            tracked=Tracked(track_quality_wrapper=12),
        ),
    ]
    s = build_snapshot(entities, [], NOW, LA)
    # Expired entities are dropped from the snapshot map; a retired one stays and is filtered out.
    assert "lw-cand-live" in s.entities and "lw-cand-retired" in s.entities
    assert "lw-cand-expired" not in s.entities
    assert [c.entity_id for c in s.candidates] == ["lw-cand-live"]
    assert not candidate_is_gone(s.entities, "lw-cand-live")
    assert candidate_is_gone(s.entities, "lw-cand-missing")
    assert candidate_is_gone(s.entities, "lw-cand-expired")
    assert candidate_is_gone(s.entities, "lw-cand-retired")


def test_apply_candidate_gone_is_terminal_and_not_a_failed_attempt():
    state = PlannerState()
    state.tasks["t1"] = TaskRecord("t1", "c1", "e1", 1, "STATUS_SENT", NOW.isoformat(), NOW.isoformat())
    change = apply_candidate_gone(state, "t1", NOW, "candidate c1 is no longer live")
    assert change is not None
    assert (change["status"], change["prev_status"], change["terminal"]) == (
        STATUS_CANCELLED,
        "STATUS_SENT",
        True,
    )
    assert state.tasks["t1"].status == STATUS_CANCELLED
    assert state.open_records() == []
    assert state.history == {}, "a gone candidate is not a failed attempt"
    assert apply_candidate_gone(state, "t1", NOW, "again") is None


def test_build_snapshot_reads_capacity_from_asset_description():
    catalog = TaskCatalog(task_definitions=[TaskDefinition(task_specification_url=INVESTIGATE_URL)])
    entities = [
        _entity(
            "engine-1",
            None,
            "TEMPLATE_ASSET",
            HOT[0]["lat"],
            HOT[0]["lon"],
            description="Type 1 engine. capacity=3",
            task_catalog=catalog,
        ),
        _entity("lw-analyst-box", "kodros-analyst", "TEMPLATE_ASSET", task_catalog=catalog),
    ]
    s = build_snapshot(entities, [], NOW, LA)
    by_id = {a.entity_id: a for a in s.assets}
    assert by_id["engine-1"].capacity == 3
    assert by_id["lw-analyst-box"].capacity is None


def test_build_task_request_shape():
    c = cand(0, name="Eaton Canyon candidate")
    p = plan(snap([c], [asset("engine", c, 0.01)]), PlannerState())
    req = build_task_request(p.assignments[0])
    assert req["display_name"] == "Investigate Eaton Canyon candidate"
    assert req["specification"].type == INVESTIGATE_URL
    assert req["specification"].model_extra["objective"] == {"entity_id": c.entity_id}
    assert req["relations"].assignee.system.entity_id == "engine"
    assert req["author"].system.service_name == INTEGRATION_NAME
    assert req["is_executed_elsewhere"] is False
    assert "initial_entities" not in req


def test_state_round_trip(tmp_path):
    state = PlannerState()
    state.tasks["t1"] = TaskRecord("t1", "c", "e", 1, "STATUS_SENT", NOW.isoformat(), NOW.isoformat())
    state.history["c"] = CandidateHistory(attempts=1, failures=1, failed_assets=["e"], last_error="x")
    path = tmp_path / "s" / "state.json"
    save_state(state, path)
    assert load_state(path) == state
    assert load_state(tmp_path / "missing.json") == PlannerState()


# --------------------------------------------------------------------------- end to end on the harness


def _publish_candidate(
    lattice,
    eid: str,
    hot: dict,
    name: str,
    twq: int = 12,
    *,
    description: str | None = None,
    expiry_seconds: int = 600,
    expiry_time: datetime | None = None,
    source_time: datetime | None = None,
) -> None:
    from lattice_wildfire.core.entities import expiry, point_location, provenance, symbology, unclassified

    extra = {"description": description} if description is not None else {}
    lattice.entities.publish_entity(
        entity_id=eid,
        is_live=True,
        expiry_time=expiry_time or expiry(expiry_seconds),
        aliases=Aliases(name=name),
        location=point_location(hot["lat"], hot["lon"]),
        mil_view=MilView(disposition="DISPOSITION_SUSPICIOUS", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_TRACK", platform_type="New Start Candidate"),
        provenance=provenance("fusion", "ember-candidate", source_time or hot["time"]),
        tracked=Tracked(track_quality_wrapper=twq),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_SUSPICIOUS", "ENVIRONMENT_LAND"),
        **extra,
    )


def _publish_asset(lattice, eid: str, lat: float, lon: float, types: tuple[str, ...], data_type: str) -> None:
    from lattice_wildfire.core.entities import expiry, point_location, provenance, symbology, unclassified

    lattice.entities.publish_entity(
        entity_id=eid,
        is_live=True,
        expiry_time=expiry(600),
        aliases=Aliases(name=eid),
        location=point_location(lat, lon),
        mil_view=MilView(disposition="DISPOSITION_FRIENDLY", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_ASSET", platform_type="test asset"),
        provenance=provenance("planner-test", data_type),
        health=Health(connection_status="CONNECTION_STATUS_ONLINE", health_status="HEALTH_STATUS_HEALTHY"),
        task_catalog=TaskCatalog(task_definitions=[TaskDefinition(task_specification_url=u) for u in types]),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND"),
    )


def _agent_status(lattice, task_id: str, agent_id: str, version: int, status: str, error=None) -> None:
    """Play the assignee: the same call the Kodros Analyst (and Anduril's simulated asset) makes."""
    lattice.tasks.update_task_status(
        task_id,
        status_version=version,
        new_status=TaskStatus(status=status, task_error=error),
        author=Principal(system=System(entity_id=agent_id)),
    )


def _local(h: dict) -> tuple[AOI, Policy]:
    """AOI and range limit around one hotspot, so entities other tests left in the harness
    (other candidates, other assets) cannot leak into this test's plan."""
    d = 0.05
    return AOI(
        min_lat=h["lat"] - d, min_lon=h["lon"] - d, max_lat=h["lat"] + d, max_lon=h["lon"] + d
    ), Policy(max_range_m=10_000)


def test_end_to_end_plan_dispatch_monitor(lattice, tmp_path):
    from lattice_wildfire.planner.runner import take_snapshot
    from lattice_wildfire.planner.snapshot import objective_entity_id

    h = HOT[0]
    aoi, policy = _local(h)
    cid, agent_id = "lw-cand-e2e-1", "lw-test-investigator"
    _publish_candidate(lattice, cid, h, "Eaton candidate")
    # Closer asset without Investigate (a station) and a farther one that advertises it.
    _publish_asset(lattice, "lw-station-e2e", h["lat"] + 0.001, h["lon"], (), "lacofd-station")
    _publish_asset(lattice, agent_id, h["lat"] + 0.02, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")

    state = PlannerState()
    timeline = Timeline(tmp_path / "timeline.jsonl")
    r1 = run_cycle(lattice, state, timeline, policy, aoi)
    assert [(a.candidate_id, a.asset_id) for a in r1.plan.assignments] == [(cid, agent_id)]
    assert len(r1.created) == 1
    task_id = r1.created[0].task_id

    # Read the task back through the SDK in a separate call.
    task = lattice.tasks.get_task(task_id)
    assert task.display_name == "Investigate Eaton candidate"
    assert task.specification.type == INVESTIGATE_URL
    assert objective_entity_id(task) == cid
    assert task.relations.assignee.system.entity_id == agent_id

    # Second cycle: still open -> no duplicate task. A fresh state (lost state file) also
    # sees the open task in Lattice and does not duplicate it.
    r2 = run_cycle(lattice, state, timeline, policy, aoi)
    assert r2.created == []
    assert r2.plan.reasons[cid].startswith(f"open task {task_id}")
    r2b = plan(take_snapshot(lattice, aoi), PlannerState(), policy)
    assert r2b.assignments == ()
    assert r2b.reasons[cid].startswith(f"open task {task_id}")

    # Agent side: EXECUTING then DONE_OK, via the SDK.
    _agent_status(lattice, task_id, agent_id, 1, "STATUS_EXECUTING")
    changes = Monitor(lattice, timeline).poll(state)
    assert [c["status"] for c in changes] == ["STATUS_EXECUTING"]
    _agent_status(lattice, task_id, agent_id, 2, "STATUS_DONE_OK")
    assert Monitor(lattice, timeline).wait_terminal(state, [task_id], timeout_s=5, interval_s=0.1)
    assert state.tasks[task_id].status == "STATUS_DONE_OK"
    assert state.history[cid].last_status == "STATUS_DONE_OK"

    rows = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
    task_rows = [(r["event"], r.get("status")) for r in rows if r.get("task_id") == task_id]
    assert task_rows[0][0] == "created"
    assert task_rows[-2:] == [("status", "STATUS_EXECUTING"), ("terminal", "STATUS_DONE_OK")]

    # Terminal task frees the asset; cooldown keeps the candidate from being re-tasked.
    r3 = run_cycle(lattice, state, timeline, policy, aoi)
    assert r3.created == []
    assert r3.plan.reasons[cid].startswith("cooldown after DONE_OK")


def test_end_to_end_done_not_ok_retries_then_defers(lattice, tmp_path):
    from anduril import TaskError

    h = HOT[FAR]
    aoi, base = _local(h)
    cid, near_id, far_id = "lw-cand-e2e-2", "lw-test-flaky-near", "lw-test-flaky-far"
    _publish_candidate(lattice, cid, h, "Flaky candidate")
    _publish_asset(lattice, near_id, h["lat"] + 0.02, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
    _publish_asset(lattice, far_id, h["lat"] + 0.05, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
    state = PlannerState()
    timeline = Timeline(tmp_path / "timeline.jsonl")
    policy = Policy(max_retries=2, retry_backoff_s=0, max_range_m=base.max_range_m)
    task_ids, assignees = [], []
    for _ in range(3):
        r = run_cycle(lattice, state, timeline, policy, aoi)
        assert len(r.created) == 1, r.plan.reasons
        tid, who = r.created[0].task_id, r.created[0].asset_id
        task_ids.append(tid)
        assignees.append(who)
        _agent_status(lattice, tid, who, 1, "STATUS_EXECUTING")
        _agent_status(
            lattice,
            tid,
            who,
            2,
            "STATUS_DONE_NOT_OK",
            TaskError(code="ERROR_CODE_FAILED", message="no imagery"),
        )
        assert Monitor(lattice, timeline).wait_terminal(state, [tid], timeout_s=5, interval_s=0.1)
    # Nearest first; the first retry moves off the asset that failed.
    assert assignees[:2] == [near_id, far_id]
    assert len(set(task_ids)) == 3
    assert [state.tasks[t].attempt for t in task_ids] == [1, 2, 3]
    assert [lattice.tasks.get_task(t).status.status for t in task_ids] == ["STATUS_DONE_NOT_OK"] * 3
    final = run_cycle(lattice, state, timeline, policy, aoi)
    assert final.created == []
    assert cid in final.plan.deferred
    assert final.plan.reasons[cid] == (
        "retries exhausted: 3 DONE_NOT_OK (max 2 retries); last error: ERROR_CODE_FAILED: no imagery"
    )


def test_end_to_end_timeout_cancels_stale_task_and_replans(lattice, tmp_path):
    from anduril import EntityIdsSelector

    from lattice_wildfire.core.entities import now_utc

    h = HOT[MID]
    aoi, base = _local(h)
    stale_c, fresh_c = "lw-cand-e2e-3", "lw-cand-e2e-4"
    dead, live, spare = "lw-test-dead", "lw-test-live", "lw-test-spare"
    _publish_candidate(lattice, stale_c, h, "Stale candidate", twq=14)
    _publish_candidate(lattice, fresh_c, {**h, "lon": h["lon"] + 0.01}, "Fresh candidate", twq=8)
    _publish_asset(lattice, dead, h["lat"] + 0.02, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
    _publish_asset(lattice, live, h["lat"] + 0.05, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
    _publish_asset(lattice, spare, h["lat"] + 0.08, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
    state = PlannerState()
    timeline = Timeline(tmp_path / "timeline.jsonl")
    policy = Policy(task_timeout_s=900, retry_backoff_s=0, max_range_m=base.max_range_m)

    r1 = run_cycle(lattice, state, timeline, policy, aoi)
    by_cand = {r.candidate_id: r for r in r1.created}
    assert {c: r.asset_id for c, r in by_cand.items()} == {stale_c: dead, fresh_c: live}
    stale_tid, fresh_tid = by_cand[stale_c].task_id, by_cand[fresh_c].task_id

    # Lattice delivers the task to the dead asset (-> SENT); it never answers.
    req = lattice.tasks.listen_as_agent(agent_selector=EntityIdsSelector(entity_ids=[dead]))
    assert req.execute_request.task.version.task_id == stale_tid
    Monitor(lattice, timeline).poll(state)
    assert state.tasks[stale_tid].status == "STATUS_SENT"
    # 901 s pass with no status change on the stale task; the fresh task changed just now.
    state.tasks[stale_tid].updated_at = (now_utc() - timedelta(seconds=901)).isoformat()

    r2 = run_cycle(lattice, state, timeline, policy, aoi)
    assert list(r2.plan.timeouts) == [stale_tid]
    # cancel_task reached Lattice, authored by the planner, and the dead agent gets the cancel.
    cancelled = lattice.tasks.get_task(stale_tid)
    assert cancelled.status.status == "STATUS_CANCEL_REQUESTED"
    assert cancelled.last_updated_by.system.service_name == INTEGRATION_NAME
    cancel_req = lattice.tasks.listen_as_agent(agent_selector=EntityIdsSelector(entity_ids=[dead]))
    assert cancel_req.cancel_request.task_id == stale_tid
    # Counted as a failed attempt and re-planned to another free asset.
    assert state.tasks[stale_tid].timed_out is True
    assert state.history[stale_c].failures == 1
    assert [(r.candidate_id, r.asset_id, r.attempt) for r in r2.created] == [(stale_c, spare, 2)]
    # The fresh task was not cancelled.
    assert fresh_tid not in r2.plan.timeouts
    assert lattice.tasks.get_task(fresh_tid).status.status == "STATUS_CREATED"
    assert state.tasks[fresh_tid].timed_out is False

    rows = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
    timeouts = [r for r in rows if r["event"] == "timeout"]
    assert [(r["task_id"], r["asset_id"], r["lattice_status"]) for r in timeouts] == [
        (stale_tid, dead, "STATUS_CANCEL_REQUESTED")
    ]

    # Next cycle: the lingering CANCEL_REQUESTED task does not block; nothing is duplicated.
    r3 = run_cycle(lattice, state, timeline, policy, aoi)
    assert r3.created == [] and r3.plan.timeouts == {}
    assert r3.plan.reasons[stale_c].startswith(f"open task {r2.created[0].task_id}")


def test_expired_candidate_cancels_its_task_without_a_failed_attempt(tmp_path):
    """A candidate that expires frees its task and asset, and is not retried.

    An isolated harness keeps every entity in the snapshot this test's own: the shared session
    harness accumulates candidates from other tests, which made this test order-dependent.
    """
    from lattice_wildfire.core.client import make_client
    from lattice_wildfire.core.config import LatticeSettings
    from lattice_wildfire.harness import start_harness

    h = HOT[3]
    d = 0.002
    aoi = AOI(min_lat=h["lat"] - d, min_lon=h["lon"] - d, max_lat=h["lat"] + d, max_lon=h["lon"] + d)
    policy = Policy(max_range_m=10_000)
    cid, agent_id = "lw-cand-expiring", "lw-test-expiring-asset"
    harness = start_harness(0, long_poll_timeout_s=2.0)
    try:
        lattice = make_client(
            LatticeSettings(
                base_url=harness.base_url,
                environment_token="test-token",
                sandboxes_token=None,
                client_id=None,
                client_secret=None,
            ),
            timeout=30,
        )
        _publish_candidate(lattice, cid, h, "Expiring candidate")
        _publish_asset(lattice, agent_id, h["lat"] + 0.02, h["lon"], (INVESTIGATE_URL,), "planner-test-asset")
        state = PlannerState()
        timeline = Timeline(tmp_path / "timeline.jsonl")

        r1 = run_cycle(lattice, state, timeline, policy, aoi)
        assert [(a.candidate_id, a.asset_id) for a in r1.plan.assignments] == [(cid, agent_id)]
        (created,) = r1.created
        task_id = created.task_id
        assert state.tasks[task_id].status == "STATUS_CREATED"

        # Shorten the candidate's expiry, then wait for it to pass (bounded), not a fixed sleep.
        expires_at = datetime.now(UTC) + timedelta(seconds=2)
        _publish_candidate(
            lattice, cid, h, "Expiring candidate", expiry_time=expires_at, source_time=datetime.now(UTC)
        )
        deadline = time.monotonic() + 60.0
        while datetime.now(UTC) < expires_at + timedelta(milliseconds=100):
            assert time.monotonic() < deadline, "candidate expiry did not pass in 60 s"
            time.sleep(0.05)

        r2 = run_cycle(lattice, state, timeline, policy, aoi)
        assert r2.created == []
        assert cid not in r2.plan.reasons, "an expired candidate is not planned"
        assert state.tasks[task_id].status == STATUS_CANCELLED
        assert state.tasks[task_id].timed_out is False
        assert state.history.get(cid, CandidateHistory()).failures == 0
        cancelled = lattice.tasks.get_task(task_id)
        # Harness: an undelivered task becomes DONE_NOT_OK, a delivered one CANCEL_REQUESTED.
        assert cancelled.status.status in ("STATUS_DONE_NOT_OK", "STATUS_CANCEL_REQUESTED")
        assert cancelled.last_updated_by.system.service_name == INTEGRATION_NAME

        rows = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
        gone = [row for row in rows if row["event"] == "candidate_gone"]
        assert len(gone) == 1
        assert (gone[0]["task_id"], gone[0]["candidate_id"], gone[0]["status"]) == (
            task_id,
            cid,
            STATUS_CANCELLED,
        )
        assert gone[0]["lattice_status"] == cancelled.status.status

        # Another cycle neither re-cancels nor retries the gone candidate.
        r3 = run_cycle(lattice, state, timeline, policy, aoi)
        assert r3.created == []
        rows = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
        assert len([row for row in rows if row["event"] == "candidate_gone"]) == 1
    finally:
        harness.stop()
