"""Lattice Talk tests against the contract harness (real SDK client, real HTTP).

Station locations are real LACoFD stations captured from LA County eGIS (tests/fixtures/talk).
Hotspot and polygon probes are geometry placed at computed distances from Station 11 to pin the
radius boundary; they are test probes, not feed data.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest
from anduril import Aliases, MilView, Ontology, TaskCatalog, TaskDefinition

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.core.entities import (
    expiry,
    point_location,
    polygon_geo,
    provenance,
    symbology,
    unclassified,
)
from lattice_wildfire.core.geo import EARTH_RADIUS_M, haversine_m
from lattice_wildfire.core.layers import facilities_from_geojson
from lattice_wildfire.harness import start_harness
from lattice_wildfire.talk.ask import run_ask
from lattice_wildfire.talk.tools import (
    BRIEF_PREFIX,
    INVESTIGATE_URL,
    TOOL_NAMES,
    TalkTools,
    objective_entity_id,
    parse_bbox,
    snapshot_from_events,
    task_id_of,
)

FIXTURE = Path(__file__).parent / "fixtures" / "talk" / "lacofd-stations-20261109.geojson"
STATIONS = facilities_from_geojson(json.loads(FIXTURE.read_text()), "fire_station")
ST11 = next(s for s in STATIONS if s.id == "11")
ANALYST_ID = "lw-analyst-talk-test"
CANDIDATE_ID = "lw-cand-20261109-talktst"


# ---------------------------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------------------------


def destination(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    """Spherical destination point (same Earth radius as core.geo.haversine_m)."""
    d = dist_m / EARTH_RADIUS_M
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def _common(entity_id: str, name: str, data_type: str) -> dict:
    return {
        "entity_id": entity_id,
        "is_live": True,
        "expiry_time": expiry(3600),
        "aliases": Aliases(name=name),
        "provenance": provenance("talk-test", data_type),
        "data_classification": unclassified(),
    }


def publish_station(lattice, facility) -> str:
    entity_id = f"lw-station-{facility.id}"
    lattice.entities.publish_entity(
        **_common(entity_id, facility.name, "lacofd-station"),
        location=point_location(facility.lat, facility.lon),
        mil_view=MilView(disposition="DISPOSITION_FRIENDLY", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_ASSET", platform_type="Fire Station"),
        symbology=symbology("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND"),
    )
    return entity_id


def publish_point(lattice, entity_id: str, data_type: str, lat: float, lon: float, **extra) -> None:
    disposition = extra.pop("disposition", "DISPOSITION_UNKNOWN")
    template = extra.pop("template", "TEMPLATE_TRACK")
    lattice.entities.publish_entity(
        **_common(entity_id, extra.pop("name", entity_id), data_type),
        location=point_location(lat, lon),
        mil_view=MilView(disposition=disposition, environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template=template, platform_type=extra.pop("platform_type", "Test Probe")),
        symbology=symbology(disposition, "ENVIRONMENT_LAND"),
        **extra,
    )


def publish_polygon(lattice, entity_id: str, ring: list[tuple[float, float]]) -> None:
    details, shape = polygon_geo(ring)
    lattice.entities.publish_entity(
        **_common(entity_id, "Test perimeter probe", "wfigs-perimeter"),
        geo_details=details,
        geo_shape=shape,
        ontology=Ontology(template="TEMPLATE_GEO", platform_type="Wildfire Perimeter"),
    )


def publish_analyst(lattice, lat: float, lon: float) -> None:
    publish_point(
        lattice,
        ANALYST_ID,
        "kodros-analyst",
        lat,
        lon,
        name="Talk test analyst",
        disposition="DISPOSITION_FRIENDLY",
        template="TEMPLATE_ASSET",
        platform_type="AI Analyst",
        task_catalog=TaskCatalog(task_definitions=[TaskDefinition(task_specification_url=INVESTIGATE_URL)]),
    )


def all_tasks(lattice) -> list:
    tasks, token = [], None
    while True:
        page = lattice.tasks.query_tasks(page_token=token) if token else lattice.tasks.query_tasks()
        tasks.extend(page.tasks or [])
        token = page.next_page_token
        if not token:
            return tasks


def investigations_on(lattice, entity_id: str) -> list:
    return [t for t in all_tasks(lattice) if objective_entity_id(t) == entity_id]


# ---------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def lattice(monkeypatch):
    """A fresh harness per test (not the shared session one), so tasks and seeded probes neither
    leak into other apps' tests nor depend on them. Real SDK client via core.make_client."""
    for name in (
        "LATTICE_BASE_URL",
        "LATTICE_ENDPOINT",
        "SANDBOXES_TOKEN",
        "LATTICE_CLIENT_ID",
        "LATTICE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    with start_harness(0, long_poll_timeout_s=2.0) as h:
        monkeypatch.setenv("LATTICE_BASE_URL", h.base_url)
        monkeypatch.setenv("ENVIRONMENT_TOKEN", "test-token")
        yield make_client(LatticeSettings.from_env(dotenv=False), timeout=30)


@pytest.fixture
def seeded(lattice, monkeypatch):
    """Harness picture: 15 real stations, boundary probes around Station 11, a perimeter probe."""
    monkeypatch.delenv("LW_AOI_BBOX", raising=False)
    for facility in STATIONS:
        publish_station(lattice, facility)
    inside = destination(ST11.lat, ST11.lon, 0.0, 4_990.0)
    outside = destination(ST11.lat, ST11.lon, 0.0, 5_010.0)
    publish_point(lattice, "lw-firms-probe-inside", "firms-viirs-hotspot", *inside, name="Probe 4.99 km N")
    publish_point(lattice, "lw-firms-probe-outside", "firms-viirs-hotspot", *outside, name="Probe 5.01 km N")
    # Square perimeter whose west edge is 3 km east of Station 11 and whose centroid is 6 km east.
    west = destination(ST11.lat, ST11.lon, 90.0, 3_000.0)[1]
    east = destination(ST11.lat, ST11.lon, 90.0, 9_000.0)[1]
    north = destination(ST11.lat, ST11.lon, 0.0, 3_000.0)[0]
    south = destination(ST11.lat, ST11.lon, 180.0, 3_000.0)[0]
    publish_polygon(lattice, "lw-perim-probe", [(south, west), (north, west), (north, east), (south, east)])
    return {"inside": inside, "outside": outside, "perimeter_west_lon": west}


@pytest.fixture
def tools(lattice, seeded):
    return TalkTools(lattice, picture_ttl_s=0.0)


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------


def test_parse_bbox_uses_lon_first_order_and_rejects_bad_boxes():
    aoi = parse_bbox("-118.3,34.0,-118.0,34.3")
    assert (aoi.min_lat, aoi.min_lon, aoi.max_lat, aoi.max_lon) == (34.0, -118.3, 34.3, -118.0)
    assert parse_bbox(None) is None and parse_bbox("") is None
    with pytest.raises(ValueError):
        parse_bbox("34.0,-118.3,34.3")  # three numbers
    with pytest.raises(ValueError):
        parse_bbox("34.0,-118.3,34.3,-118.0")  # lat-first order gives min_lon > max_lon


def test_station_fixture_is_the_real_capture():
    assert len(STATIONS) == 15
    assert (round(ST11.lat, 4), round(ST11.lon, 4)) == (34.1886, -118.1328)


# ---------------------------------------------------------------------------------------------
# Reads through the harness
# ---------------------------------------------------------------------------------------------


def test_snapshot_reads_the_seeded_picture(lattice, seeded):
    entities = snapshot_from_events(lattice.entities.stream_entities(pre_existing_only=True))
    ids = {e.entity_id for e in entities}
    assert {f"lw-station-{s.id}" for s in STATIONS} <= ids
    assert {"lw-firms-probe-inside", "lw-firms-probe-outside", "lw-perim-probe"} <= ids


def test_wildfire_picture_counts_and_bbox(tools):
    view = tools.wildfire_picture()
    assert view["counts"]["lacofd-station"] == 15
    assert view["counts"]["firms-viirs-hotspot"] == 2
    # A bbox around Station 11 only (about 1 km each way) with a kinds filter.
    box = f"{ST11.lon - 0.01},{ST11.lat - 0.01},{ST11.lon + 0.01},{ST11.lat + 0.01}"
    small = tools.wildfire_picture(bbox=box, kinds=["lacofd-station"])
    assert [e["entity_id"] for e in small["entities"]] == ["lw-station-11"]
    assert "error" in tools.wildfire_picture(bbox="1,2,3")


def test_near_radius_boundary_is_inclusive_and_exact(tools, seeded):
    hits = tools.near(ST11.lat, ST11.lon, 5.0, kinds=["firms-viirs-hotspot"])
    assert [e["entity_id"] for e in hits["entities"]] == ["lw-firms-probe-inside"]
    assert hits["entities"][0]["distance_km"] == pytest.approx(4.99, abs=0.002)
    assert hits["entities"][0]["bearing_deg"] == pytest.approx(0.0, abs=0.2)

    # Radius exactly equal to the outside probe's distance includes it (<=), a hair less does not.
    exact_km = haversine_m(ST11.lat, ST11.lon, *seeded["outside"]) / 1000.0
    ids = [
        e["entity_id"]
        for e in tools.near(ST11.lat, ST11.lon, exact_km, kinds=["firms-viirs-hotspot"])["entities"]
    ]
    assert ids == ["lw-firms-probe-inside", "lw-firms-probe-outside"]
    ids = [
        e["entity_id"]
        for e in tools.near(ST11.lat, ST11.lon, exact_km - 1e-6, kinds=["firms-viirs-hotspot"])["entities"]
    ]
    assert ids == ["lw-firms-probe-inside"]

    assert "error" in tools.near(91.0, 0.0, 1.0)
    assert "error" in tools.near(ST11.lat, ST11.lon, -1.0)


def test_near_measures_polygons_to_the_edge_not_the_centroid(tools, seeded):
    hits = tools.near(ST11.lat, ST11.lon, 4.0, kinds=["wfigs-perimeter"])["entities"]
    assert [e["entity_id"] for e in hits] == ["lw-perim-probe"]
    assert hits[0]["distance_km"] == pytest.approx(3.0, abs=0.02)
    assert tools.near(ST11.lat, ST11.lon, 2.9, kinds=["wfigs-perimeter"])["count"] == 0
    # A point inside the perimeter is at distance 0.
    inside_lon = seeded["perimeter_west_lon"] + 0.01
    inside = tools.near(ST11.lat, inside_lon, 0.0, kinds=["wfigs-perimeter"])["entities"]
    assert inside and inside[0]["distance_km"] == 0.0


def test_near_station_matches_independent_distances(tools):
    radius_km = 6.0
    expected = sorted(
        (haversine_m(ST11.lat, ST11.lon, s.lat, s.lon), f"lw-station-{s.id}")
        for s in STATIONS
        if s.id != "11" and haversine_m(ST11.lat, ST11.lon, s.lat, s.lon) <= radius_km * 1000.0
    )
    assert expected, "fixture should have neighbours within 6 km of Station 11"
    for spelling in ("11", "011", "Station 11", 11):
        out = tools.near_station(spelling, radius_km=radius_km, kinds=["lacofd-station"])
        assert out["station"]["entity_id"] == "lw-station-11"
        assert [e["entity_id"] for e in out["entities"]] == [eid for _, eid in expected]
    default = tools.near_station("11", radius_km=5.0)
    ids = {e["entity_id"] for e in default["entities"]}
    assert "lw-station-11" not in ids
    assert "lw-firms-probe-inside" in ids and "lw-firms-probe-outside" not in ids
    assert "error" in tools.near_station("9999")
    assert "error" in tools.near_station("no number")


def test_get_entity_and_missing_entity(tools):
    out = tools.get_entity("lw-station-11")
    assert out["name"] == ST11.name and out["data_type"] == "lacofd-station"
    assert out["template"] == "TEMPLATE_ASSET" and out["disposition"] == "DISPOSITION_FRIENDLY"
    assert "error" in tools.get_entity("lw-does-not-exist")


# ---------------------------------------------------------------------------------------------
# The one write: request_investigation
# ---------------------------------------------------------------------------------------------


def _seed_candidate(lattice):
    publish_analyst(lattice, ST11.lat, ST11.lon)
    lat, lon = destination(ST11.lat, ST11.lon, 45.0, 2_000.0)
    publish_point(
        lattice,
        CANDIDATE_ID,
        "ember-candidate",
        lat,
        lon,
        name="Talk test candidate",
        disposition="DISPOSITION_SUSPICIOUS",
    )


def test_request_investigation_without_confirm_creates_nothing(lattice, tools):
    _seed_candidate(lattice)
    before = len(all_tasks(lattice))
    out = tools.request_investigation(CANDIDATE_ID)
    assert out["created"] is False and out["dry_run"] is True
    assert out["would_create"]["objective_entity_id"] == CANDIDATE_ID
    assert out["would_create"]["assignee_entity_id"] == ANALYST_ID
    assert len(all_tasks(lattice)) == before
    assert investigations_on(lattice, CANDIDATE_ID) == []


def test_request_investigation_with_confirm_creates_one_task_with_objective(lattice, tools):
    _seed_candidate(lattice)
    out = tools.request_investigation(CANDIDATE_ID, confirm=True)
    assert out["created"] is True, out
    task_id = out["task"]["task_id"]

    # Read back through the SDK in a separate call.
    task = lattice.tasks.get_task(task_id)
    spec = task.specification.dict()
    assert spec["@type"] == INVESTIGATE_URL
    assert spec["objective"]["entity_id"] == CANDIDATE_ID
    assert task.relations.assignee.system.entity_id == ANALYST_ID
    assert task.display_name == "Investigate Talk test candidate"
    assert [task_id_of(t) for t in investigations_on(lattice, CANDIDATE_ID)] == [task_id]

    # A second confirmed request is refused while the first is open.
    again = tools.request_investigation(CANDIDATE_ID, confirm=True)
    assert again["created"] is False and again["existing"][0]["task_id"] == task_id
    assert len(investigations_on(lattice, CANDIDATE_ID)) == 1

    status = tools.task_status(task_id)
    assert status["objective_entity_id"] == CANDIDATE_ID and status["assignee_entity_id"] == ANALYST_ID


def test_request_investigation_rejects_bad_targets_and_assignees(lattice, tools):
    _seed_candidate(lattice)
    assert "error" in tools.request_investigation("lw-does-not-exist", confirm=True)
    # A fire station does not advertise Investigate, so it cannot be the assignee.
    out = tools.request_investigation(CANDIDATE_ID, confirm=True, assignee_entity_id="lw-station-11")
    assert "error" in out
    assert investigations_on(lattice, CANDIDATE_ID) == []


def test_request_investigation_needs_an_investigate_capable_asset(lattice, tools):
    lat, lon = destination(ST11.lat, ST11.lon, 45.0, 2_000.0)
    publish_point(lattice, CANDIDATE_ID, "ember-candidate", lat, lon, disposition="DISPOSITION_SUSPICIOUS")
    out = tools.request_investigation(CANDIDATE_ID, confirm=True)
    assert "error" in out and "Investigate-capable" in out["error"]
    assert investigations_on(lattice, CANDIDATE_ID) == []


def test_latest_brief_reads_objects_by_task_and_by_entity(lattice, tools):
    _seed_candidate(lattice)
    task_id = tools.request_investigation(CANDIDATE_ID, confirm=True)["task"]["task_id"]
    assert "error" in tools.latest_brief(entity_id=CANDIDATE_ID)  # no brief uploaded yet

    markdown = f"# Brief for {CANDIDATE_ID}\n\nFacts only.\n"
    lattice.objects.upload_object(f"{BRIEF_PREFIX}{task_id}.md", request=markdown.encode())
    lattice.objects.upload_object(
        f"{BRIEF_PREFIX}{task_id}.json", request=json.dumps({"target": CANDIDATE_ID}).encode()
    )
    by_entity = tools.latest_brief(entity_id=CANDIDATE_ID)
    assert by_entity["task_id"] == task_id and by_entity["markdown"] == markdown
    assert by_entity["facts"] == {"target": CANDIDATE_ID}
    assert tools.latest_brief(task_id=task_id)["markdown"] == markdown
    assert "error" in tools.latest_brief()
    assert "error" in tools.latest_brief(task_id="no-such-task")


# ---------------------------------------------------------------------------------------------
# Chat loop safety (scripted completion, real tools and harness)
# ---------------------------------------------------------------------------------------------


def test_ask_forces_dry_run_without_confirm_tasking(lattice, tools):
    _seed_candidate(lattice)
    replies = iter(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "request_investigation",
                            "arguments": json.dumps({"entity_id": CANDIDATE_ID, "confirm": True}),
                        },
                    }
                ],
            },
            {"role": "assistant", "content": f"Planned only [{CANDIDATE_ID}] [{ANALYST_ID}] [lw-made-up]."},
        ]
    )
    seen_tools: list = []

    def complete(messages, specs):
        seen_tools.append(specs)
        return next(replies)

    result = run_ask("Investigate the candidate", tools, complete)
    assert result.calls[0].arguments["confirm"] is False
    assert result.calls[0].result["dry_run"] is True
    assert investigations_on(lattice, CANDIDATE_ID) == []
    assert result.cited_ids == [CANDIDATE_ID, ANALYST_ID, "lw-made-up"]
    assert result.unsupported_ids == ["lw-made-up"]
    assert {s["function"]["name"] for s in seen_tools[0]} == set(TOOL_NAMES)


