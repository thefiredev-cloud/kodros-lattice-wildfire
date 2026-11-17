"""Ember Fusion tests.

Inputs are real captured feed responses (tests/fixtures/feeds/, 2026-11-09) turned into Lattice
entities by Fireline Feeds' own parsers and builders, so these tests also pin the shared entity
contract between lanes 1 and 2. Where a test needs geometry the captures do not contain (two
detections exactly 900 m apart, a hotspot inside the Timber perimeter, a Red Flag polygon: the
national Red Flag feed was empty at capture), it moves a real detection or draws a polygon and
says so at that spot. Fixed-heat-source tests use the real FIRMS NOAA-20, Suomi NPP and NOAA-21
7-day files captured the same day (tests/fixtures/fusion/), cut to the LA County AOI.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from anduril import Entity
from anduril.core import ApiError

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.core.entities import provenance
from lattice_wildfire.core.geo import haversine_m, point_in_polygon
from lattice_wildfire.core.http import Fetched, FetchError
from lattice_wildfire.feeds import builders, parsers
from lattice_wildfire.feeds.geometry import ranked_parts
from lattice_wildfire.feeds.records import NwsAlert, Perimeter
from lattice_wildfire.fusion import (
    ExposurePerceiver,
    Finding,
    HeatHistory,
    HotspotClusterer,
    IncidentAssociator,
    PersistentHeatPerceiver,
    RedFlagContext,
    SourceFailure,
    build_candidate_entity,
    build_retirement_entity,
    firms_7d_history,
    fuse,
    history_from_csv,
    merged_history,
    picture_from_entities,
    retirements,
    run_perceivers,
    track_quality,
)
from lattice_wildfire.fusion import history as history_module
from lattice_wildfire.fusion.app import (
    CANDIDATE_INTEGRATION,
    assess,
    feeds_ready,
    live_candidates,
    run_cycle,
    snapshot_entities,
)
from lattice_wildfire.fusion.fuser import RETIRED_TTL_S, WEIGHTS
from lattice_wildfire.fusion.picture import CLUSTER_RADIUS_M
from lattice_wildfire.harness import start_harness

FEEDS = Path(__file__).parent / "fixtures" / "feeds"
FUSION = Path(__file__).parent / "fixtures" / "fusion"
HISTORY_7D = FUSION / "firms-viirs-noaa20-7d-lacounty-20261109.csv"
HISTORY_FETCHED_AT = datetime(2026, 11, 9, 19, 37, 5, tzinfo=UTC)  # see tests/fixtures/fusion/README.md

CORE = Path(__file__).parent / "fixtures" / "core"
M_PER_DEG_LAT = 6_371_008.8 * math.pi / 180.0
CANDIDATE_ID = re.compile(r"^lw-cand-\d{8}-[0-9b-hjkmnp-z]{7}$")


# ------------------------------------------------------------------ fixtures from real captures


@pytest.fixture(scope="module")
def hotspots():
    return parsers.parse_firms_csv((FEEDS / "firms-viirs-noaa20-ca-20261109.csv").read_text())


@pytest.fixture(scope="module")
def timber():
    data = json.loads((FEEDS / "wfigs-perimeters-ca-20261109.geojson").read_text())
    (perimeter,) = parsers.parse_wfigs_perimeters(data)
    return perimeter


@pytest.fixture(scope="module")
def wfigs_incidents():
    return parsers.parse_wfigs_incidents(
        json.loads((FEEDS / "wfigs-incidents-ca-20261109.geojson").read_text())
    )


@pytest.fixture(scope="module")
def history7d() -> HeatHistory:
    return history_from_csv(HISTORY_7D.read_text(), HISTORY_FETCHED_AT)


def history_subset(history: HeatHistory, keep) -> HeatHistory:
    return dataclasses.replace(history, detections=tuple(d for d in history.detections if keep(d)))


def entity(kwargs: dict) -> Entity:
    return Entity(**kwargs)


def hotspot_entity(h) -> Entity:
    return entity(builders.build_hotspot_entity(h))


def find(hotspots, lat: float, lon: float):
    return next(h for h in hotspots if (h.lat, h.lon) == (lat, lon))


def moved(h, lat: float, lon: float, acq_time: str | None = None):
    """A real detection relocated (and optionally re-timed); entity id follows the new fields."""
    acq_time = acq_time or h.acq_time
    acquired = h.acquired.replace(hour=int(acq_time[:2]), minute=int(acq_time[2:]))
    return dataclasses.replace(h, lat=lat, lon=lon, acq_time=acq_time, acquired=acquired)


def offset_north(lat: float, lon: float, meters: float) -> tuple[float, float]:
    return lat + meters / M_PER_DEG_LAT, lon


def square(lat: float, lon: float, half_deg: float) -> list[tuple[float, float]]:
    return [
        (lat - half_deg, lon - half_deg),
        (lat - half_deg, lon + half_deg),
        (lat + half_deg, lon + half_deg),
        (lat + half_deg, lon - half_deg),
        (lat - half_deg, lon - half_deg),
    ]


def red_flag_kwargs(ring: list[tuple[float, float]], now: datetime) -> list[dict]:
    """Drawn polygon run through Fireline Feeds' red flag builder (no live warning at capture)."""
    alert = NwsAlert(
        alert_id="urn:oid:test.red-flag.ember-fusion",
        event="Red Flag Warning",
        headline="Red Flag Warning (test polygon)",
        area_desc="test polygon",
        severity="Severe",
        certainty="Likely",
        urgency="Expected",
        sender_name="NWS Los Angeles/Oxnard CA",
        sent=now,
        effective=now,
        onset=now,
        expires=now + timedelta(hours=6),
        ends=now + timedelta(hours=6),
        parts=[[ring]],
    )
    return builders.build_red_flag_entities(alert, now)


def red_flag_entities(ring: list[tuple[float, float]], now: datetime) -> list[Entity]:
    return [entity(kw) for kw in red_flag_kwargs(ring, now)]


def offline_perceivers(picture, hospitals=(), fhsz=None, history=None):
    """Production perceivers with exposure data (and optionally 7-day history) passed in, not fetched."""
    perceivers = [
        HotspotClusterer(),
        IncidentAssociator(),
        RedFlagContext(),
        ExposurePerceiver(hospitals=hospitals, fhsz_lookup=lambda lat, lon: fhsz),
    ]
    if history is not None:
        perceivers.append(PersistentHeatPerceiver(history=history))
    return perceivers


