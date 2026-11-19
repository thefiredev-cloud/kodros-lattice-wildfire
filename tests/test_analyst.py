"""Kodros Analyst: pure fact gathering, the brief verifier, and the task lifecycle on the harness.

Network inputs are real responses captured 2026-11-09 (tests/fixtures/analyst/README.md) replayed
through ``FixtureSources``, so URL building and parsing run exactly as in production. Lattice is the
contract harness, read back through the real SDK client in separate calls. Capacity is proven on the
harness by counting concurrent fetches: with capacity N, N tasks run and the N+1st stays SENT.
"""

from __future__ import annotations

import csv
import json
import logging
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from anduril import (
    Aliases,
    EntityIdsSelector,
    GoogleProtobufAny,
    MilView,
    Ontology,
    Principal,
    Relations,
    System,
    Tracked,
)

from lattice_wildfire.analyst import agent as agent_mod
from lattice_wildfire.analyst.agent import (
    DEFAULT_CAPACITY,
    INVESTIGATE_URL,
    AgentHealth,
    AnalystAgent,
    build_agent_entity,
    capacity_from_env,
)
from lattice_wildfire.analyst.brief import (
    LABELS,
    NARRATIVE_OK,
    NARRATIVE_REJECTED,
    NARRATIVE_UNAVAILABLE,
    apply_glossary,
    build_messages,
    fact_lines,
    narrative_footer,
    object_paths,
    parse_narrative,
    prompt_facts,
    render_markdown,
    unsupported_reasons,
    verify_narrative,
)
from lattice_wildfire.analyst.facts import (
    NearbyEntity,
    Target,
    active_alerts,
    cardinal,
    fhsz_status,
    gather_facts,
    nearby_fire_activity,
    nearest_facilities,
)
from lattice_wildfire.analyst.gather import (
    NWS_ALERTS_URL,
    fetch_inputs,
    write_narrative,
)
from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.core.entities import expiry, point_location, provenance, symbology, unclassified
from lattice_wildfire.core.geo import haversine_m
from lattice_wildfire.core.http import Fetched
from lattice_wildfire.core.layers import FHSZ_URL, FIRE_STATIONS_URL, HOSPITALS_URL, Facility
from lattice_wildfire.core.llm import ChatResult, LLMUnavailable
from lattice_wildfire.planner.snapshot import capacity_from_description

FIX = Path(__file__).parent / "fixtures" / "analyst"
FEEDS = Path(__file__).parent / "fixtures" / "feeds"
CAPTURED = {
    "points": "2026-11-09T19:24:53+00:00",
    "forecast": "2026-11-09T19:24:53+00:00",
    "alerts": "2026-11-09T19:24:54+00:00",
    "layers": "2026-11-09T19:33:00+00:00",
}
POINT = (34.15232, -118.19506)


# --------------------------------------------------------------------------- fixtures


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def _at(key: str) -> datetime:
    return datetime.fromisoformat(CAPTURED[key])


class FixtureSources:
    """Replays captured responses by URL. Unknown URLs raise, so a URL-building bug fails loudly.

    ``gate``: when set, the forecast fetch signals ``entered`` and blocks until the gate opens,
    which pins a task worker inside EXECUTING for lifecycle tests.
    """

    def __init__(self, *, fail: set[str] = frozenset(), gated: bool = False) -> None:
        self.fail = set(fail)
        self.calls: list[tuple[str, Any]] = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        if not gated:
            self.gate.set()

    def json(self, url: str, params: dict | None, ttl_s: float) -> Fetched:
        self.calls.append((url, params))
        if url == "https://api.weather.gov/points/34.1523,-118.1951" and params is None:
            return self._ok("points", _load("nws-points-eaglerock-20261109.json"))
        if url == "https://api.weather.gov/gridpoints/LOX/158,49/forecast" and params is None:
            self.entered.set()
            if not self.gate.wait(30):
                raise TimeoutError("test gate never opened")
            return self._ok("forecast", _load("nws-forecast-eaglerock-20261109.json"))
        if url == NWS_ALERTS_URL and params == {"point": "34.1523,-118.1951"}:
            return self._ok("alerts", _load("nws-alerts-eaglerock-20261109.json"))
        raise KeyError(f"no fixture for {url} {params}")

    def arcgis(self, layer_url: str, **query: Any) -> Fetched:
        self.calls.append((layer_url, query))
        if layer_url == FHSZ_URL and query.get("point") == POINT:
            return self._ok("fhsz", _load("fhsz-eaglerock-20261109.geojson"), "layers")
        box = query.get("geometry_bbox")
        if box is None or abs(box.min_lat - (POINT[0] - 0.2)) > 1e-9:
            raise KeyError(f"no fixture for {layer_url} box {box}")  # only the near box was captured
        if layer_url == FIRE_STATIONS_URL and "Los Angeles County Fire Department" in query.get("where", ""):
            return self._ok("stations", _load("lacofd-stations-eaglerock-20261109.geojson"), "layers")
        if layer_url == HOSPITALS_URL:
            return self._ok("hospitals", _load("hospitals-eaglerock-20261109.geojson"), "layers")
        raise KeyError(f"no fixture for {layer_url} {query}")

    def _ok(self, name: str, data: Any, captured: str | None = None) -> Fetched:
        if name in self.fail:
            raise ConnectionError(f"{name} unreachable")
        return Fetched(data, _at(captured or name), from_cache=False, stale=False)


def _firms_row(lat: str, lon: str) -> dict[str, str]:
    with (FEEDS / "firms-viirs-noaa20-ca-20261109.csv").open() as fh:
        for row in csv.DictReader(fh):
            if row["latitude"] == lat and row["longitude"] == lon:
                return row
    raise LookupError(f"FIRMS row {lat},{lon} not in fixture")


