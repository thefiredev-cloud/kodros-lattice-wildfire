"""Fireline Feeds: parsers on real captured feeds, builders vs the contract, harness round-trip."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.http import Fetched, FetchError
from lattice_wildfire.core.layers import Facility, facilities_from_geojson
from lattice_wildfire.feeds import builders, parsers, sources
from lattice_wildfire.feeds.__main__ import main as feeds_main
from lattice_wildfire.feeds.geometry import orient_part, signed_area
from lattice_wildfire.feeds.publisher import (
    cycle_summary,
    publication_order,
    publish_batch,
    ready_path,
    run_cycle,
    wire_payload,
)
from lattice_wildfire.feeds.publisher import run as feeds_run
from lattice_wildfire.feeds.sources import (
    Batch,
    FetchInfo,
    select_calfire_incidents,
    select_hotspots,
    select_perimeters,
    select_red_flags,
    select_stations,
    select_wfigs_incidents,
)

FIX = Path(__file__).parent / "fixtures" / "feeds"
ZONES = Path(__file__).parent / "fixtures" / "feeds-zones"
FIRE_ZONE_URL = "https://api.weather.gov/zones/fire/CAZ288"
VENTURA_ZONE_URL = "https://api.weather.gov/zones/forecast/CAZ354"
NWS_CA_CAPTURED = datetime(2026, 11, 9, 19, 20, 7, tzinfo=UTC)
LA = AOI(min_lat=33.70, min_lon=-118.95, max_lat=34.82, max_lon=-117.65)
CALIFORNIA = AOI(min_lat=32.5, min_lon=-124.5, max_lat=42.0, max_lon=-114.1)
CONUS = AOI(min_lat=17.0, min_lon=-125.0, max_lat=50.0, max_lon=-64.0)
# Capture instants from tests/fixtures/feeds/README.md; time-dependent logic is pinned to them.
FIRMS_CAPTURED = datetime(2026, 11, 9, 19, 20, 4, tzinfo=UTC)
NWS_GEOMETRY_CAPTURED = datetime(2026, 11, 9, 19, 24, 27, tzinfo=UTC)

SIDC_RE = re.compile(r"^S[A-Z][A-Z]P[A-Z0-9-]{6}-{5}$")


def _text(name: str) -> str:
    return (FIX / name).read_text()


def _json(name: str):
    return json.loads(_text(name))


@pytest.fixture(scope="module")
def hotspots():
    return parsers.parse_firms_csv(_text("firms-viirs-noaa20-ca-20261109.csv"))


@pytest.fixture(scope="module")
def wfigs():
    return parsers.parse_wfigs_incidents(_json("wfigs-incidents-ca-20261109.geojson"))


@pytest.fixture(scope="module")
def perimeters_ca():
    return parsers.parse_wfigs_perimeters(_json("wfigs-perimeters-ca-20261109.geojson"))


@pytest.fixture(scope="module")
def perimeters_us():
    return parsers.parse_wfigs_perimeters(_json("wfigs-perimeters-us-sample10-20261109.geojson"))


@pytest.fixture(scope="module")
def geo_alerts():
    return parsers.parse_nws_alerts(_json("nws-alerts-with-geometry-20261109.json"), event=None)


@pytest.fixture(scope="module")
def calfire():
    return parsers.parse_calfire_incidents(_json("calfire-incidents-2026-20261109.json"))


def _wire(payload) -> dict:
    return wire_payload(payload)


def _rings(wire: dict) -> list[list[tuple[float, float]]]:
    return [
        [(p["position"]["latitudeDegrees"], p["position"]["longitudeDegrees"]) for p in ring["positions"]]
        for ring in wire["geoShape"]["polygon"]["rings"]
    ]


def assert_contract(payload, now: datetime) -> dict:
    """The CONTRACT.md floor every published entity must meet, checked on the wire JSON."""
    wire = _wire(payload)
    assert wire["isLive"] is True
    expiry = datetime.fromisoformat(wire["expiryTime"])
    assert now < expiry < now + timedelta(days=30)
    prov = wire["provenance"]
    assert prov["integrationName"] == "lattice-wildfire-feeds"
    assert prov["dataType"] in {
        "firms-viirs-hotspot",
        "wfigs-incident",
        "calfire-incident",
        "wfigs-perimeter",
        "nws-red-flag",
        "lacofd-station",
    }
    assert prov["sourceId"]
    assert prov["sourceUpdateTime"]
    assert wire["aliases"]["name"].strip()
    assert wire["dataClassification"]["default"]["level"] == "CLASSIFICATION_LEVELS_UNCLASSIFIED"
    template = wire["ontology"]["template"]
    if template in ("TEMPLATE_TRACK", "TEMPLATE_ASSET"):
        assert SIDC_RE.match(wire["symbology"]["milStd2525C"]["sidc"])
        assert wire["location"]["position"]["latitudeDegrees"] is not None
        assert wire["milView"]["environment"] == "ENVIRONMENT_LAND"
    else:
        assert template == "TEMPLATE_GEO"
        rings = _rings(wire)
        for index, ring in enumerate(rings):
            assert ring[0] == ring[-1] and len(ring) >= 4
            assert (signed_area(ring) > 0) == (index == 0), "exterior must be CCW, holes CW (RFC 7946)"
    return wire


# ---------------------------------------------------------------- FIRMS


def test_firms_parses_real_rows_exactly(hotspots):
    assert len(hotspots) == 83
    first = hotspots[0]
    # Line 2 of the capture: 40.82299,-114.25813,300.16,0.41,0.45,2026-11-08,0911,N20,nominal,2.0NRT,275.21,0.79,N
    assert (first.lat, first.lon) == (40.82299, -114.25813)
    assert first.acquired == datetime(2026, 11, 8, 9, 11, tzinfo=UTC)
    assert (first.bright_ti4_k, first.bright_ti5_k, first.frp_mw) == (300.16, 275.21, 0.79)
    assert (first.scan_km, first.track_km) == (0.41, 0.45)
    assert (first.satellite, first.confidence, first.version, first.daynight) == (
        "N20",
        "nominal",
        "2.0NRT",
        "N",
    )


def test_firms_header_only_file_is_empty():
    assert parsers.parse_firms_csv(_text("firms-viirs-snpp-us-20261109.csv")) == []


def test_firms_rejects_wrong_shape():
    with pytest.raises(ValueError, match="missing header"):
        parsers.parse_firms_csv("")
    with pytest.raises(ValueError, match="missing columns"):
        parsers.parse_firms_csv("latitude,longitude\n34,-118\n")
    lines = _text("firms-viirs-noaa20-ca-20261109.csv").splitlines()
    broken = lines[0] + "\n" + lines[1].replace("0.79", "n/a") + "\n"
    with pytest.raises(ValueError, match="line 2"):
        parsers.parse_firms_csv(broken)


def test_firms_area_api_confidence_letters_and_short_time():
    """The FIRMS area API abbreviates confidence (n/l/h) and may drop the leading zero of acq_time."""
    rows = list(csv.reader(io.StringIO(_text("firms-viirs-noaa20-ca-20261109.csv"))))
    header, row = rows[0], rows[1]
    row = row.copy()
    row[header.index("confidence")] = "h"
    row[header.index("acq_time")] = "911"
    buf = io.StringIO()
    csv.writer(buf).writerows([header, row])
    (h,) = parsers.parse_firms_csv(buf.getvalue())
    assert h.confidence == "high" and h.acq_time == "0911"
    assert builders.build_hotspot_entity(h)["tracked"].track_quality_wrapper == 3


def test_firms_ids_deterministic_unique_and_contract_shaped(hotspots):
    again = parsers.parse_firms_csv(_text("firms-viirs-noaa20-ca-20261109.csv"))
    ids = [h.entity_id for h in hotspots]
    assert ids == [h.entity_id for h in again]
    assert len(set(ids)) == len(ids)
    assert hotspots[0].entity_id == "lw-firms-N20-2026-11-080911-40.8230--114.2581"


def test_firms_aoi_filter_matches_independent_count(hotspots):
    payloads, skipped = select_hotspots(hotspots, LA, FIRMS_CAPTURED)
    rows = list(csv.DictReader(io.StringIO(_text("firms-viirs-noaa20-ca-20261109.csv"))))
    expected = sum(
        1
        for r in rows
        if LA.min_lat <= float(r["latitude"]) <= LA.max_lat
        and LA.min_lon <= float(r["longitude"]) <= LA.max_lon
    )
    assert expected > 0 and len(payloads) == expected
    assert skipped == Counter(out_of_aoi=len(rows) - expected)
    for p in payloads:
        pos = p["location"].position
        assert LA.min_lat <= pos.latitude_degrees <= LA.max_lat
        assert LA.min_lon <= pos.longitude_degrees <= LA.max_lon


def test_aoi_boundary_is_inclusive(hotspots):
    h = hotspots[0]
    on_edge = AOI(min_lat=h.lat, min_lon=h.lon, max_lat=h.lat + 1, max_lon=h.lon + 1)
    just_outside = AOI(min_lat=h.lat + 1e-6, min_lon=h.lon, max_lat=h.lat + 1, max_lon=h.lon + 1)
    assert len(select_hotspots([h], on_edge, FIRMS_CAPTURED)[0]) == 1
    assert len(select_hotspots([h], just_outside, FIRMS_CAPTURED)[0]) == 0


def test_hotspot_entity_maps_every_field(hotspots):
    h = next(x for x in hotspots if LA.min_lat <= x.lat <= LA.max_lat and LA.min_lon <= x.lon <= LA.max_lon)
    wire = assert_contract(builders.build_hotspot_entity(h, FIRMS_CAPTURED), FIRMS_CAPTURED)
    assert wire["entityId"] == h.entity_id
    assert wire["ontology"] == {
        "template": "TEMPLATE_TRACK",
        "platformType": "Wildfire Hotspot",
        "specificType": "VIIRS N20",
    }
    assert wire["milView"]["disposition"] == "DISPOSITION_UNKNOWN"
    assert wire["tracked"] == {"trackQualityWrapper": 2, "lastMeasurementTime": "2026-11-08T09:13:00Z"}
    assert wire["createdTime"] == "2026-11-08T09:13:00Z"
    lines = dict(line.split(": ", 1) for line in wire["description"].splitlines()[1:])
    # Capture line 9: 34.33597,-118.52243,306.7,0.58,0.52,2026-11-08,0913,N20,nominal,2.0NRT,286.78,1.41,N
    assert (lines["frp_mw"], lines["confidence"], lines["satellite"]) == ("1.41", "nominal", "N20")
    assert (lines["scan_km"], lines["track_km"]) == ("0.58", "0.52")
    assert (lines["bright_ti4_k"], lines["bright_ti5_k"]) == ("306.7", "286.78")
    assert (lines["daynight"], lines["version"], lines["acquired"]) == ("N", "2.0NRT", "2026-11-08T09:13:00Z")
    expiry = datetime.fromisoformat(wire["expiryTime"])
    assert expiry == FIRMS_CAPTURED + timedelta(hours=6)


# ---------------------------------------------------------------- FIRMS NOAA-21


def _n21(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "feeds-n21" / name).read_text()


@pytest.fixture(scope="module")
def hotspots_n21():
    return parsers.parse_firms_csv(_n21("firms-viirs-noaa21-ca-20261109.csv"))


def test_firms_noaa21_parses_real_rows_exactly(hotspots_n21):
    assert len(hotspots_n21) == 122
    first = hotspots_n21[0]
    # Line 2 of the capture: 40.00307,-120.94685,304.5,0.39,0.36,2026-11-08,0956,N21,nominal,2.0NRT,274.76,1.14,N
    assert (first.lat, first.lon) == (40.00307, -120.94685)
    assert first.acquired == datetime(2026, 11, 8, 9, 56, tzinfo=UTC)
    assert (first.bright_ti4_k, first.bright_ti5_k, first.frp_mw) == (304.5, 274.76, 1.14)
    assert (first.satellite, first.confidence, first.version, first.daynight) == (
        "N21",
        "nominal",
        "2.0NRT",
        "N",
    )
    assert {h.satellite for h in hotspots_n21} == {"N21"}
    assert {h.confidence for h in hotspots_n21} == {"high", "low", "nominal"}


def test_firms_noaa21_ids_stable_and_distinct_from_noaa20(hotspots_n21):
    again = parsers.parse_firms_csv(_n21("firms-viirs-noaa21-ca-20261109.csv"))
    ids = [h.entity_id for h in hotspots_n21]
    assert ids == [h.entity_id for h in again]
    assert len(set(ids)) == len(ids)
    # lon -120.94685 formats to 4 dp as -120.9468 (binary float, round-half-even), so the id is stable.
    assert hotspots_n21[0].entity_id == "lw-firms-N21-2026-11-080956-40.0031--120.9468"
    # Same pixel coordinates and acquisition time, different satellite: two entities, no dedupe.
    h = hotspots_n21[0]
    twin = replace(h, satellite="N20")
    assert twin.entity_id != h.entity_id
    payloads, skipped = select_hotspots([h, twin], CONUS, FIRMS_CAPTURED)
    assert [p["entity_id"] for p in payloads] == [h.entity_id, twin.entity_id]
    assert not skipped
    wire = assert_contract(builders.build_hotspot_entity(h, FIRMS_CAPTURED), FIRMS_CAPTURED)
    assert wire["ontology"]["specificType"] == "VIIRS N21"
    assert "satellite: N21\n" in wire["description"]


def test_firms_noaa21_aoi_filter_matches_independent_count(hotspots_n21):
    payloads, skipped = select_hotspots(hotspots_n21, LA, FIRMS_CAPTURED)
    rows = list(csv.DictReader(io.StringIO(_n21("firms-viirs-noaa21-ca-20261109.csv"))))
    expected = sum(
        1
        for r in rows
        if LA.min_lat <= float(r["latitude"]) <= LA.max_lat
        and LA.min_lon <= float(r["longitude"]) <= LA.max_lon
    )
    assert expected == 33 and len(payloads) == expected
    assert skipped == Counter(out_of_aoi=len(rows) - expected)
    assert all(p["entity_id"].startswith("lw-firms-N21-") for p in payloads)


def test_collect_firms_fetches_noaa21_without_colliding_satellite_codes(monkeypatch):
    """The collector fetches all three FIRMS files and keeps each CSV satellite code verbatim."""
    header = _n21("firms-viirs-noaa21-ca-20261109.csv").splitlines()[0] + "\n"
    served = {
        sources.FIRMS_URLS["N21"]: _n21("firms-viirs-noaa21-ca-20261109.csv"),
        sources.FIRMS_URLS["N20"]: header,
        sources.FIRMS_URLS["N"]: header,
    }
    calls: list[str] = []

    def fake_fetch_text(url, params=None, ttl_s=300, cache_dir=None, **_):
        calls.append(url)
        return Fetched(served[url], FIRMS_CAPTURED, from_cache=False, stale=False)

    monkeypatch.setattr(sources, "fetch_text", fake_fetch_text)
    batch = sources.collect_firms(CALIFORNIA, 60)
    assert calls == [sources.FIRMS_URLS["N20"], sources.FIRMS_URLS["N21"], sources.FIRMS_URLS["N"]]
    assert batch.received == 122
    assert {p["entity_id"].split("-")[2] for p in batch.payloads} == {"N21"}
    assert [f.url for f in batch.fetches] == calls


# ---------------------------------------------------------------- WFIGS incidents


def test_wfigs_incidents_parse_and_normalize_ids(wfigs):
    assert len(wfigs) == 82
    assert all(i.irwin_id and "{" not in i.irwin_id and i.irwin_id == i.irwin_id.upper() for i in wfigs)
    first = wfigs[0]
    assert first.irwin_id == "0AE0858B-9086-4CB9-8010-F9C383F6AB61"
    assert first.name == "BIG CREEK VMP RX BROADCAST BURN"
    assert first.size_acres == 0.01 and first.percent_contained is None
    assert first.discovered == datetime.fromtimestamp(1767364012, tz=UTC)
    assert (first.lat, first.lon) == (40.5533501005482, -123.142665684026)
    # Two separate incidents named ARROW must stay two entities.
    arrows = {i.entity_id for i in wfigs if i.name == "ARROW"}
    assert len(arrows) == 2


def test_wfigs_aoi_selection_and_mapping(wfigs):
    now = datetime(2026, 11, 9, 19, 20, 5, tzinfo=UTC)
    payloads, skipped = select_wfigs_incidents(wfigs, LA, now)
    raw = _json("wfigs-incidents-ca-20261109.geojson")["features"]
    expected = sum(
        1
        for f in raw
        if LA.min_lat <= f["geometry"]["coordinates"][1] <= LA.max_lat
        and LA.min_lon <= f["geometry"]["coordinates"][0] <= LA.max_lon
    )
    assert len(payloads) == expected and skipped["out_of_aoi"] == len(raw) - expected
    by_id = {p["entity_id"]: p for p in payloads}
    arrow = next(i for i in wfigs if i.name == "ARROW" and i.size_acres == 0.01)
    wire = assert_contract(by_id[arrow.entity_id], now)
    assert wire["entityId"] == f"lw-wfigs-{arrow.irwin_id}"
    assert wire["aliases"]["name"] == f"ARROW ({arrow.unique_fire_id})"
    assert {"id": arrow.irwin_id, "type": "ALT_ID_TYPE_TRACK_ID_1"} in wire["aliases"]["alternateIds"]
    assert {"id": arrow.unique_fire_id, "type": "ALT_ID_TYPE_TRACK_ID_2"} in wire["aliases"]["alternateIds"]
    assert wire["ontology"]["platformType"] == "Wildfire Incident"
    assert wire["ontology"]["specificType"] == "Wildfire"
    assert "size_acres: 0.01" in wire["description"]
    assert wire["provenance"]["sourceId"] == arrow.irwin_id


def test_wfigs_feature_without_point_falls_back_to_initial_lat_lon():
    data = _json("wfigs-incidents-ca-20261109.geojson")
    feature = data["features"][0]
    feature["geometry"] = None
    (inc,) = parsers.parse_wfigs_incidents({"type": "FeatureCollection", "features": [feature]})
    assert (inc.lat, inc.lon) == (
        feature["properties"]["InitialLatitude"],
        feature["properties"]["InitialLongitude"],
    )


# ---------------------------------------------------------------- WFIGS perimeters


def test_perimeter_multipolygon_becomes_canonical_parts(perimeters_ca):
    (timber,) = perimeters_ca
    assert timber.irwin_id == "51374D2C-D96B-40F7-940B-6CB8E0A483A2"
    assert len(timber.parts) == 10
    # The source delivers clockwise exteriors (ArcGIS); Lattice needs RFC 7946 counter-clockwise.
    assert signed_area(timber.parts[0][0]) < 0
    now = datetime(2026, 11, 9, 19, 20, 56, tzinfo=UTC)
    payloads = builders.build_perimeter_entities(timber, now)
    ids = [p["entity_id"] for p in payloads]
    base = "lw-perim-51374D2C-D96B-40F7-940B-6CB8E0A483A2"
    assert ids[0] == base and ids[1:] == [f"{base}-{n}" for n in range(2, 11)]
    wires = [assert_contract(p, now) for p in payloads]
    assert len(_rings(wires[0])[0]) == 458  # largest part first
    assert wires[0]["geoDetails"]["type"] == "GEO_TYPE_HAZARD"
    assert wires[0]["ontology"]["platformType"] == "Wildfire Perimeter"
    assert "gis_acres: 25349.505" in wires[0]["description"]
    assert ids == [p["entity_id"] for p in builders.build_perimeter_entities(timber, now)]


def test_perimeter_aoi_selection(perimeters_ca):
    now = datetime(2026, 11, 9, 19, 20, 56, tzinfo=UTC)
    payloads, skipped = select_perimeters(perimeters_ca, LA, now)
    assert payloads == [] and skipped == Counter(out_of_aoi=1)  # Timber is in Monterey County
    payloads, skipped = select_perimeters(perimeters_ca, CALIFORNIA, now)
    assert len(payloads) == 10 and not skipped


def test_perimeter_national_sample_ids_unique_and_canonical(perimeters_us):
    now = datetime(2026, 11, 9, 19, 20, 56, tzinfo=UTC)
    payloads, skipped = select_perimeters(perimeters_us, CONUS, now)
    ids = [p["entity_id"] for p in payloads]
    assert len(ids) == len(set(ids)) and "duplicate_id" not in skipped
    assert {p["entity_id"].split("-", 2)[2][:36] for p in payloads} == {p.irwin_id for p in perimeters_us}
    for p in payloads:
        assert_contract(p, now)


def test_collect_perimeters_requests_generalization_offset(monkeypatch):
    """The perimeter query carries maxAllowableOffset=0.0005 (degrees, outSR 4326) plus the AOI envelope."""
    params_seen: list[dict] = []

    def fake_fetch_json(url, params=None, ttl_s=300, cache_dir=None, **_):
        params_seen.append(dict(params or {}))
        return Fetched(
            {"type": "FeatureCollection", "features": []}, FIRMS_CAPTURED, from_cache=False, stale=False
        )

    monkeypatch.setattr("lattice_wildfire.core.http.fetch_json", fake_fetch_json)
    batch = sources.collect_perimeters(LA, 60)
    assert len(params_seen) == 1
    params = params_seen[0]
    assert params["maxAllowableOffset"] == sources.PERIMETER_MAX_ALLOWABLE_OFFSET_DEG == 0.0005
    assert params["geometry"] == ",".join(str(v) for v in LA.envelope())
    assert params["geometryType"] == "esriGeometryEnvelope" and params["outSR"] == 4326
    assert batch.received == 0 and batch.fetches[0].url == sources.WFIGS_PERIMETERS_LAYER


def test_orient_part_handles_holes_and_degenerates():
    exterior_cw = [
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
        (0.0, 0.0),
    ]  # lat, lon -> clockwise in lon/lat
    hole_ccw = [(0.2, 0.2), (0.2, 0.8), (0.8, 0.8), (0.8, 0.2), (0.2, 0.2)]
    part = orient_part([exterior_cw, hole_ccw])
    assert signed_area(part[0]) > 0 and signed_area(part[1]) < 0
    assert orient_part([[(0.0, 0.0), (1.0, 1.0), (0.0, 0.0)]]) is None
    assert orient_part([[(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (0.0, 0.0)]]) is None  # collinear
    _details, shape = builders.geo_polygon(part)
    assert len(shape.polygon.rings) == 2


# ---------------------------------------------------------------- NWS red flag


def test_red_flag_filter_on_real_feeds():
    assert parsers.parse_nws_alerts(_json("nws-alerts-red-flag-us-20261109.json")) == []
    assert parsers.parse_nws_alerts(_json("nws-alerts-ca-20261109.json")) == []  # no Red Flag in CA that day
    every = parsers.parse_nws_alerts(_json("nws-alerts-ca-20261109.json"), event=None)
    assert len(every) == 17
    payloads, skipped = select_red_flags(every, CALIFORNIA, datetime(2026, 11, 9, 19, 20, 7, tzinfo=UTC))
    assert payloads == [] and skipped == Counter(no_geometry=17)


def test_alert_polygons_counted_ended_and_canonical(geo_alerts):
    assert len(geo_alerts) == 20
    with_geometry = [a for a in geo_alerts if a.parts]
    assert len(with_geometry) == 12
    assert all(signed_area(a.parts[0][0]) < 0 for a in with_geometry)  # NWS sends clockwise rings
    now = NWS_GEOMETRY_CAPTURED
    ended = sum(1 for a in geo_alerts if a.end_time is not None and a.end_time <= now)
    ended_with_geometry = sum(1 for a in with_geometry if a.end_time is not None and a.end_time <= now)
    payloads, skipped = select_red_flags(geo_alerts, CONUS, now)
    assert skipped["ended"] == ended
    assert skipped["no_geometry"] == 8 - (ended - ended_with_geometry)
    assert len(payloads) == 12 - ended_with_geometry
    for p in payloads:
        assert_contract(p, now)


def test_alert_expiry_is_capped_by_alert_end(geo_alerts):
    now = NWS_GEOMETRY_CAPTURED
    live = [a for a in geo_alerts if a.parts and a.end_time and a.end_time > now]
    soon = min(live, key=lambda a: a.end_time)
    late = max(live, key=lambda a: a.end_time)
    assert soon.end_time < now + timedelta(hours=1) < late.end_time
    (p_soon,) = builders.build_red_flag_entities(soon, now)
    (p_late,) = builders.build_red_flag_entities(late, now)
    assert p_soon["expiry_time"] == soon.end_time
    assert p_late["expiry_time"] == now + timedelta(hours=1)
    assert p_soon["entity_id"] == f"lw-nws-{builders.alert_key(soon.alert_id)}"
    assert re.fullmatch(r"lw-nws-[0-9a-f]{16}", p_soon["entity_id"])
    with pytest.raises(ValueError, match="ended"):
        builders.build_red_flag_entities(soon, soon.end_time)


# ---------------------------------------------------------------- NWS zones (zone-based alerts)


def _zone(name: str, url: str):
    return parsers.parse_nws_zone(json.loads((ZONES / name).read_text()), url)


def _ventura_high_surf():
    alerts = parsers.parse_nws_alerts(_json("nws-alerts-ca-20261109.json"), event=None)
    (alert,) = [
        a for a in alerts if a.event == "High Surf Advisory" and a.area_desc == "Ventura County Beaches"
    ]
    assert alert.parts == [] and alert.affected_zones == [VENTURA_ZONE_URL]
    return alert


def test_nws_zone_fixtures_parse():
    fire = _zone("nws-zone-fire-CAZ288-20261109.json", FIRE_ZONE_URL)
    assert (fire.zone_id, fire.zone_type, fire.name, fire.url) == (
        "CAZ288",
        "fire",
        "Santa Clarita Valley",
        FIRE_ZONE_URL,
    )
    assert len(fire.parts) == 1 and len(fire.parts[0][0]) == 125
    assert signed_area(fire.parts[0][0]) < 0  # api.weather.gov sends clockwise rings
    ventura = _zone("nws-zone-forecast-CAZ354-20261109.json", VENTURA_ZONE_URL)
    assert (ventura.zone_id, ventura.zone_type, len(ventura.parts)) == ("CAZ354", "public", 6)
    with pytest.raises(ValueError, match="Feature"):
        parsers.parse_nws_zone(_json("nws-alerts-ca-20261109.json"))


def test_zone_based_alert_publishes_canonical_zone_polygons():
    alert = _ventura_high_surf()
    zone = _zone("nws-zone-forecast-CAZ354-20261109.json", VENTURA_ZONE_URL)
    payloads, skipped = select_red_flags([alert], CALIFORNIA, NWS_CA_CAPTURED, {VENTURA_ZONE_URL: zone})
    base = f"lw-nws-{builders.alert_key(alert.alert_id)}-CAZ354"
    assert [p["entity_id"] for p in payloads] == [base] + [f"{base}-{n}" for n in range(2, 7)]
    assert not skipped
    for p in payloads:
        wire = assert_contract(p, NWS_CA_CAPTURED)
        assert "zone: CAZ354 (Ventura County Beaches)" in wire["description"]
        assert wire["geoDetails"]["type"] == "GEO_TYPE_HAZARD"
    assert payloads[0]["aliases"].name == "High Surf Advisory: Ventura County Beaches (part 1/6)"
    # Without zone resolution the same alert has nothing to draw.
    assert select_red_flags([alert], CALIFORNIA, NWS_CA_CAPTURED) == ([], Counter(no_geometry=1))


def test_fire_zone_alert_lands_in_la_aoi():
    # Real alert record pointed at a real fire weather zone (no Red Flag Warning was active at capture).
    alert = replace(_ventura_high_surf(), affected_zones=[FIRE_ZONE_URL, VENTURA_ZONE_URL])
    zones = {
        FIRE_ZONE_URL: _zone("nws-zone-fire-CAZ288-20261109.json", FIRE_ZONE_URL),
        VENTURA_ZONE_URL: _zone("nws-zone-forecast-CAZ354-20261109.json", VENTURA_ZONE_URL),
    }
    payloads, skipped = select_red_flags([alert], LA, NWS_CA_CAPTURED, zones)
    base = f"lw-nws-{builders.alert_key(alert.alert_id)}"
    # The largest Ventura beaches part reaches lon -118.9436, inside the AOI edge (-118.95); its five
    # small offshore parts near lon -119.22 / -119.44 do not, so only part 1 of CAZ354 survives.
    assert [p["entity_id"] for p in payloads] == [f"{base}-CAZ288", f"{base}-CAZ354"]
    ring = _rings(assert_contract(payloads[0], NWS_CA_CAPTURED))[0]
    assert len(ring) == 125 and signed_area(ring) > 0
    assert len(_rings(assert_contract(payloads[1], NWS_CA_CAPTURED))[0]) == 537
    assert not skipped


def test_zone_fetch_failure_is_counted_not_fatal():
    alert = _ventura_high_surf()
    payloads, skipped = select_red_flags([alert], CALIFORNIA, NWS_CA_CAPTURED, {VENTURA_ZONE_URL: None})
    assert payloads == [] and skipped == Counter(zone_fetch_failed=1, no_geometry=1)


def test_collect_redflag_resolves_zones_and_survives_failures(monkeypatch):
    """Collector with the network edge replaced by captured files: one zone loads, one fails."""
    doc = _json("nws-alerts-ca-20261109.json")
    picked = {
        ("High Surf Advisory", "Ventura County Beaches"),
        ("Coastal Flood Advisory", "Northern Coastal Humboldt"),
    }
    features = [
        f for f in doc["features"] if (f["properties"]["event"], f["properties"]["areaDesc"]) in picked
    ]
    for f in features:  # relabel: no Red Flag Warning was active at capture
        f["properties"]["event"] = parsers.RED_FLAG_EVENT
    doc["features"] = features
    zone_doc = json.loads((ZONES / "nws-zone-forecast-CAZ354-20261109.json").read_text())
    calls: list[str] = []

    def fake_fetch_json(url, params=None, ttl_s=300, cache_dir=None, **_):
        calls.append(url)
        if url == sources.NWS_ALERTS_URL:
            return Fetched(doc, NWS_CA_CAPTURED, from_cache=False, stale=False)
        if url == VENTURA_ZONE_URL:
            assert ttl_s == sources.NWS_ZONE_TTL_S
            return Fetched(zone_doc, NWS_CA_CAPTURED, from_cache=False, stale=False)
        raise FetchError(f"fetch failed for {url}: ConnectError")

    monkeypatch.setattr(sources, "fetch_json", fake_fetch_json)
    batch = sources.collect_redflag(CALIFORNIA, 60, now=NWS_CA_CAPTURED)
    assert batch.received == 2
    assert calls == [
        sources.NWS_ALERTS_URL,
        VENTURA_ZONE_URL,
        "https://api.weather.gov/zones/forecast/CAZ103",
    ]
    assert len(batch.payloads) == 6
    assert batch.skipped == Counter(zone_fetch_failed=1, no_geometry=1)
    assert [f.url for f in batch.fetches] == [sources.NWS_ALERTS_URL, VENTURA_ZONE_URL]


# ---------------------------------------------------------------- CAL FIRE


def test_calfire_parse_and_inactive_skipped(calfire):
    assert len(calfire) == 567
    first = calfire[0]
    assert first.unique_id == "8f9a3173-0ea3-46c2-a4f9-8b254990c187"
    assert first.started == datetime(2026, 1, 24, 22, 17, 6, tzinfo=UTC)
    assert first.acres_burned == 10.0 and first.percent_contained == 100.0
    payloads, skipped = select_calfire_incidents(calfire, CALIFORNIA, datetime(2026, 11, 9, tzinfo=UTC))
    assert payloads == [] and skipped == Counter(inactive=567)  # none active at capture


def test_calfire_entity_mapping(calfire):
    now = datetime(2026, 11, 9, 19, 20, 8, tzinfo=UTC)
    inc = next(i for i in calfire if i.county == "Los Angeles")
    wire = assert_contract(builders.build_calfire_entity(inc, now), now)
    assert wire["entityId"] == f"lw-calfire-{inc.unique_id}"
    assert wire["aliases"]["name"] == inc.name
    assert wire["ontology"]["platformType"] == "Wildfire Incident"
    assert inc.name == "Crown Fire" and "acres_burned: 385\n" in wire["description"]
    assert f"url: {inc.url}" in wire["description"]
    assert wire["createdTime"] == inc.started.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- dry run and CLI


def test_publish_batch_dry_run_writes_wire_json(hotspots):
    payloads, skipped = select_hotspots(hotspots, LA, FIRMS_CAPTURED)
    out = io.StringIO()
    report = publish_batch(None, Batch("firms", len(hotspots), payloads, skipped), out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(lines) == len(payloads) and report.published == 0 and report.selected == len(payloads)
    assert lines[0]["source"] == "firms"
    assert lines[0]["entity"]["entityId"] == payloads[0]["entity_id"]
    assert lines[0]["entity"]["provenance"]["dataType"] == "firms-viirs-hotspot"


def test_cycle_report_times_each_source_separately(hotspots):
    """``elapsed_s`` is wall time per source, present on the success and the failure path alike."""
    payloads, skipped = select_hotspots(hotspots, LA, FIRMS_CAPTURED)
    ok = Batch("firms", len(hotspots), payloads, skipped)
    perim = Batch("perimeters", 0, [], Counter())

    def slow(aoi, ttl):
        time.sleep(0.1)
        return ok

    def failed(aoi, ttl):
        time.sleep(0.05)
        raise FetchError("perimeters layer down")

    def instant(aoi, ttl):
        return perim

    perimeters, wfigs, firms = run_cycle(
        None,
        ["firms", "wfigs", "perimeters"],
        LA,
        60,
        collectors={"firms": slow, "wfigs": failed, "perimeters": instant},
    )
    assert firms.elapsed_s >= 0.1
    assert wfigs.errors == ["collect failed: FetchError: perimeters layer down"]
    assert wfigs.elapsed_s >= 0.05, "a failed source is timed too"
    assert perimeters.elapsed_s < 0.05, "per-source, not cumulative: the cycle already spent 0.15 s"
    summary = json.loads(json.dumps(cycle_summary([perimeters, wfigs, firms], dry_run=True)))
    assert [s["source"] for s in summary["sources"]] == ["perimeters", "wfigs", "firms"]
    assert [s["elapsed_s"] for s in summary["sources"]] == [
        round(r.elapsed_s, 3) for r in (perimeters, wfigs, firms)
    ]
    assert all(isinstance(s["elapsed_s"], float) and s["elapsed_s"] >= 0 for s in summary["sources"])


def test_cli_rejects_bad_arguments(capsys):
    with pytest.raises(SystemExit):
        feeds_main(["--sources", "firms,nope"])
    assert feeds_main(["--dry-run", "--interval", "600", "--sources", "stations"]) == 2
    assert "--interval" in capsys.readouterr().err


# ---------------------------------------------------------------- harness round-trip


def _fixture_batches(now: datetime, stations) -> dict[str, Batch]:
    hs = parsers.parse_firms_csv(_text("firms-viirs-noaa20-ca-20261109.csv"))
    inc = parsers.parse_wfigs_incidents(_json("wfigs-incidents-ca-20261109.geojson"))
    per = parsers.parse_wfigs_perimeters(_json("wfigs-perimeters-ca-20261109.geojson"))
    # The fixture alerts ended on 2026-11-09; keep them live relative to ``now`` so the harness accepts them.
    live_until = now + timedelta(hours=2)
    alerts = [
        replace(a, ends=live_until, expires=live_until)
        for a in parsers.parse_nws_alerts(_json("nws-alerts-with-geometry-20261109.json"), event=None)
    ]
    cal = [
        i
        for i in parsers.parse_calfire_incidents(_json("calfire-incidents-2026-20261109.json"))
        if i.county == "Los Angeles"
    ]
    return {
        "firms": Batch("firms", len(hs), *select_hotspots(hs, CALIFORNIA, now)),
        "wfigs": Batch("wfigs", len(inc), *select_wfigs_incidents(inc, CALIFORNIA, now)),
        "perimeters": Batch("perimeters", len(per), *select_perimeters(per, CALIFORNIA, now)),
        "redflag": Batch("redflag", len(alerts), *select_red_flags(alerts, CONUS, now)),
        # CAL FIRE had no active incident at capture; publish LA County records directly to prove shape.
        "calfire": Batch("calfire", len(cal), [builders.build_calfire_entity(i, now) for i in cal]),
        "stations": Batch("stations", len(stations), *select_stations(stations, LA, now)),
    }


@pytest.fixture(scope="module")
def stations():
    (path,) = sorted((Path(__file__).parent / "fixtures" / "core").glob("lacounty_fire_stations_*.geojson"))
    return facilities_from_geojson(json.loads(path.read_text()), "fire_station")


def test_stations_from_county_layer(stations):
    now = datetime(2026, 11, 9, 19, 30, tzinfo=UTC)
    payloads, skipped = select_stations(stations, LA, now)
    ids = [p["entity_id"] for p in payloads]
    assert payloads and len(ids) == len(set(ids))
    assert all(re.fullmatch(r"lw-station-(\d+[A-Za-z]?|oid-\d+)", i) for i in ids)
    assert len(payloads) + sum(skipped.values()) == len(stations)
    wire = assert_contract(payloads[0], now)
    assert wire["ontology"] == {"template": "TEMPLATE_ASSET", "platformType": "Fire Station"}
    assert wire["milView"]["disposition"] == "DISPOSITION_FRIENDLY"
    assert wire["health"]["healthStatus"] == "HEALTH_STATUS_HEALTHY"
    assert wire["health"]["connectionStatus"] == "CONNECTION_STATUS_ONLINE"
    reordered = select_stations(list(reversed(stations)), LA, now)[0]
    assert [(p["entity_id"], p["location"]) for p in reordered] == [
        (p["entity_id"], p["location"]) for p in payloads
    ]


def test_duplicate_station_selection_keeps_record_with_highest_object_id():
    older = Facility(
        id="182",
        name="Station 182",
        kind="fire_station",
        lat=34.1,
        lon=-118.1,
        attrs={"OBJECTID": 100},
    )
    newer = Facility(
        id="182",
        name="Station 182",
        kind="fire_station",
        lat=34.2,
        lon=-118.2,
        attrs={"OBJECTID": 200},
    )

    now = datetime(2026, 11, 9, tzinfo=UTC)
    for records in ([older, newer], [newer, older]):
        payloads, skipped = select_stations(records, LA, now)
        assert [payload["entity_id"] for payload in payloads] == ["lw-station-182"]
        assert payloads[0]["location"].position.latitude_degrees == 34.2
        assert skipped == Counter(duplicate_id=1)


def test_harness_accepts_every_payload_and_reads_back(lattice, stations):
    now = datetime.now(UTC)
    batches = _fixture_batches(now, stations)
    reports = run_cycle(
        lattice,
        list(batches),
        CALIFORNIA,
        60,
        collectors={k: (lambda b: lambda aoi, ttl: b)(v) for k, v in batches.items()},
    )
    by_source = {r.source: r for r in reports}
    for name, batch in batches.items():
        report = by_source[name]
        assert report.errors == [], f"{name}: {report.errors[:3]}"
        assert report.published == len(batch.payloads) > 0, name

    for batch in batches.values():
        sent = batch.payloads[0]
        got = lattice.entities.get_entity(sent["entity_id"])
        assert got.entity_id == sent["entity_id"]
        assert got.is_live is True
        assert got.provenance.data_type == sent["provenance"].data_type
        assert got.provenance.integration_name == "lattice-wildfire-feeds"
        assert got.aliases.name == sent["aliases"].name
        assert got.ontology.template == sent["ontology"].template
        assert got.description == sent["description"]
        if sent["ontology"].template == "TEMPLATE_GEO":
            sent_ring = sent["geo_shape"].polygon.rings[0].positions
            got_ring = got.geo_shape.polygon.rings[0].positions
            assert [(p.position.latitude_degrees, p.position.longitude_degrees) for p in got_ring] == [
                (p.position.latitude_degrees, p.position.longitude_degrees) for p in sent_ring
            ]
        else:
            assert got.location.position.latitude_degrees == sent["location"].position.latitude_degrees
            assert got.mil_view.disposition == sent["mil_view"].disposition


def test_heartbeat_republish_extends_expiry(lattice, hotspots):
    first_now = datetime.now(UTC)
    h = hotspots[0]
    lattice.entities.publish_entity(**builders.build_hotspot_entity(h, first_now))
    before = lattice.entities.get_entity(h.entity_id).expiry_time
    later = first_now + timedelta(minutes=5)
    lattice.entities.publish_entity(**builders.build_hotspot_entity(h, later))
    after = lattice.entities.get_entity(h.entity_id)
    assert after.expiry_time > before
    assert after.expiry_time == later + timedelta(hours=6)


# ---------------------------------------------------------------- publish order and readiness marker


def _recorder(name: str, calls: list[str], stale: bool | None = True):
    def collect(aoi, ttl):
        calls.append(name)
        fetches = [FetchInfo("https://feed.test", datetime.now(UTC), False, stale)] if stale else []
        return Batch(name, 0, [], Counter(), fetches)

    return collect


def _failing(aoi, ttl):
    raise FetchError("feed down")


def test_cycle_publishes_context_sources_before_hotspots():
    calls: list[str] = []
    names = ["firms", "stations", "redflag", "wfigs", "calfire", "perimeters"]
    run_cycle(None, names, LA, 60, collectors={n: _recorder(n, calls) for n in names})
    assert calls == ["perimeters", "wfigs", "calfire", "redflag", "stations", "firms"]
    assert publication_order(["firms", "wfigs"]) == ["wfigs", "firms"]
    assert publication_order(["firms"]) == ["firms"]


def test_ready_path_needs_lw_state_dir():
    assert ready_path({}) is None
    assert ready_path({"LW_STATE_DIR": ""}) is None
    assert ready_path({"LW_STATE_DIR": "/tmp/lw"}) == Path("/tmp/lw/feeds/ready.json")


def test_ready_marker_written_after_a_clean_cycle_that_used_a_stale_cache(lattice, monkeypatch, tmp_path):
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path))
    marker = tmp_path / "feeds" / "ready.json"
    assert (
        feeds_run(
            lattice,
            ["perimeters"],
            LA,
            interval_s=60,
            once=True,
            out=io.StringIO(),
            report_out=io.StringIO(),
            collectors={"perimeters": _recorder("perimeters", [])},
        )
        == 0
    )
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert set(document) == {"at", "sources"}
    assert document["at"].endswith("Z")
    assert document["sources"] == {"perimeters": {"published": 0, "stale": True}}

    # The marker records the first clean cycle only: a later failing cycle leaves it untouched.
    before = marker.read_text(encoding="utf-8")
    assert (
        feeds_run(
            lattice,
            ["perimeters"],
            LA,
            interval_s=60,
            once=True,
            out=io.StringIO(),
            report_out=io.StringIO(),
            collectors={"perimeters": _failing},
        )
        == 1
    )
    assert marker.read_text(encoding="utf-8") == before


def test_ready_marker_not_written_for_a_failed_cycle(lattice, monkeypatch, tmp_path):
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path))
    assert (
        feeds_run(
            lattice,
            ["perimeters"],
            LA,
            interval_s=60,
            once=True,
            out=io.StringIO(),
            report_out=io.StringIO(),
            collectors={"perimeters": _failing},
        )
        == 1
    )
    assert not (tmp_path / "feeds" / "ready.json").exists()


def test_dry_run_never_writes_the_ready_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("LW_STATE_DIR", str(tmp_path))
    assert (
        feeds_run(
            None,
            ["perimeters"],
            LA,
            interval_s=60,
            once=True,
            out=io.StringIO(),
            report_out=io.StringIO(),
            collectors={"perimeters": _recorder("perimeters", [])},
        )
        == 0
    )
    assert not (tmp_path / "feeds" / "ready.json").exists()