def test_snapshot_keeps_latest_input_entity_and_applies_deletions():
    def entity(entity_id: str, data_type: str):
        return SimpleNamespace(entity_id=entity_id, provenance=SimpleNamespace(data_type=data_type))

    hotspot = entity("hotspot-1", "firms-viirs-hotspot")
    updated_hotspot = entity("hotspot-1", "firms-viirs-hotspot")
    deleted_hotspot = entity("hotspot-2", "firms-viirs-hotspot")
    candidate = entity("candidate-1", "ember-candidate")
    foreign = entity("foreign-1", "unrelated-feed")
    events = [
        SimpleNamespace(event="entity", entity=hotspot, event_type="EVENT_TYPE_CREATED"),
        SimpleNamespace(event="entity", entity=deleted_hotspot, event_type="EVENT_TYPE_CREATED"),
        SimpleNamespace(event="heartbeat", entity=None, event_type=None),
        SimpleNamespace(event="entity", entity=foreign, event_type="EVENT_TYPE_CREATED"),
        SimpleNamespace(event="entity", entity=candidate, event_type="EVENT_TYPE_CREATED"),
        SimpleNamespace(event="entity", entity=updated_hotspot, event_type="EVENT_TYPE_UPDATED"),
        SimpleNamespace(event="entity", entity=deleted_hotspot, event_type="EVENT_TYPE_DELETED"),
    ]

    class Entities:
        def stream_entities(self, **kwargs):
            assert kwargs == {
                "pre_existing_only": True,
                "request_options": {"timeout_in_seconds": 120},
            }
            return iter(events)

    snapshot = snapshot_entities(SimpleNamespace(entities=Entities()))

    # Own candidates stay in the snapshot so a cycle can retire lapsed ones; unrelated types drop.
    assert snapshot == [updated_hotspot, candidate]


# ------------------------------------------------------------------ clustering


def test_900m_apart_cluster_2km_apart_do_not(hotspots):
    base = find(hotspots, 34.15419, -118.19738)  # real NOAA-20 pixel, Eagle Rock / Glendale
    near = moved(base, *offset_north(base.lat, base.lon, 900), acq_time="0915")
    far = moved(base, *offset_north(base.lat, base.lon, 2000), acq_time="0915")
    assert haversine_m(base.lat, base.lon, near.lat, near.lon) == pytest.approx(900, abs=1)
    assert haversine_m(base.lat, base.lon, far.lat, far.lon) == pytest.approx(2000, abs=1)

    together = picture_from_entities([hotspot_entity(base), hotspot_entity(near)])
    assert [len(c.members) for c in together.clusters] == [2]

    apart = picture_from_entities([hotspot_entity(base), hotspot_entity(far)])
    assert sorted(len(c.members) for c in apart.clusters) == [1, 1]
    assert len({c.cluster_id for c in apart.clusters}) == 2


def test_cluster_radius_boundary(hotspots):
    base = find(hotspots, 34.15419, -118.19738)
    inside = moved(base, *offset_north(base.lat, base.lon, CLUSTER_RADIUS_M - 5), acq_time="0915")
    outside = moved(base, *offset_north(base.lat, base.lon, CLUSTER_RADIUS_M + 5), acq_time="0915")
    assert len(picture_from_entities([hotspot_entity(base), hotspot_entity(inside)]).clusters) == 1
    assert len(picture_from_entities([hotspot_entity(base), hotspot_entity(outside)]).clusters) == 2


def test_real_pairs_cluster_by_distance(hotspots):
    # Two real Carson-area pixels 1624 m apart stay separate; two real Merced pixels 856 m apart join.
    carson = [find(hotspots, 33.82021, -118.24789), find(hotspots, 33.81532, -118.23132)]
    assert len(picture_from_entities(map(hotspot_entity, carson)).clusters) == 2
    merced = [find(hotspots, 37.73487, -119.881), find(hotspots, 37.74072, -119.87468)]
    assert len(picture_from_entities(map(hotspot_entity, merced)).clusters) == 1


def test_clusterer_merges_satellites_sums_frp_keeps_max_confidence(hotspots):
    base = find(hotspots, 34.15419, -118.19738)  # NOAA-20, nominal, 1.73 MW
    snpp_high = dataclasses.replace(
        moved(base, base.lat + 0.001, base.lon, acq_time="0950"), satellite="N", confidence="high", frp_mw=4.2
    )
    picture = picture_from_entities([hotspot_entity(base), hotspot_entity(snpp_high)])
    (cluster,) = picture.clusters
    findings = {f.kind: f for f in HotspotClusterer().observe(picture)}
    data = findings["confidence"].data
    assert data["satellites"] == ["N", "N20"]
    assert data["frp_sum_mw"] == pytest.approx(5.93)
    assert data["max_confidence"] == "high"
    assert data["passes"] == 2 and data["detections"] == 2
    assert findings["confidence"].value == 1.0
    assert "5.93 MW" in findings["frp"].evidence


# ------------------------------------------------------------------ association


def test_cluster_inside_perimeter_is_associated_not_published(hotspots, timber):
    perimeter_entities = [entity(kw) for kw in builders.build_perimeter_entities(timber)]
    exterior = ranked_parts(timber.parts)[0][0]
    # Real detection moved to a point inside the real Timber perimeter (no capture had one there).
    inside_point = (36.250222762212346, -121.7064417459883)
    assert point_in_polygon(*inside_point, exterior)
    hotspot = moved(hotspots[0], *inside_point)

    picture, _, (assessment,) = assess(
        [hotspot_entity(hotspot), *perimeter_entities], offline_perceivers, threshold=0.0
    )
    assert assessment.associated_with == perimeter_entities[0].entity_id
    assert assessment.publish is False
    assert "inside perimeter" in "\n".join(assessment.evidence)

    # Control: the same detection outside every perimeter part is unassociated and, at threshold 0, published.
    outside = moved(hotspots[0], 36.6, -121.7064417459883)
    _, _, (control,) = assess(
        [hotspot_entity(outside), *perimeter_entities], offline_perceivers, threshold=0.0
    )
    assert control.associated_with is None and control.publish is True