def test_tools_call_rejects_unknown_tools_and_bad_arguments(tools):
    assert "error" in tools.call("drop_everything", {})
    assert "bad arguments" in tools.call("near", {"lat": 34.0})["error"]
    assert tools.call("near", {"lat": ST11.lat, "lon": ST11.lon, "radius_km": 0.5})["count"] >= 1


# ---------------------------------------------------------------------------------------------
# MCP server (in-process)
# ---------------------------------------------------------------------------------------------


def test_mcp_server_lists_every_tool_without_touching_lattice():
    pytest.importorskip("mcp")
    from lattice_wildfire.talk.mcp_server import build_server

    def factory():
        raise AssertionError("tools/list must not build a Lattice client")

    listed = {t.name: t for t in asyncio.run(build_server(factory).list_tools())}
    assert set(listed) == set(TOOL_NAMES)
    confirm = listed["request_investigation"].input_schema["properties"]["confirm"]
    assert confirm["default"] is False
    assert set(listed["near"].input_schema["required"]) == {"lat", "lon", "radius_km"}
    assert listed["request_investigation"].annotations.read_only_hint is False
    assert all(listed[n].annotations.read_only_hint for n in TOOL_NAMES if n != "request_investigation")


def test_mcp_call_tool_runs_against_the_harness(lattice, seeded):
    pytest.importorskip("mcp")
    from lattice_wildfire.talk.mcp_server import build_server

    server = build_server(lambda: TalkTools(lattice, picture_ttl_s=0.0))
    result = asyncio.run(
        server.call_tool(
            "near_station", {"station_number": "11", "radius_km": 5.0, "kinds": ["firms-viirs-hotspot"]}
        )
    )
    assert not result.is_error
    payload = result.structured_content
    assert [e["entity_id"] for e in payload["entities"]] == ["lw-firms-probe-inside"]