def _hotspot_id(row: dict[str, str]) -> str:
    return (
        f"lw-firms-{row['satellite']}-{row['acq_date']}{row['acq_time']}"  # CONTRACT pattern, as Feeds publishes it
        f"-{float(row['latitude']):.4f}-{float(row['longitude']):.4f}"
    )


def publish_hotspot(client: Any, row: dict[str, str]) -> str:
    """Publish a real FIRMS detection the way Fireline Feeds shapes it (CONTRACT vocabulary)."""
    acquired = datetime.strptime(f"{row['acq_date']} {row['acq_time']}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
    entity_id = _hotspot_id(row)
    client.entities.publish_entity(
        entity_id=entity_id,
        is_live=True,
        expiry_time=expiry(3600),
        aliases=Aliases(name=f"VIIRS {row['satellite']} hotspot {row['acq_date']} {row['acq_time']}Z"),
        location=point_location(float(row["latitude"]), float(row["longitude"])),
        ontology=Ontology(template="TEMPLATE_TRACK", platform_type="Wildfire Hotspot"),
        mil_view=MilView(disposition="DISPOSITION_UNKNOWN", environment="ENVIRONMENT_LAND"),
        provenance=provenance("feeds", "firms-viirs-hotspot", acquired),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_UNKNOWN", "ENVIRONMENT_LAND"),
        tracked=Tracked(track_quality_wrapper=2),
        description=f"FRP {row['frp']} MW, confidence {row['confidence']}",
    )
    return entity_id


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


MODEL_ID = "DeepSeek-V4.1-Flash-1017-EXL3"
LATENCY_S = 12.5


def facts_chat(record: list, *, model: str = MODEL_ID, latency_s: float = LATENCY_S) -> Any:
    """Chat stand-in that writes six lines from the facts JSON it is sent (proves the prompt carries them)."""

    def chat_result(messages: list[dict], **kwargs: Any) -> ChatResult:
        record.append((messages, kwargs))
        body = messages[-1]["content"].split("Facts JSON:\n", 1)[1].rsplit("\n\nWrite", 1)[0]
        facts = json.loads(body)
        station = facts["resources"]["fire_stations"][0]
        text = "\n".join(
            [
                f"LOCATION: {facts['target']['name']} near {facts['location']['place']}.",
                f"FUEL/FHSZ: {facts['fuel']['fhsz']}.",
                f"WEATHER/WIND: {facts['weather']['periods'][0]['wind_direction']} "
                f"{facts['weather']['periods'][0]['wind_speed']}.",
                f"EXPOSURES: {facts['exposures']['hospitals'][0]['name']}.",
                f"NEAREST RESOURCES: {station['name']} {station['distance_km']} km {station['direction']}.",
                "UNKNOWNS: fire size unknown.",
            ]
        )
        return ChatResult(text=text, model=model, latency_s=latency_s, usage={"total_tokens": 1083})

    return chat_result


def down_chat(messages: list[dict], **kwargs: Any) -> ChatResult:
    raise LLMUnavailable("cluster LLM at http://127.0.0.1:8080/v1 failed: ConnectError: refused")


TARGET = Target(
    entity_id="lw-firms-N20-2026-11-091036-34.1523--118.1951",
    name="VIIRS N20 hotspot 2026-11-09 1036Z",
    lat=POINT[0],
    lon=POINT[1],
    data_type="firms-viirs-hotspot",
    platform_type="Wildfire Hotspot",
)


@pytest.fixture()
def captured_facts() -> dict:
    inputs = fetch_inputs(TARGET, sources=FixtureSources())
    return gather_facts(inputs, task_id="t-pure", generated_at="2026-11-09T19:40:00+00:00")


# --------------------------------------------------------------------------- pure: facts


def test_gather_facts_from_captured_sources(captured_facts: dict) -> None:
    f = captured_facts
    assert f["target"]["entity_id"] == TARGET.entity_id
    assert f["location"]["grid"] == "LOX/158,49"
    assert f["location"]["nearest_place"]["city"] == "Glendale"
    assert f["location"]["fire_weather_zone"] and f["location"]["fire_weather_zone"].startswith("CAZ")
    assert f["fuel"] == {"fhsz": "Very High", "fhsz_status": "fire hazard severity zone"}
    assert fhsz_status("NonWildland") == "mapped, not a fire hazard class"  # Pasadena flatland reads this way

    periods = f["weather"]["periods"]
    assert [p["name"] for p in periods] == ["Today", "Tonight", "Tuesday"]
    assert periods[0]["temperature"] == 78 and periods[0]["wind_speed"] == "5 mph"
    assert periods[0]["wind_direction"] == "WSW"
    assert f["weather"]["alerts"] == []  # lookup worked and found nothing: empty, not None

    for key in (("exposures", "hospitals"), ("resources", "fire_stations")):
        rows = f[key[0]][key[1]]
        assert len(rows) == 3
        assert [r["distance_km"] for r in rows] == sorted(r["distance_km"] for r in rows)
    stations = f["resources"]["fire_stations"]
    # Distances are recomputed from the parsed layer, not trusted from anywhere else.
    from lattice_wildfire.core.layers import facilities_from_geojson

    parsed = facilities_from_geojson(_load("lacofd-stations-eaglerock-20261109.geojson"), "fire_station")
    nearest = min(parsed, key=lambda s: haversine_m(POINT[0], POINT[1], s.lat, s.lon))
    assert stations[0]["id"] == nearest.id
    assert stations[0]["distance_km"] == round(
        haversine_m(POINT[0], POINT[1], nearest.lat, nearest.lon) / 1000, 1
    )

    urls = {s["name"].split(" (bbox")[0]: s for s in f["sources"]}
    assert urls["NWS forecast"]["url"] == "https://api.weather.gov/gridpoints/LOX/158,49/forecast"
    assert urls["NWS forecast"]["fetched_at"] == "2026-11-09T19:24:53+00:00"
    assert (
        urls["NWS active alerts at point"]["url"]
        == "https://api.weather.gov/alerts/active?point=34.1523%2C-118.1951"
    )
    assert all(s["error"] is None for s in f["sources"])
    assert "Satellite thermal detection only; not ground-confirmed" in f["unknowns"]
    assert f["nearby_fire_activity"]["entities"] is None  # no Lattice picture was fetched here


def test_failed_sources_become_explicit_unknowns() -> None:
    sources = FixtureSources(fail={"forecast", "fhsz", "stations"})
    facts = gather_facts(
        fetch_inputs(TARGET, sources=sources), task_id=None, generated_at="2026-11-09T19:40:00+00:00"
    )
    assert facts["weather"]["periods"] == []
    # A failed FHSZ lookup must not read as "outside any zone".
    assert facts["fuel"] == {"fhsz": None, "fhsz_status": "unavailable"}
    assert facts["resources"]["fire_stations"] is None
    assert len(facts["exposures"]["hospitals"]) == 3
    joined = " | ".join(facts["unknowns"])
    assert "NWS forecast: not available (ConnectionError: forecast unreachable)" in joined
    assert "LA County FHSZ (LRA+SRA): not available" in joined
    assert "LACoFD fire stations: not available" in joined
    lines = fact_lines(facts)
    assert (
        "forecast unavailable" in lines[2] and "lookup failed" in lines[4] and "FHSZ unavailable" in lines[1]
    )


def test_facility_search_widens_when_few_found() -> None:
    class Sparse(FixtureSources):
        def arcgis(self, layer_url: str, **query: Any) -> Fetched:
            self.calls.append((layer_url, query))
            if layer_url == FHSZ_URL:
                return self._ok("fhsz", {"type": "FeatureCollection", "features": []}, "layers")
            return self._ok("stations", {"type": "FeatureCollection", "features": []}, "layers")

    sources = Sparse()
    inputs = fetch_inputs(TARGET, sources=sources)
    boxes = [q["geometry_bbox"] for url, q in sources.calls if url == FIRE_STATIONS_URL]
    assert len(boxes) == 2 and boxes[1].max_lat - boxes[1].min_lat > boxes[0].max_lat - boxes[0].min_lat
    assert inputs.stations == []
    facts = gather_facts(inputs, task_id=None, generated_at="x")
    assert facts["fuel"] == {"fhsz": None, "fhsz_status": "not in a mapped FHSZ"}
    assert "none found in search area" in fact_lines(facts)[4]


def test_nearest_facilities_order_bearing_and_cardinal_boundaries() -> None:
    lat, lon = 34.0, -118.0
    east = Facility("e", "East", "fire_station", 34.0, -117.9)
    north = Facility("n", "North", "fire_station", 34.05, -118.0)
    tie_b = Facility("b", "TieB", "fire_station", 33.95, -118.0)
    tie_a = Facility("a", "TieA", "fire_station", 33.95, -118.0)
    rows = nearest_facilities(lat, lon, [east, tie_b, north, tie_a], n=3)
    assert [r["id"] for r in rows] == ["a", "b", "n"]  # equal distance broken by id, deterministic
    assert rows[0]["direction"] == "S" and rows[0]["bearing_deg"] == 180
    assert rows[2]["direction"] == "N" and rows[2]["bearing_deg"] == 0
    assert nearest_facilities(lat, lon, [east], n=3)[0]["direction"] == "E"
    assert nearest_facilities(lat, lon, [], n=3) == []
    assert cardinal(11.24) == "N" and cardinal(11.25) == "NNE"
    assert cardinal(348.74) == "NNW" and cardinal(348.75) == "N" and cardinal(-90) == "W"


def test_nearby_fire_activity_radius_polygon_and_self() -> None:
    def east_of(km: float) -> float:
        return POINT[1] + km / (111.32 * 0.8273)  # cos(34.15 deg)

    near = NearbyEntity("near", "Near", "wfigs-incident", POINT[0], east_of(9.9))
    far = NearbyEntity("far", "Far", "wfigs-incident", POINT[0], east_of(10.4))
    ring = ((POINT[0] - 0.01, POINT[1] - 0.01), (POINT[0] - 0.01, POINT[1] + 0.5), (POINT[0] + 0.01, POINT[1] + 0.5),
            (POINT[0] + 0.01, POINT[1] - 0.01))  # fmt: skip
    perim = NearbyEntity("perim", "Perimeter", "wfigs-perimeter", POINT[0], POINT[1] + 0.245, ring=ring)
    me = NearbyEntity(TARGET.entity_id, "self", "firms-viirs-hotspot", POINT[0], POINT[1])
    rows = nearby_fire_activity(TARGET, [far, near, perim, me])
    assert [r["entity_id"] for r in rows] == ["perim", "near"]
    assert rows[0]["target_inside_polygon"] is True and rows[0]["distance_km"] > 10
    assert rows[1]["direction"] == "E" and rows[1]["distance_km"] <= 10


def test_active_alerts_real_ca_feed_and_fire_first() -> None:
    doc = json.loads((FEEDS / "nws-alerts-ca-20261109.json").read_text())
    actual = [f for f in doc["features"] if f["properties"].get("status") == "Actual"]
    alerts = active_alerts(doc)
    assert len(alerts) == len(actual) and len(alerts) > 0
    assert all(a["event"] and a["sender"] for a in alerts)
    mixed = {
        "features": [
            {"properties": {"status": "Actual", "event": "Wind Advisory", "onset": "2026-11-09T01:00:00Z"}},
            {"properties": {"status": "Test", "event": "Red Flag Warning"}},
            {
                "properties": {
                    "status": "Actual",
                    "event": "Red Flag Warning",
                    "onset": "2026-11-09T05:00:00Z",
                }
            },
        ]
    }
    assert [a["event"] for a in active_alerts(mixed)] == ["Red Flag Warning", "Wind Advisory"]


# --------------------------------------------------------------------------- pure: brief


def test_prompt_carries_only_facts(captured_facts: dict) -> None:
    system, user = build_messages(captured_facts)
    assert (
        system["role"] == "system"
        and "ONLY" in system["content"]
        and "Do not convert units" in system["content"]
    )
    for label in LABELS:
        assert f"{label}:" in system["content"]
    body = user["content"].split("Facts JSON:\n", 1)[1].rsplit("\n\nWrite", 1)[0]
    assert json.loads(body) == prompt_facts(captured_facts)
    assert "sources" not in json.loads(body)
    # Slim on purpose: no ids, bearings or URLs for the reasoning model to chase.
    assert TARGET.entity_id not in body and "bearing_deg" not in body and "https://" not in body
    station = captured_facts["resources"]["fire_stations"][0]
    assert json.loads(body)["resources"]["fire_stations"][0] == {
        "name": station["name"],
        "distance_km": station["distance_km"],
        "direction": station["direction"],
    }


def test_parse_narrative_accepts_labels_and_rejects_short_output() -> None:
    text = "<think>plan</think>\n" + "\n".join(
        [
            "1. **LOCATION:** Glendale, 34.15232, -118.19506.",
            "- FUEL/FHSZ: Very High.",
            "WEATHER/WIND: WSW 5 mph, 78F.",
            "EXPOSURES: hospitals listed.",
            "NEAREST RESOURCES: station 987.6 km away.",
            "UNKNOWNS: fire size.",
        ]
    )
    lines = parse_narrative(text)
    assert lines is not None and [line.split(":")[0] for line in lines] == list(LABELS)
    assert lines[0] == "LOCATION: Glendale, 34.15232, -118.19506."
    assert parse_narrative("\n".join(text.splitlines()[:-1])) is None  # five lines: rejected


def _supported_lines(facts: dict) -> list[str]:
    """A six-line narrative built only from values the facts carry."""
    station = facts["resources"]["fire_stations"][0]
    hospital = facts["exposures"]["hospitals"][0]
    period = facts["weather"]["periods"][0]
    return [
        f"LOCATION: {facts['target']['name']} near {facts['location']['nearest_place']['city']}.",
        f"FUEL/FHSZ: {facts['fuel']['fhsz']} in a fire hazard severity zone.",
        f"WEATHER/WIND: {period['wind_direction']} {period['wind_speed']}, "
        f"{period['temperature']}{period['temperature_unit']}.",
        f"EXPOSURES: {hospital['name']} {hospital['distance_km']} km {hospital['direction']}.",
        f"NEAREST RESOURCES: {station['name']} {station['distance_km']} km {station['direction']} "
        f"({station['bearing_deg']} deg).",
        "UNKNOWNS: fire size unknown.",
    ]


def test_verifier_keeps_a_supported_claim(captured_facts: dict) -> None:
    lines = _supported_lines(captured_facts)
    assert [unsupported_reasons(line.split(":", 1)[1], captured_facts) for line in lines] == [[]] * len(
        LABELS
    )
    result = verify_narrative(lines, captured_facts)
    assert result.replaced == [] and result.lines_replaced == 0
    assert result.lines == lines  # kept verbatim, no rewrite of a supported brief

    narrative = write_narrative(captured_facts, chat_result=facts_chat([]))
    assert narrative.status == NARRATIVE_OK and narrative.verifier is not None
    assert narrative.verifier.lines_replaced == 0
    md = render_markdown(captured_facts, narrative)
    assert f"({MODEL_ID}) in {LATENCY_S:.1f} s" in md
    assert "Verifier: 0 lines replaced" in md


def test_verifier_replaces_an_invented_station(captured_facts: dict) -> None:
    lines = _supported_lines(captured_facts)
    lines[4] = "NEAREST RESOURCES: Ventura County Fire Department - Station 9, 12.5 km NW."
    result = verify_narrative(lines, captured_facts)
    assert result.replaced == ["NEAREST RESOURCES"]
    assert result.lines[:4] == lines[:4] and result.lines[5] == lines[5]
    assert result.lines[4] == fact_lines(captured_facts)[4]  # that section's facts-only line
    reasons = result.details["NEAREST RESOURCES"]
    assert "direction NW" in reasons and "station Station 9" in reasons
    assert any(r.startswith("facility Ventura County") for r in reasons)

    md = render_markdown(captured_facts, write_narrative(captured_facts, chat_result=_one_bad_line(lines)))
    assert "Verifier: 1 lines replaced (NEAREST RESOURCES -> facts-only lines)" in md


def test_verifier_keeps_a_compass_word_inside_a_listed_facility_name(captured_facts: dict) -> None:
    """A hospital called "South Bay" is a name, not a bearing claim (eval note 2026-11-09).

    The 2026-11-09 eval replaced the Avalon EXPOSURES line for "direction South" because the
    hospital is "Kaiser Permanente - South Bay Medical Center": a correct line was thrown away.
    """
    facts = json.loads(json.dumps(captured_facts))
    base = facts["exposures"]["hospitals"][0]
    facts["exposures"]["hospitals"] = [
        {
            **base,
            "name": "Kaiser Permanente - South Bay Medical Center",
            "direction": "N",
            "distance_km": 44.0,
        },
        {**base, "name": "West Hills Hospital and Medical Center", "direction": "SW", "distance_km": 7.7},
    ]
    body = (
        "Kaiser Permanente - South Bay Medical Center 44.0 km N, "
        "West Hills Hospital and Medical Center 7.7 km SW."
    )
    assert unsupported_reasons(body, facts) == []
    # A bearing the facts do not report is still caught, even next to a compass-word name.
    assert unsupported_reasons("Kaiser Permanente - South Bay Medical Center 44.0 km E.", facts) == [
        "direction E"
    ]


def _one_bad_line(lines: list[str]) -> Any:
    text = "\n".join(lines)

    def chat_result(messages: list[dict], **kwargs: Any) -> ChatResult:
        return ChatResult(text=text, model=MODEL_ID, latency_s=LATENCY_S, usage={})

    return chat_result


def test_verifier_fixes_the_glossary_slip(captured_facts: dict) -> None:
    assert apply_glossary("A Fuel hazard severity zone, fuel HAZARD severity zones") == (
        "A Fire Hazard Severity Zone, Fire Hazard Severity Zones"
    )
    lines = _supported_lines(captured_facts)
    lines[1] = "FUEL/FHSZ: A fuel hazard severity zone rated Very High."
    result = verify_narrative(lines, captured_facts)
    assert result.replaced == []
    assert result.lines[1] == "FUEL/FHSZ: A Fire Hazard Severity Zone rated Very High."


def test_verifier_rejects_a_wrong_class_or_invented_number(captured_facts: dict) -> None:
    lines = _supported_lines(captured_facts)
    lines[1] = "FUEL/FHSZ: Moderate in a Fire Hazard Severity Zone."
    lines[2] = "WEATHER/WIND: WNW 22 mph, 78F."
    result = verify_narrative(lines, captured_facts)
    assert result.replaced == ["FUEL/FHSZ", "WEATHER/WIND"]
    assert "FHSZ class Moderate" in result.details["FUEL/FHSZ"]
    assert "direction WNW" in result.details["WEATHER/WIND"]
    assert "number 22" in result.details["WEATHER/WIND"]
    assert result.lines[1] == fact_lines(captured_facts)[1]


def test_llm_unavailable_brief_ships_facts_with_explicit_line(captured_facts: dict) -> None:
    narrative = write_narrative(captured_facts, chat_result=down_chat)
    assert narrative.status == NARRATIVE_UNAVAILABLE and narrative.lines == []
    md = render_markdown(captured_facts, narrative)
    assert "**Narrative unavailable: local model down** (cluster LLM at" in md
    assert "No text was generated" in md
    for line in fact_lines(captured_facts):
        assert f"- {line}" in md
    assert TARGET.entity_id in md and "https://api.weather.gov/gridpoints/LOX/158,49/forecast" in md

    rejected = write_narrative(
        captured_facts,
        chat_result=lambda messages, **kw: ChatResult(
            text="Sure! The fire is big.", model=MODEL_ID, latency_s=1.0
        ),
    )
    assert rejected.status == NARRATIVE_REJECTED
    assert "Narrative rejected" in render_markdown(captured_facts, rejected)
    assert rejected.model == MODEL_ID and rejected.verifier is None
    assert "unrecorded model" not in render_markdown(captured_facts, rejected)


def test_footer_names_the_server_model(captured_facts: dict) -> None:
    narrative = write_narrative(
        captured_facts, chat_result=facts_chat([], model="test-served-model", latency_s=3.25)
    )
    assert narrative.model == "test-served-model" and narrative.latency_s == 3.25
    doc = narrative.as_dict()
    assert doc["model"] == "test-served-model" and doc["latency_s"] == 3.25
    assert doc["verifier"] == {"lines_replaced": 0, "replaced": [], "details": {}}
    footer = narrative_footer(narrative)
    assert "test-served-model" in footer and "3.2 s" in footer


# --------------------------------------------------------------------------- harness: agent
# The harness is session-wide, so every test runs its own agent entity id.


def make_agent(lattice: Any, harness: Any, **kwargs: Any) -> AnalystAgent:
    kwargs.setdefault("sources", FixtureSources())
    kwargs.setdefault("chat_result", facts_chat([]))
    return AnalystAgent(
        lattice,
        entity_id=f"lw-analyst-test-{uuid.uuid4().hex[:8]}",
        anchor=(34.26, -118.30),
        lattice_url=harness.base_url,
        heartbeat_s=0.3,
        stream_heartbeat_ms=500,
        **kwargs,
    )


@pytest.fixture()
def running(lattice: Any, harness: Any):
    agents: list[AnalystAgent] = []

    def start(**kwargs: Any) -> AnalystAgent:
        agent = make_agent(lattice, harness, **kwargs).start()
        agents.append(agent)
        return agent

    yield start
    for agent in agents:
        agent.stop(timeout=10)


def create_investigate(
    client: Any, target_id: str, agent_id: str, *, spec_type: str = INVESTIGATE_URL, objective: bool = True
) -> str:
    spec = (
        GoogleProtobufAny(type=spec_type, objective={"entity_id": target_id})
        if objective
        else GoogleProtobufAny(type=spec_type)
    )
    task = client.tasks.create_task(
        display_name=f"Investigate {target_id}",
        specification=spec,
        author=Principal(system=System(service_name="test-analyst")),
        relations=Relations(assignee=Principal(system=System(entity_id=agent_id))),
        is_executed_elsewhere=False,
    )
    return task.version.task_id


def terminal(client: Any, task_id: str):
    def check():
        task = client.tasks.get_task(task_id)
        return task if task.status.status in agent_mod.TERMINAL else None

    return wait_for(check, timeout=20)


def read_object(client: Any, path: str) -> bytes:
    return b"".join(client.objects.get_object(path))


def brief_objects(client: Any, task_id: str) -> list[str]:
    return sorted(
        p.content_identifier.path
        for p in client.objects.list_objects(prefix=f"lattice-wildfire/briefs/{task_id}")
    )


def test_build_agent_entity_rejects_bad_time() -> None:
    health = AgentHealth(stream="connected", llm="unused")
    with pytest.raises(ValueError):
        build_agent_entity(
            entity_id="x", name="x", lat=34, lon=-118, now=datetime(2026, 11, 9), expiry_s=30, health=health
        )
    with pytest.raises(ValueError):
        build_agent_entity(
            entity_id="x",
            name="x",
            lat=34,
            lon=-118,
            now=datetime.now(UTC),
            expiry_s=31 * 86400,
            health=health,
        )


def test_agent_entity_live_with_investigate_catalog(lattice: Any, running) -> None:
    agent = running()
    first = lattice.entities.get_entity(agent.entity_id)
    assert first.is_live is True
    assert [d.task_specification_url for d in first.task_catalog.task_definitions] == [INVESTIGATE_URL]
    assert first.ontology.template == "TEMPLATE_ASSET" and first.ontology.platform_type == "AI Analyst"
    assert first.mil_view.disposition == "DISPOSITION_FRIENDLY"
    assert first.provenance.integration_name == "lattice-wildfire-analyst"
    assert first.provenance.data_type == "kodros-analyst"
    assert first.aliases.name == "Kodros Analyst"
    assert first.symbology.mil_std2525c.sidc[:3] == "SFG"
    now = datetime.now(UTC)
    assert now < first.expiry_time < now + timedelta(days=30)
    # Heartbeat: a later read shows a newer publish with the task stream connected.
    second = wait_for(
        lambda: (
            (e := lattice.entities.get_entity(agent.entity_id)).provenance.source_update_time
            > first.provenance.source_update_time
            and e.health.health_status == "HEALTH_STATUS_HEALTHY"
            and e
        )
    )
    assert second.health.connection_status == "CONNECTION_STATUS_ONLINE"
    assert {c.id: c.health for c in second.health.components}["task-stream"] == "HEALTH_STATUS_HEALTHY"
    agent.stop()
    offline = lattice.entities.get_entity(agent.entity_id)
    assert offline.health.connection_status == "CONNECTION_STATUS_OFFLINE"


def test_task_status_sent_executing_done_ok_with_brief(lattice: Any, harness: Any) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    neighbour = publish_hotspot(lattice, _firms_row("34.15582", "-118.19634"))  # same site, 0.4 km
    far = publish_hotspot(lattice, _firms_row("33.85357", "-118.3331"))  # ~36 km, outside radius
    chats: list = []
    sources = FixtureSources(gated=True)
    agent = make_agent(lattice, harness, sources=sources, chat_result=facts_chat(chats))
    agent.publish()
    task_id = create_investigate(lattice, hotspot, agent.entity_id)
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_CREATED"

    # Pull the request the way the agent stream does, then hand it to the agent's handler.
    request = lattice.tasks.listen_as_agent(agent_selector=EntityIdsSelector(entity_ids=[agent.entity_id]))
    assert request.execute_request.task.version.task_id == task_id
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_SENT"
    agent.handle_request(request)
    assert sources.entered.wait(10)
    executing = lattice.tasks.get_task(task_id)
    assert executing.status.status == "STATUS_EXECUTING"
    assert executing.last_updated_by.system.entity_id == agent.entity_id
    sources.gate.set()

    done = terminal(lattice, task_id)
    assert done.status.status == "STATUS_DONE_OK"
    assert done.version.status_version > executing.version.status_version

    md_path, json_path = object_paths(task_id)
    assert brief_objects(lattice, task_id) == sorted([md_path, json_path])
    md = read_object(lattice, md_path).decode()
    doc = json.loads(read_object(lattice, json_path))
    assert hotspot in md and task_id in md
    assert "LOCATION: VIIRS N20 hotspot 2026-11-09 1036Z near Glendale, CA." in md
    assert "FHSZ Very High" in md and "LOX/158,49" in md
    station = doc["facts"]["resources"]["fire_stations"][0]
    assert f"{station['name']} {station['distance_km']} km {station['direction']}" in md
    assert doc["target_entity_id"] == hotspot and doc["narrative"]["status"] == NARRATIVE_OK
    assert doc["narrative"]["verifier"]["lines_replaced"] == 0
    assert doc["narrative"]["model"] == MODEL_ID and doc["narrative"]["latency_s"] == LATENCY_S
    assert f"({MODEL_ID}) in {LATENCY_S:.1f} s" in md and "Verifier: 0 lines replaced" in md
    nearby = doc["facts"]["nearby_fire_activity"]["entities"]
    ids = [n["entity_id"] for n in nearby]
    assert neighbour in ids and far not in ids and hotspot not in ids
    assert all(n["target_inside_polygon"] or n["distance_km"] <= 10 for n in nearby)
    lattice_src = [s for s in doc["facts"]["sources"] if s["name"].startswith("Lattice entities")]
    assert (
        lattice_src[0]["url"] == f"{harness.base_url}/api/v1/entities/stream" and lattice_src[0]["fetched_at"]
    )
    assert all(s["fetched_at"] for s in doc["facts"]["sources"])
    # The model only ever saw the facts prompt.
    ((messages, _kwargs),) = chats
    assert messages == build_messages(doc["facts"])

    # A redelivered execute for a finished task is ignored: terminal stays final.
    agent.handle_request(request)
    time.sleep(0.3)
    again = lattice.tasks.get_task(task_id)
    assert (
        again.status.status == "STATUS_DONE_OK"
        and again.version.status_version == done.version.status_version
    )
    agent.stop(timeout=10)


def test_stream_agent_end_to_end_and_poll_fallback(lattice: Any, running) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    agent = running()
    wait_for(lambda: agent.health().stream == "connected")
    first = create_investigate(lattice, hotspot, agent.entity_id)
    assert terminal(lattice, first).status.status == "STATUS_DONE_OK"

    agent.mode = "poll"  # what the agent switches to when the stream endpoint is missing
    second = create_investigate(lattice, hotspot, agent.entity_id)
    assert terminal(lattice, second).status.status == "STATUS_DONE_OK"
    assert len(brief_objects(lattice, second)) == 2


def test_cancel_mid_task_ends_done_not_ok_without_brief(lattice: Any, running) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    sources = FixtureSources(gated=True)
    agent = running(sources=sources)
    task_id = create_investigate(lattice, hotspot, agent.entity_id)
    assert sources.entered.wait(10)
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_EXECUTING"

    lattice.tasks.cancel_task(task_id, author=Principal(system=System(service_name="test-analyst")))
    wait_for(lambda: agent.cancel_requested(task_id))
    sources.gate.set()

    done = terminal(lattice, task_id)
    assert done.status.status == "STATUS_DONE_NOT_OK"
    assert done.status.task_error.code == "ERROR_CODE_CANCELLED"
    assert done.last_updated_by.system.entity_id == agent.entity_id
    time.sleep(1.0)  # nothing more may arrive after the terminal update
    later = lattice.tasks.get_task(task_id)
    assert (
        later.status.status == "STATUS_DONE_NOT_OK"
        and later.version.status_version == done.version.status_version
    )
    assert brief_objects(lattice, task_id) == []
    wait_for(lambda: agent.in_flight() == [])


def test_llm_down_task_completes_with_facts_only_brief(lattice: Any, running) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    agent = running(chat_result=down_chat)
    task_id = create_investigate(lattice, hotspot, agent.entity_id)
    assert terminal(lattice, task_id).status.status == "STATUS_DONE_OK"
    md = read_object(lattice, object_paths(task_id)[0]).decode()
    assert "**Narrative unavailable: local model down**" in md
    assert "- FUEL/FHSZ: FHSZ Very High" in md and hotspot in md
    doc = json.loads(read_object(lattice, object_paths(task_id)[1]))
    assert doc["narrative"]["status"] == NARRATIVE_UNAVAILABLE and doc["narrative"]["lines"] == []
    wait_for(
        lambda: (
            {c.id: c.health for c in lattice.entities.get_entity(agent.entity_id).health.components}.get(
                "local-llm"
            )
            == "HEALTH_STATUS_WARN"
        )
    )


@pytest.mark.parametrize(
    ("spec_type", "objective", "target", "code"),
    [
        ("type.googleapis.com/anduril.tasks.v2.VisualId", True, "lw-anything", "ERROR_CODE_REJECTED"),
        (INVESTIGATE_URL, False, "lw-anything", "ERROR_CODE_REJECTED"),
        (INVESTIGATE_URL, True, "lw-firms-does-not-exist", "ERROR_CODE_FAILED"),
    ],
)
def test_bad_tasks_end_done_not_ok(
    lattice: Any, running, spec_type: str, objective: bool, target: str, code: str
) -> None:
    agent = running()
    task_id = create_investigate(lattice, target, agent.entity_id, spec_type=spec_type, objective=objective)
    done = terminal(lattice, task_id)
    assert done.status.status == "STATUS_DONE_NOT_OK"
    assert done.status.task_error.code == code
    assert brief_objects(lattice, task_id) == []


# --------------------------------------------------------------------------- capacity


def test_entity_description_carries_capacity_for_the_planner() -> None:
    entity = build_agent_entity(
        entity_id="lw-analyst-x",
        name="Kodros Analyst",
        lat=34.0,
        lon=-118.0,
        now=datetime.now(UTC),
        expiry_s=30,
        health=AgentHealth(stream="connected", llm="unused"),
    )
    assert DEFAULT_CAPACITY == 3 and f"capacity={DEFAULT_CAPACITY}" in entity["description"]
    assert capacity_from_description(entity["description"]) == 3  # the planner's own reader
    wider = build_agent_entity(
        entity_id="lw-analyst-x",
        name="Kodros Analyst",
        lat=34.0,
        lon=-118.0,
        now=datetime.now(UTC),
        expiry_s=30,
        health=AgentHealth(stream="connected", llm="unused"),
        capacity=7,
    )
    assert capacity_from_description(wider["description"]) == 7
    with pytest.raises(ValueError):
        build_agent_entity(
            entity_id="lw-analyst-x",
            name="Kodros Analyst",
            lat=34.0,
            lon=-118.0,
            now=datetime.now(UTC),
            expiry_s=30,
            health=AgentHealth(stream="connected", llm="unused"),
            capacity=0,
        )


def test_capacity_defaults_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LW_ANALYST_CAPACITY", raising=False)
    assert capacity_from_env() == DEFAULT_CAPACITY == 3
    monkeypatch.setenv("LW_ANALYST_CAPACITY", "5")
    assert capacity_from_env() == 5
    monkeypatch.setenv("LW_ANALYST_CAPACITY", "0")
    with pytest.raises(ValueError):
        capacity_from_env()
    monkeypatch.setenv("LW_ANALYST_CAPACITY", "lots")
    with pytest.raises(ValueError):
        capacity_from_env()


def test_agent_entity_publishes_capacity_to_lattice(lattice: Any, running) -> None:
    agent = running(capacity=3)
    entity = wait_for(lambda: lattice.entities.get_entity(agent.entity_id))
    assert "capacity=3" in entity.description
    assert capacity_from_description(entity.description) == 3
    with pytest.raises(ValueError):
        AnalystAgent(lattice, entity_id="lw-analyst-bad", anchor=(34.0, -118.0), capacity=0)


class CountingSources(FixtureSources):
    """Blocks every worker inside the forecast fetch and records how many are there at once."""

    def __init__(self) -> None:
        super().__init__(gated=True)  # the gate starts closed; each worker blocks in the forecast fetch
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.waiting = threading.Event()

    def json(self, url: str, params: dict | None, ttl_s: float) -> Fetched:
        if url.endswith("/forecast"):
            with self.lock:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
                self.waiting.set()
            try:
                if not self.gate.wait(30):
                    raise TimeoutError("test gate never opened")
            finally:
                with self.lock:
                    self.inside -= 1
        return super().json(url, params, ttl_s)


def _pull(lattice: Any, agent: AnalystAgent) -> Any:
    return lattice.tasks.listen_as_agent(agent_selector=EntityIdsSelector(entity_ids=[agent.entity_id]))


def test_capacity_limits_concurrency_and_queues_the_rest(lattice: Any, harness: Any) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    sources = CountingSources()
    agent = make_agent(lattice, harness, sources=sources, capacity=2)
    agent.publish()
    task_ids = [create_investigate(lattice, hotspot, agent.entity_id) for _ in range(3)]
    requests = [_pull(lattice, agent) for _ in task_ids]
    assert [r.execute_request.task.version.task_id for r in requests] == task_ids
    for request in requests:
        agent.handle_request(request)

    assert sources.waiting.wait(10)
    wait_for(lambda: sources.peak == 2)
    statuses = [lattice.tasks.get_task(task_id).status.status for task_id in task_ids]
    assert statuses.count("STATUS_EXECUTING") == 2
    assert statuses.count("STATUS_SENT") == 1  # queued: it stays SENT until a slot frees
    time.sleep(0.2)
    assert sources.peak == 2  # the third task never starts while two run

    sources.gate.set()
    for task_id in task_ids:
        done = terminal(lattice, task_id)
        assert done.status.status == "STATUS_DONE_OK", (task_id, done.status.task_error)  # fmt: skip
    assert sources.peak == 2  # never more than capacity, even as the queue drains
    assert len(brief_objects(lattice, task_ids[2])) == 2
    agent.stop(timeout=10)


def test_bounded_queue_refuses_an_overflow_task(lattice: Any, harness: Any) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    sources = CountingSources()
    agent = make_agent(lattice, harness, sources=sources, capacity=1, queue_max=1)
    agent.publish()
    first = create_investigate(lattice, hotspot, agent.entity_id)
    agent.handle_request(_pull(lattice, agent))
    assert sources.waiting.wait(10)  # the single worker is busy, so the queue decides

    waiting_task = create_investigate(lattice, hotspot, agent.entity_id)
    overflow_task = create_investigate(lattice, hotspot, agent.entity_id)
    for _ in range(2):
        agent.handle_request(_pull(lattice, agent))

    done = terminal(lattice, overflow_task)
    assert done.status.status == "STATUS_DONE_NOT_OK"
    assert done.status.task_error.code == "ERROR_CODE_REJECTED"
    assert "queued" in done.status.task_error.message
    assert lattice.tasks.get_task(waiting_task).status.status == "STATUS_SENT"

    sources.gate.set()
    assert terminal(lattice, first).status.status == "STATUS_DONE_OK"
    assert terminal(lattice, waiting_task).status.status == "STATUS_DONE_OK"
    agent.stop(timeout=10)


# --------------------------------------------------------------------------- harness: shutdown
# SIGTERM used to hang: the SDK's default retries outlived the supervisor's 10 s grace period while
# Lattice was gone and a worker was still inside the local model. These prove the stop path is
# bounded and that every unfinished task ends DONE_NOT_OK with the shutdown reason.


class NeverAnsweringModel:
    """Local OpenAI-compatible stub that accepts ``/chat/completions`` and never answers.

    ``core.llm`` waits ``DEFAULT_TIMEOUT_S`` (180 s), so a worker inside it outlives any sane
    shutdown: this is the in-flight model call the stop path must abandon rather than wait for.
    """

    def __init__(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # no request log in test output
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length") or 0))
                stub.entered.set()
                stub.release.wait(60)

        self.entered = threading.Event()
        self.release = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.release.set()  # the held handler returns with no response: the worker sees LLMUnavailable
        self.server.shutdown()
        self.server.server_close()


def test_stop_is_bounded_while_a_task_waits_on_the_model(
    lattice: Any, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = NeverAnsweringModel()
    monkeypatch.setenv("KODROS_LLM_URL", model.url)
    try:
        hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
        agent = make_agent(lattice, harness, chat_result=None, capacity=1).start()
        running_task = create_investigate(lattice, hotspot, agent.entity_id)
        queued_task = create_investigate(lattice, hotspot, agent.entity_id)
        assert model.entered.wait(20)  # the single worker is inside the model call
        wait_for(lambda: sorted(agent.in_flight()) == sorted([running_task, queued_task]), timeout=10)

        started = time.monotonic()
        agent.stop(timeout=3.0)
        elapsed = time.monotonic() - started
        assert elapsed < 3.5, f"stop took {elapsed:.2f}s of a 3.0s deadline"

        for task_id in (running_task, queued_task):
            done = terminal(lattice, task_id)
            assert done.status.status == "STATUS_DONE_NOT_OK"
            assert done.status.task_error.code == "ERROR_CODE_CANCELLED"
            assert done.status.task_error.message == "agent stopping"
        offline = wait_for(
            lambda: (
                entity
                if (entity := lattice.entities.get_entity(agent.entity_id)).health.connection_status
                == "CONNECTION_STATUS_OFFLINE"
                else None
            ),
            timeout=5,
        )
        assert offline.health.health_status == "HEALTH_STATUS_OFFLINE"
    finally:
        model.stop()


def _no_lattice_client() -> Any:
    """A client pointed at a closed port: what a vanished Lattice looks like from this process.

    Closing the harness listener is not enough, because its handler threads keep answering on the
    keep-alive sockets already open; a refused connection is what the analyst hit in the field.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return make_client(
        LatticeSettings(
            base_url=f"http://127.0.0.1:{port}",
            environment_token="test-token",
            sandboxes_token=None,
            client_id=None,
            client_secret=None,
        ),
        timeout=30,
    )


def test_stop_after_lattice_vanishes_is_bounded_and_logs_once(
    lattice: Any, harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hotspot = publish_hotspot(lattice, _firms_row("34.15232", "-118.19506"))
    sources = FixtureSources(gated=True)
    agent = make_agent(lattice, harness, sources=sources).start()
    create_investigate(lattice, hotspot, agent.entity_id)
    assert sources.entered.wait(20)  # the worker is mid-brief, so a terminal update is owed
    agent.client = _no_lattice_client()  # Lattice goes away while the task is in flight

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="lattice_wildfire.analyst"):
        agent.stop(timeout=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, f"stop took {elapsed:.2f}s of a 2.0s deadline"
    unreachable = [
        record.getMessage()
        for record in caplog.records
        if "Lattice unreachable at shutdown" in record.getMessage()
    ]
    assert len(unreachable) == 1, unreachable
    assert agent.health().running is False
    sources.gate.set()