def test_cluster_near_real_incident_is_associated(hotspots, wfigs_incidents):
    # Real capture: NOAA-20 pixels 0.3-1.0 km from the WFIGS "2026 NORTH MERCED UNDERBURN PLAN" (RX).
    merced = [h for h in hotspots if abs(h.lat - 37.735) < 0.01 and abs(h.lon + 119.88) < 0.01]
    assert len(merced) >= 3
    incident_entities = [
        entity(builders.build_wfigs_incident_entity(i))
        for i in wfigs_incidents
        if i.irwin_id and i.lat is not None
    ]
    _, _, assessments = assess([*map(hotspot_entity, merced), *incident_entities], offline_perceivers, 0.0)
    (assessment,) = assessments
    merced_incident = next(i for i in wfigs_incidents if "NORTH MERCED" in i.name.upper())
    assert assessment.associated_with == merced_incident.entity_id
    assert assessment.publish is False


def test_incident_association_boundary_3km(hotspots, wfigs_incidents):
    incident = next(i for i in wfigs_incidents if "NORTH MERCED" in i.name.upper())
    inc_entity = entity(builders.build_wfigs_incident_entity(incident))
    base = hotspots[0]
    near = moved(base, *offset_north(incident.lat, incident.lon, 2990))
    far = moved(base, *offset_north(incident.lat, incident.lon, 3010))
    assert (
        assess([hotspot_entity(near), inc_entity], offline_perceivers)[2][0].associated_with
        == incident.entity_id
    )
    assert assess([hotspot_entity(far), inc_entity], offline_perceivers)[2][0].associated_with is None


# ------------------------------------------------------------------ scoring


def test_red_flag_raises_score_of_same_isolated_cluster(hotspots):
    now = datetime.now(UTC)
    base = find(hotspots, 34.15419, -118.19738)
    over_cluster = red_flag_entities(square(base.lat, base.lon, 0.05), now)
    elsewhere = red_flag_entities(square(34.6, -118.0, 0.05), now)

    _, _, (inside,) = assess([hotspot_entity(base), *over_cluster], offline_perceivers)
    _, _, (outside,) = assess([hotspot_entity(base), *elsewhere], offline_perceivers)
    assert inside.associated_with is None and outside.associated_with is None
    assert inside.components["red_flag"] == 1.0 and outside.components["red_flag"] == 0.0
    assert inside.score == pytest.approx(outside.score + WEIGHTS["red_flag"])
    assert inside.score > outside.score


def test_expired_red_flag_is_ignored(hotspots):
    now = datetime.now(UTC)
    base = find(hotspots, 34.15419, -118.19738)
    flags = red_flag_entities(square(base.lat, base.lon, 0.05), now)
    later = now + timedelta(hours=2)  # builder caps expiry at now + 1 h
    picture = picture_from_entities([hotspot_entity(base), *flags], now=later)
    assert picture.red_flags == ()
    assert picture.skipped.get("expired-or-not-live") == len(flags)


def test_weights_sum_to_one_and_score_bounded(hotspots):
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    base = find(hotspots, 34.15419, -118.19738)
    strong = dataclasses.replace(base, confidence="high", frp_mw=500.0)
    entities = [
        hotspot_entity(strong),
        *red_flag_entities(square(base.lat, base.lon, 0.05), datetime.now(UTC)),
    ]
    _, _, (a,) = assess(entities, lambda p: offline_perceivers(p, fhsz="Very High"))
    assert 0.0 <= a.score <= 1.0
    assert a.components["fhsz"] == 1.0
    assert track_quality(a.score) == round(a.score * 15)
    assert track_quality(0.0) == 0 and track_quality(1.0) == 15


def test_fhsz_lookup_failure_is_reported_not_guessed(hotspots):
    def broken(lat, lon):
        raise TimeoutError("eGIS timed out")

    picture = picture_from_entities([hotspot_entity(hotspots[0])])
    findings = ExposurePerceiver(hospitals=(), fhsz_lookup=broken).observe(picture)
    fhsz = next(f for f in findings if f.kind == "fhsz")
    assert fhsz.value == 0.0 and "FHSZ lookup failed: TimeoutError" in fhsz.evidence


@pytest.mark.skipif(not list(CORE.glob("*station*")), reason="core station fixture not captured")
def test_exposure_counts_real_stations(hotspots):
    from lattice_wildfire.core.layers import facilities_from_geojson

    stations = facilities_from_geojson(
        json.loads(sorted(CORE.glob("*station*"))[0].read_text()), "fire_station"
    )
    assert stations
    s = stations[0]
    detection = moved(hotspots[0], *offset_north(s.lat, s.lon, 2000))
    picture = picture_from_entities([hotspot_entity(detection)])
    findings = ExposurePerceiver(hospitals=(), stations=stations, fhsz_lookup=lambda a, b: None).observe(
        picture
    )
    proximity = next(f for f in findings if f.kind == "proximity")
    assert proximity.value == 1.0
    assert proximity.data["stations"]["within_5km"] >= 1
    assert proximity.data["stations"]["nearest"]["distance_m"] <= 2001


# ------------------------------------------------------------------ ids, snapshot rules, parallelism


def test_candidate_id_stable_across_runs_and_growth(hotspots):
    glendale = [h for h in hotspots if abs(h.lat - 34.154) < 0.003 and abs(h.lon + 118.196) < 0.003]
    assert len(glendale) == 3  # three real passes over the same spot, 2026-11-08 and 2026-11-09
    entities = list(map(hotspot_entity, glendale))
    first = assess(entities, offline_perceivers, now=datetime.now(UTC))[0].clusters
    second = assess(
        list(reversed(entities)), offline_perceivers, now=datetime.now(UTC) + timedelta(minutes=5)
    )[0].clusters
    assert [c.cluster_id for c in first] == [c.cluster_id for c in second]
    (cluster,) = first
    assert CANDIDATE_ID.match(cluster.cluster_id)
    assert cluster.cluster_id.startswith("lw-cand-20261108-")  # date of the earliest detection

    # Earlier picture with only the first pass already carries the same id.
    earliest = min(glendale, key=lambda h: h.acquired)
    (alone,) = assess([hotspot_entity(earliest)], offline_perceivers)[0].clusters
    assert alone.cluster_id == cluster.cluster_id


def test_picture_ignores_own_candidates_and_dead_entities(hotspots):
    base = find(hotspots, 34.15419, -118.19738)
    picture, perception, (assessment,) = assess([hotspot_entity(base)], offline_perceivers, threshold=0.0)
    candidate = entity(build_candidate_entity(assessment, datetime.now(UTC)))
    dead = entity({**builders.build_hotspot_entity(moved(base, 34.5, -118.5)), "is_live": False})
    again = picture_from_entities([hotspot_entity(base), candidate, dead])
    assert [c.cluster_id for c in again.clusters] == [assessment.cluster.cluster_id]
    assert again.skipped == {"expired-or-not-live": 1}


def test_perceivers_run_concurrently(hotspots):
    barrier = threading.Barrier(3, timeout=5)

    class Waiting:
        def __init__(self, name):
            self.name = name

        def observe(self, picture):
            barrier.wait()  # only returns when all three are running at once
            return []

    picture = picture_from_entities([hotspot_entity(hotspots[0])])
    result = run_perceivers(picture, [Waiting("a"), Waiting("b"), Waiting("c")])
    assert result.errors == {}


def test_failed_associator_blocks_publication(hotspots):
    class BrokenAssociator(IncidentAssociator):
        def observe(self, picture):
            raise RuntimeError("perimeter layer unreadable")

    picture = picture_from_entities([hotspot_entity(hotspots[0])])
    perception = run_perceivers(picture, [HotspotClusterer(), BrokenAssociator(), RedFlagContext()])
    (assessment,) = fuse(picture, perception, threshold=0.0)
    assert assessment.publish is False
    assert "incident-associator" in perception.errors
    assert any("perceiver incident-associator failed" in line for line in assessment.evidence)


def test_fuser_is_deterministic(hotspots):
    entities = list(map(hotspot_entity, hotspots))
    picture = picture_from_entities(entities)
    perception = run_perceivers(picture, offline_perceivers(picture))
    shuffled = run_perceivers(picture, list(reversed(offline_perceivers(picture))))
    a = [x.to_json() for x in fuse(picture, perception)]
    b = [x.to_json() for x in fuse(picture, shuffled)]
    assert [x["cluster_id"] for x in a] == [x["cluster_id"] for x in b]
    assert [x["score"] for x in a] == [x["score"] for x in b]
    assert [x["components"] for x in a] == [x["components"] for x in b]


def test_findings_only_reference_known_clusters(hotspots):
    picture = picture_from_entities(map(hotspot_entity, hotspots))
    ids = {c.cluster_id for c in picture.clusters}
    for perceiver in offline_perceivers(picture):
        for finding in perceiver.observe(picture):
            assert isinstance(finding, Finding) and finding.cluster_id in ids
            assert 0.0 <= finding.value <= 1.0


# ------------------------------------------------------------------ persistent heat (7-day FIRMS history)

TORRANCE = "lw-cand-20261108-9q5be4w"  # real repeat pixels at the Torrance refinery
CARSON = "lw-cand-20261108-9q5bmhy"  # real repeat pixels at the Carson refinery


def la_hotspot_entities(hotspots) -> list[Entity]:
    return [hotspot_entity(h) for h in hotspots if 33.70 <= h.lat <= 34.82 and -118.95 <= h.lon <= -117.65]


def assess_with_history(hotspots, history, threshold=0.0):
    _, _, assessments = assess(
        la_hotspot_entities(hotspots), lambda p: offline_perceivers(p, history=history), threshold
    )
    return {a.cluster.cluster_id: a for a in assessments}


def heat_finding(assessment) -> str:
    return next(
        line for line in assessment.evidence if line.startswith(("persistent heat:", "persistent site"))
    )


def with_frp(hotspots, cluster_id, frp_for) -> list:
    """The real detections of one LA cluster, the first pixel's FRP replaced by `frp_for(member)`."""
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    members = next(c for c in picture.clusters if c.cluster_id == cluster_id).members
    first = members[0].entity_id
    return [
        dataclasses.replace(h, frp_mw=frp_for(h)) if h.entity_id == first else h
        for h in hotspots
        if 33.70 <= h.lat <= 34.82 and -118.95 <= h.lon <= -117.65
    ]


def test_persistent_heat_sources_suppressed(hotspots, history7d):
    by_id = assess_with_history(hotspots, history7d)
    for site in (TORRANCE, CARSON):
        a = by_id[site]
        assert a.status == "persistent-heat" and a.publish is False  # threshold 0: only the filter holds it
        assert a.associated_with is None and a.score > 0
        assert a.reason.startswith("persistent heat source: prior detections on ")
        assert a.to_json()["status"] == "persistent-heat"
    assert "2026-11-02, 2026-11-03, 2026-11-04" in heat_finding(by_id[TORRANCE])


def test_two_day_site_not_suppressed_three_days_is(hotspots, history7d):
    two_days = history_subset(history7d, lambda d: d.acq_date in {"2026-11-02", "2026-11-03"})
    a = assess_with_history(hotspots, two_days)[TORRANCE]
    assert a.status == "candidate" and a.publish is True
    assert "on 2 date(s) in 7 days: 2026-11-02, 2026-11-03" in heat_finding(a)

    three_days = history_subset(history7d, lambda d: d.acq_date in {"2026-11-02", "2026-11-03", "2026-11-04"})
    assert assess_with_history(hotspots, three_days)[TORRANCE].status == "persistent-heat"


def test_persistent_site_still_held_within_its_own_history(hotspots, history7d):
    a = assess_with_history(hotspots, history7d)[TORRANCE]
    finding = next(f for f in a.evidence if f.startswith("persistent heat: fixed heat source"))
    by_kind = {}
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    for f in PersistentHeatPerceiver(history=history7d).observe(picture):
        by_kind[f.cluster_id] = f
    data = by_kind[TORRANCE].data
    assert data["status"] == "persistent"
    assert data["current_max_frp_mw"] <= 2.0 * data["history_max_frp_mw"]
    assert data["current_max_pixels_per_date"] <= data["history_max_pixels_per_date"] + 2
    assert "FRP x" in finding and a.status == "persistent-heat"


def test_persistent_site_with_frp_3x_history_publishes(hotspots, history7d):
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    site = next(
        f for f in PersistentHeatPerceiver(history=history7d).observe(picture) if f.cluster_id == TORRANCE
    )
    history_max = site.data["history_max_frp_mw"]

    def judged(frp):
        entities = [hotspot_entity(h) for h in with_frp(hotspots, TORRANCE, lambda h: frp)]
        _, _, assessments = assess(entities, lambda p: offline_perceivers(p, history=history7d), 0.0)
        return next(a for a in assessments if a.cluster.cluster_id == TORRANCE)

    grown = judged(3.0 * history_max)
    assert grown.status == "candidate" and grown.publish is True
    assert heat_finding(grown).startswith("persistent site but growing: FRP x3.0, pixels ")
    assert judged(2.0 * history_max).status == "persistent-heat"  # boundary: 2.0x is still the site


def test_cluster_own_detections_are_not_history(hotspots, history7d):
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    glendale = next(c for c in picture.clusters if c.cluster_id == "lw-cand-20261108-9q5fq3e")
    own_ids = {h.entity_id for h in glendale.members}
    own_only = history_subset(history7d, lambda d: d.entity_id in own_ids)
    assert own_only.detections  # the 7-day file repeats the cluster's own 24 h pixels
    finding = PersistentHeatPerceiver(history=own_only).observe(picture)
    mine = next(f for f in finding if f.cluster_id == glendale.cluster_id)
    assert mine.value == 0.0 and mine.data["dates"] == []


def test_history_fetch_failure_does_not_suppress(hotspots):
    def unreachable() -> HeatHistory:
        raise FetchError("FIRMS 7-day file unreachable and no cached copy")

    a = assess_with_history(hotspots, unreachable)[TORRANCE]
    assert a.status == "candidate" and a.publish is True
    assert heat_finding(a) == "persistent heat: unknown (7-day FIRMS history unavailable: FetchError)"


# ------------------------------------------------------------------ three-satellite history

HISTORY_FILES = {
    "NOAA-20": FUSION / "firms-viirs-noaa20-7d-lacounty-20261109.csv",
    "Suomi NPP": FUSION / "firms-viirs-suominpp-7d-lacounty-20261109.csv",
    "NOAA-21": FUSION / "firms-viirs-noaa21-7d-lacounty-20261109.csv",
}


def satellite_detections(name: str, dates: set[str] | None = None):
    """One satellite's real captured rows, optionally cut to a set of acquisition dates."""
    rows = history_from_csv(HISTORY_FILES[name].read_text(), HISTORY_FETCHED_AT).detections
    return tuple(d for d in rows if dates is None or d.acq_date in dates)


def merged_satellite_history(parts: dict, failures=()) -> HeatHistory:
    return merged_history(parts, HISTORY_FETCHED_AT, failures=failures)


def test_one_satellite_failing_still_holds_torrance(hotspots):
    # NOAA-21 down: NOAA-20 and Suomi NPP still carry the refineries, and the gap is named.
    history = merged_satellite_history(
        {"NOAA-20": satellite_detections("NOAA-20"), "Suomi NPP": satellite_detections("Suomi NPP")},
        failures=(SourceFailure("NOAA-21", "FetchError: HTTP 503 from FIRMS"),),
    )
    assert history.partial and history.sources == ("NOAA-20", "Suomi NPP")
    a = assess_with_history(hotspots, history)[TORRANCE]
    assert a.status == "persistent-heat" and a.publish is False
    assert "partial 7-day history: NOAA-21 unavailable" in a.reason
    assert "partial history: NOAA-21 unavailable (FetchError: HTTP 503 from FIRMS)" in heat_finding(a)


def test_merged_history_counts_distinct_dates_across_satellites(hotspots):
    # NOAA-20 saw the site on 11-02 and 11-03; only NOAA-21 saw it on 11-08. 11-03 is on both
    # files and counts once, so the union is three dates and the site is held.
    noaa20 = satellite_detections("NOAA-20", {"2026-11-02", "2026-11-03"})
    noaa21 = satellite_detections("NOAA-21", {"2026-11-03", "2026-11-08"})
    both = merged_satellite_history({"NOAA-20": noaa20, "NOAA-21": noaa21})
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    site = next(f for f in PersistentHeatPerceiver(history=both).observe(picture) if f.cluster_id == TORRANCE)
    assert site.data["dates"] == ["2026-11-02", "2026-11-03", "2026-11-08"]
    a = assess_with_history(hotspots, both)[TORRANCE]
    assert a.status == "persistent-heat"
    assert "on 3 date(s) in 7 days: 2026-11-02, 2026-11-03, 2026-11-08" in heat_finding(a)

    # The same NOAA-20 rows alone are two dates, so the last date really came from NOAA-21.
    alone = assess_with_history(hotspots, merged_satellite_history({"NOAA-20": noaa20}))[TORRANCE]
    assert alone.status == "candidate" and alone.publish is True


def test_every_satellite_failing_is_unknown_and_holds_nothing(hotspots, monkeypatch):
    def down(url: str, **kwargs: object):
        raise FetchError("connection refused")

    monkeypatch.setattr(history_module, "fetch_text", down)
    with pytest.raises(FetchError) as caught:
        firms_7d_history()
    message = str(caught.value)
    for satellite in ("NOAA-20", "Suomi NPP", "NOAA-21"):
        assert f"{satellite}: FetchError: connection refused" in message

    by_id = assess_with_history(hotspots, firms_7d_history)
    a = by_id[TORRANCE]
    assert a.status == "candidate" and a.publish is True
    assert heat_finding(a) == "persistent heat: unknown (7-day FIRMS history unavailable: FetchError)"
    assert not any(x.status == "persistent-heat" for x in by_id.values())


def test_malformed_satellite_file_is_partial_not_fatal(monkeypatch):
    real = history_module.fetch_text

    def fake(url: str, **kwargs: object):
        if "noaa-21" in url:
            return Fetched("latitude,longitude\n33.8,-118.2\n", HISTORY_FETCHED_AT, False, False)
        return real(url, **kwargs)

    monkeypatch.setattr(history_module, "fetch_text", fake)
    history = firms_7d_history()
    assert history.sources == ("NOAA-20", "Suomi NPP") and history.partial
    (failure,) = history.failures
    assert failure.satellite == "NOAA-21" and failure.error.startswith("ValueError: ")


def test_growth_override_publishes_with_partial_history(hotspots):
    history = merged_satellite_history(
        {"NOAA-20": satellite_detections("NOAA-20"), "NOAA-21": satellite_detections("NOAA-21")},
        failures=(SourceFailure("Suomi NPP", "ValueError: row 3 has 12 columns"),),
    )
    picture = picture_from_entities(la_hotspot_entities(hotspots))
    site = next(
        f for f in PersistentHeatPerceiver(history=history).observe(picture) if f.cluster_id == TORRANCE
    )
    grown = with_frp(hotspots, TORRANCE, lambda h: 3.0 * site.data["history_max_frp_mw"])
    _, _, assessments = assess(
        [hotspot_entity(h) for h in grown], lambda p: offline_perceivers(p, history=history), 0.0
    )
    a = next(x for x in assessments if x.cluster.cluster_id == TORRANCE)
    assert a.status == "candidate" and a.publish is True
    evidence = heat_finding(a)
    assert evidence.startswith("persistent site but growing: FRP x3.0")
    assert "partial history: Suomi NPP unavailable" in evidence


# ------------------------------------------------------------------ end to end on the contract harness


def test_end_to_end_publish_fuse_read_back(lattice, hotspots, timber, wfigs_incidents, history7d, tmp_path):
    """Publish real inputs with the SDK, run one fusion cycle against the harness, read back."""
    now = datetime.now(UTC)
    glendale = [h for h in hotspots if abs(h.lat - 34.154) < 0.003 and abs(h.lon + 118.196) < 0.003]
    # The same three real Glendale detections relocated to an Angeles NF ridge with no 7-day history.
    fresh = [moved(h, h.lat + 0.18, h.lon + 0.25) for h in glendale]
    merced = [h for h in hotspots if abs(h.lat - 37.735) < 0.01 and abs(h.lon + 119.88) < 0.01]
    merced_incident = next(i for i in wfigs_incidents if "NORTH MERCED" in i.name.upper())
    timber_hotspot = moved(hotspots[0], 36.250222762212346, -121.7064417459883)

    inputs = [
        *(builders.build_hotspot_entity(h, now) for h in [*glendale, *fresh, *merced, timber_hotspot]),
        builders.build_wfigs_incident_entity(merced_incident, now),
        *builders.build_perimeter_entities(timber, now),
        *red_flag_kwargs(square(34.334, -117.946, 0.05), now),
    ]
    for kwargs in inputs:
        lattice.entities.publish_entity(**kwargs)

    entities = snapshot_entities(lattice)
    assert {e.entity_id for e in entities} >= {kw["entity_id"] for kw in inputs}

    def perceivers(picture):
        return offline_perceivers(picture, history=history7d)

    picture_path = tmp_path / "picture.jsonl"
    marker = ready_marker(tmp_path)
    report = run_cycle(
        lattice, entities, aoi=None, picture_path=picture_path, perceivers_for=perceivers, ready_marker=marker
    )
    by_anchor = {a.cluster.anchor.entity_id: a for a in report.assessments}
    fresh_a = by_anchor[min(fresh, key=lambda h: h.acquired).entity_id]
    glendale_a = by_anchor[min(glendale, key=lambda h: h.acquired).entity_id]
    merced_a = by_anchor[min(merced, key=lambda h: h.acquired).entity_id]
    timber_a = by_anchor[timber_hotspot.entity_id]

    assert fresh_a.status == "candidate"
    assert glendale_a.status == "persistent-heat"  # real repeat detections at this spot all week
    assert merced_a.status == "associated" and merced_a.associated_with == merced_incident.entity_id
    assert timber_a.status == "associated" and timber_a.associated_with == f"lw-perim-{timber.key}"
    fresh_id = fresh_a.cluster.cluster_id
    assert fresh_id in report.published
    for held in (glendale_a, merced_a, timber_a):
        assert held.cluster.cluster_id not in report.published
    assert report.publish_errors == {}

    candidate = lattice.entities.get_entity(fresh_id)
    assert candidate.entity_id == fresh_id
    assert candidate.provenance.data_type == "ember-candidate"
    assert candidate.provenance.integration_name == "lattice-wildfire-fusion"
    assert candidate.ontology.template == "TEMPLATE_TRACK"
    assert candidate.ontology.platform_type == "New Start Candidate"
    assert candidate.mil_view.disposition == "DISPOSITION_SUSPICIOUS"
    assert candidate.mil_view.environment == "ENVIRONMENT_LAND"
    assert candidate.tracked.track_quality_wrapper == round(fresh_a.score * 15)
    assert candidate.tracked.sensor_hits == 3
    assert candidate.aliases.name
    assert candidate.is_live is True and candidate.expiry_time > now
    assert candidate.description.splitlines()[0] == f"score: {fresh_a.score:.2f} (threshold 0.40)"
    assert "inside Red Flag Warning" in candidate.description
    assert "3 detection(s), 3 pass(es)" in candidate.description
    assert (
        "persistent heat: not a fixed heat source; prior detections within 750 m on 0 date(s)"
        in candidate.description
    )
    assert candidate.location.position.latitude_degrees == pytest.approx(fresh_a.cluster.centroid[0])
    for held in (glendale_a, merced_a, timber_a):
        with pytest.raises(ApiError):
            lattice.entities.get_entity(held.cluster.cluster_id)

    (line,) = picture_path.read_text().splitlines()
    record = {a["cluster_id"]: a for a in json.loads(line)["assessments"]}
    assert fresh_id in json.loads(line)["published"]
    assert record[glendale_a.cluster.cluster_id]["status"] == "persistent-heat"
    assert record[glendale_a.cluster.cluster_id]["reason"].startswith("persistent heat source")

    # A second cycle over the updated Lattice state ignores its own candidate and republishes the same id.
    again = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        picture_path=picture_path,
        perceivers_for=perceivers,
        ready_marker=marker,
    )
    assert fresh_id in again.published
    assert sum(a.cluster.cluster_id == fresh_id for a in again.assessments) == 1
    assert len(picture_path.read_text().splitlines()) == 2


# ------------------------------------------------------------------ stale candidate retirement

# Open ground in Monterey County, well apart from every capture's detections, incidents and
# perimeters: a cluster here is unassociated and has no 7-day history (that file is LA County).
OPEN_GROUND = (36.6110, -121.7064)
OPEN_GROUND_NORTH = (36.6350, -121.7064)  # 2.7 km apart, so the two clusters never merge
OPEN_GROUND_EAST = (36.6110, -121.6730)  # 3 km east: a third separate cluster


def drawn_perimeter(lat: float, lon: float, half_deg: float, key: str = "RETIRE-TEST-1") -> Perimeter:
    """A square perimeter drawn around one point (no capture has a perimeter over the test ground)."""
    return Perimeter(
        irwin_id=key,
        object_id=None,
        name="RETIRE TEST",
        parts=[[square(lat, lon, half_deg)]],
        gis_acres=None,
        incident_size_acres=None,
        percent_contained=None,
        feature_category=None,
        map_method=None,
        polygon_time=None,
        date_current=None,
        county=None,
        state=None,
    )


def ready_marker(tmp_path: Path) -> Path:
    """The readiness marker Fireline Feeds leaves after its first full cycle."""
    marker = tmp_path / "feeds" / "ready.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"run_at": "2026-11-09T19:37:05Z"}), encoding="utf-8")
    return marker


@pytest.fixture
def isolated_lattice(monkeypatch):
    """A private contract harness, so readiness and retirement see no other test's entities."""
    with start_harness(0, long_poll_timeout_s=2.0) as harness:
        monkeypatch.setenv("LATTICE_BASE_URL", harness.base_url)
        monkeypatch.setenv("ENVIRONMENT_TOKEN", "test-token")
        yield make_client(LatticeSettings.from_env(dotenv=False), timeout=30)


def test_readiness_gate_holds_publishing_until_feeds_have_run(hotspots):
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    picture, _, (gated,) = assess([hotspot_entity(hotspot)], offline_perceivers, 0.0, feeds_ready=False)
    assert picture.incidents == () and picture.perimeters == ()
    assert gated.status == "association-unknown" and gated.publish is False
    assert gated.reason.startswith("feeds not ready")
    assert any(line.startswith("feeds readiness:") for line in gated.evidence)

    _, _, (open_gate,) = assess([hotspot_entity(hotspot)], offline_perceivers, 0.0, feeds_ready=True)
    assert open_gate.status == "candidate" and open_gate.publish is True


def test_readiness_gate_opens_when_the_snapshot_has_an_incident(hotspots, wfigs_incidents):
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    incidents = [
        entity(builders.build_wfigs_incident_entity(i))
        for i in wfigs_incidents
        if i.irwin_id and i.lat is not None
    ]
    _, _, (a,) = assess([hotspot_entity(hotspot), *incidents], offline_perceivers, 0.0, feeds_ready=False)
    assert "feeds not ready" not in a.reason


def test_feeds_readiness_marker_follows_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("LW_STATE_DIR", raising=False)
    assert feeds_ready() is True  # no state dir: the readiness gate is skipped
    missing = tmp_path / "feeds" / "ready.json"
    assert feeds_ready(missing) is False
    marker = ready_marker(tmp_path)
    assert feeds_ready(marker) is True
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path))
    assert feeds_ready() is True
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path / "nowhere"))
    assert feeds_ready() is False


def test_retirement_entity_keeps_id_and_shortens_expiry(hotspots):
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    _, _, (candidate,) = assess([hotspot_entity(hotspot)], offline_perceivers, 0.0, now=now)
    live = entity(build_candidate_entity(candidate, now))
    later = now + timedelta(minutes=30)
    kwargs = build_retirement_entity(live, "associated", later)

    assert kwargs["entity_id"] == candidate.cluster.cluster_id
    assert kwargs["is_live"] is True
    assert kwargs["expiry_time"] == later + timedelta(seconds=RETIRED_TTL_S)
    assert kwargs["created_time"] == candidate.cluster.first_seen
    assert kwargs["description"].splitlines()[0] == candidate.evidence[0]
    assert kwargs["description"].splitlines()[-1] == "retired: associated"
    assert kwargs["provenance"].data_type == "ember-candidate"
    assert kwargs["provenance"].integration_name == CANDIDATE_INTEGRATION
    assert kwargs["provenance"].source_update_time == later
    assert kwargs["aliases"].name == live.aliases.name
    assert kwargs["tracked"].sensor_hits == live.tracked.sensor_hits

    # Retiring the same track twice rewrites the one line instead of stacking them.
    again = build_retirement_entity(entity(kwargs), "gone", later + timedelta(seconds=1))
    retired_lines = [line for line in again["description"].splitlines() if line.startswith("retired:")]
    assert retired_lines == ["retired: gone"]


def test_retirements_name_the_reason_and_leave_live_candidates_alone(hotspots):
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    picture, _, (candidate,) = assess([hotspot_entity(hotspot)], offline_perceivers, 0.0, now=now)
    live = entity(build_candidate_entity(candidate, now))
    candidates = {live.entity_id: live}
    assert retirements(picture, (candidate,), candidates) == {}

    # A perimeter appears over the same cluster: associated, so the candidate is retracted.
    perimeter = entity(builders.build_perimeter_entities(drawn_perimeter(*OPEN_GROUND, 0.01), now)[0])
    picture2, _, (associated,) = assess(
        [hotspot_entity(hotspot), perimeter], offline_perceivers, 0.0, now=now
    )
    assert associated.status == "associated" and associated.associated_with == perimeter.entity_id
    assert retirements(picture2, (associated,), candidates) == {live.entity_id: "associated"}

    # No cluster left in the picture: the candidate's hotspots expired, merged or were withdrawn.
    empty, _, () = assess([], offline_perceivers, 0.0, now=now)
    assert retirements(empty, (), candidates) == {live.entity_id: "gone"}

    for status in ("persistent-heat", "below-threshold"):
        held = dataclasses.replace(associated, status=status, publish=False)
        assert retirements(picture2, (held,), candidates) == {live.entity_id: status}

    unknown = dataclasses.replace(associated, status="association-unknown", publish=False)
    assert retirements(picture2, (unknown,), candidates) == {}

    # Feeds not ready: an empty picture proves nothing, so nothing is retracted.
    assert retirements(empty, (), candidates, feeds_ready=False) == {}


def test_live_candidates_reads_only_this_integrations_live_tracks(hotspots):
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    _, _, (candidate,) = assess([hotspot_entity(hotspot)], offline_perceivers, 0.0, now=now)
    mine = entity(build_candidate_entity(candidate, now))
    foreign = mine.model_copy(update={"provenance": provenance("planner", "ember-candidate", now)})
    expired = mine.model_copy(update={"expiry_time": now - timedelta(seconds=1)})
    dead = mine.model_copy(update={"is_live": False})
    assert live_candidates([mine, foreign, expired, dead], now) == {mine.entity_id: mine}


def test_stale_candidate_retired_when_a_perimeter_explains_it(
    isolated_lattice, hotspots, tmp_path, monkeypatch
):
    """The Gabbs/Peavine failure: a published candidate must not outlive its association."""
    lattice = isolated_lattice
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND)
    lattice.entities.publish_entity(**builders.build_hotspot_entity(hotspot, now))
    marker = ready_marker(tmp_path)

    def perceivers(picture):
        return offline_perceivers(picture)

    first = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=perceivers,
        ready_marker=marker,
    )
    (mine,) = [a for a in first.assessments if a.cluster.anchor.entity_id == hotspot.entity_id]
    candidate_id = mine.cluster.cluster_id
    assert mine.status == "candidate" and candidate_id in first.published
    live = lattice.entities.get_entity(candidate_id)
    assert live.expiry_time > now + timedelta(seconds=3000)  # the 1 h candidate TTL
    assert "retired:" not in live.description

    # Fireline Feeds publishes the perimeter that explains this cluster before the next pass.
    perimeter = builders.build_perimeter_entities(drawn_perimeter(*OPEN_GROUND, 0.01), now)[0]
    lattice.entities.publish_entity(**perimeter)

    second = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=perceivers,
        ready_marker=marker,
    )
    (again,) = [a for a in second.assessments if a.cluster.cluster_id == candidate_id]
    assert again.status == "associated" and again.associated_with == perimeter["entity_id"]
    assert second.retired.get(candidate_id) == "associated"
    assert candidate_id not in second.published
    assert second.publish_errors == {}
    assert f"retired=1 ({candidate_id}: associated)" in second.summary()

    retired_entity = lattice.entities.get_entity(candidate_id)
    assert retired_entity.entity_id == candidate_id
    assert retired_entity.description.splitlines()[0].startswith("score: ")
    assert retired_entity.description.splitlines()[-1] == "retired: associated"
    assert retired_entity.aliases.name == live.aliases.name
    check = datetime.now(UTC)
    assert check < retired_entity.expiry_time <= check + timedelta(seconds=RETIRED_TTL_S + 1)

    # Lattice has no delete: the track is gone once its 5 s expiry passes.
    frozen = datetime.now(UTC)
    monkeypatch.setattr(
        "lattice_wildfire.harness.store.now", lambda: frozen + timedelta(seconds=RETIRED_TTL_S + 1)
    )
    with pytest.raises(ApiError):
        lattice.entities.get_entity(candidate_id)
    assert candidate_id not in {e.entity_id for e in snapshot_entities(lattice)}


def test_candidate_retired_as_gone_when_its_detections_disappear(isolated_lattice, hotspots, tmp_path):
    lattice = isolated_lattice
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND_NORTH)
    lattice.entities.publish_entity(**builders.build_hotspot_entity(hotspot, now))
    marker = ready_marker(tmp_path)

    first = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=marker,
    )
    (mine,) = [a for a in first.assessments if a.cluster.anchor.entity_id == hotspot.entity_id]
    candidate_id = mine.cluster.cluster_id
    assert candidate_id in first.published

    # The detection is withdrawn; the candidate itself is still live for its hour.
    without_hotspot = [e for e in snapshot_entities(lattice) if e.entity_id != hotspot.entity_id]
    second = run_cycle(
        lattice,
        without_hotspot,
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=marker,
    )
    assert second.retired.get(candidate_id) == "gone"
    assert second.published == []
    assert lattice.entities.get_entity(candidate_id).description.endswith("retired: gone")


def test_candidate_kept_when_feeds_are_not_ready(isolated_lattice, hotspots, tmp_path):
    """An empty snapshot with no readiness marker is a startup race, not a dead candidate."""
    lattice = isolated_lattice
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], *OPEN_GROUND_EAST)
    lattice.entities.publish_entity(**builders.build_hotspot_entity(hotspot, now))
    marker = ready_marker(tmp_path)

    first = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=marker,
    )
    (mine,) = [a for a in first.assessments if a.cluster.anchor.entity_id == hotspot.entity_id]
    candidate_id = mine.cluster.cluster_id
    assert candidate_id in first.published

    without_hotspot = [e for e in snapshot_entities(lattice) if e.entity_id != hotspot.entity_id]
    second = run_cycle(
        lattice,
        without_hotspot,
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=tmp_path / "absent" / "ready.json",
    )
    assert second.feeds_ready is False
    assert second.retired == {}
    kept = lattice.entities.get_entity(candidate_id)
    assert kept.expiry_time > now + timedelta(seconds=3000)
    assert "retired:" not in kept.description


def test_readiness_gate_blocks_then_allows_publishing_on_the_harness(isolated_lattice, hotspots, tmp_path):
    lattice = isolated_lattice
    now = datetime.now(UTC)
    hotspot = moved(hotspots[0], 36.6501, -121.7301)  # its own cluster, 4 km from the others
    lattice.entities.publish_entity(**builders.build_hotspot_entity(hotspot, now))
    late_marker = tmp_path / "feeds" / "ready.json"  # feeds has not finished a cycle yet

    first = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=late_marker,
    )
    (mine,) = [a for a in first.assessments if a.cluster.anchor.entity_id == hotspot.entity_id]
    assert first.feeds_ready is False
    assert mine.status == "association-unknown" and mine.reason.startswith("feeds not ready")
    assert first.published == []
    assert "feeds_ready=false" in first.summary()
    with pytest.raises(ApiError):
        lattice.entities.get_entity(mine.cluster.cluster_id)

    ready_marker(tmp_path)  # the feeds app completes its first full cycle
    second = run_cycle(
        lattice,
        snapshot_entities(lattice),
        aoi=None,
        threshold=0.0,
        perceivers_for=offline_perceivers,
        ready_marker=late_marker,
    )
    (published,) = [a for a in second.assessments if a.cluster.anchor.entity_id == hotspot.entity_id]
    assert second.feeds_ready is True
    assert published.status == "candidate" and published.cluster.cluster_id in second.published
    assert lattice.entities.get_entity(published.cluster.cluster_id).entity_id == published.cluster.cluster_id
