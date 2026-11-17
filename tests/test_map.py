"""lw-map: GeoJSON picture, tasks, briefs, static front end, loopback-only binding.

Everything runs against the local Lattice contract harness. No sandbox credentials exist on this
host, so nothing here claims a live Lattice run.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from anduril import Aliases, Entity, GoogleProtobufAny, MilView, Ontology, Principal, System, Tracked

from lattice_wildfire.core.entities import (
    point_location,
    polygon_geo,
    provenance,
    symbology,
    unclassified,
)
from lattice_wildfire.map.server import (
    BindRefused,
    brief_path,
    create_server,
    is_loopback,
    picture_collection,
    resolve_host,
    static_file,
)

INVESTIGATE_URL = "type.googleapis.com/anduril.tasks.v2.Investigate"
PREFIX = f"lw-test-map-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------------------------
# Harness seeding + an HTTP client for the map server
# ---------------------------------------------------------------------------------------------


def _hotspot(client, suffix: str, *, lat: float, lon: float, frp: float) -> str:
    entity_id = f"{PREFIX}-hotspot-{suffix}"
    client.entities.publish_entity(
        entity_id=entity_id,
        description="\n".join(
            [
                "FIRMS VIIRS active fire detection (NOAA-20)",
                "satellite: NOAA-20",
                "acquired: 2026-11-09T10:00:00Z",
                f"frp_mw: {frp}",
                "confidence: high",
            ]
        ),
        is_live=True,
        expiry_time=datetime.now(UTC) + timedelta(hours=2),
        location=point_location(lat, lon),
        aliases=Aliases(name=f"VIIRS NOAA-20 hotspot {suffix}"),
        mil_view=MilView(disposition="DISPOSITION_UNKNOWN", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_TRACK", platform_type="Wildfire Hotspot"),
        provenance=provenance("test", "firms-viirs-hotspot"),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_UNKNOWN", "ENVIRONMENT_LAND"),
    )
    return entity_id


def _perimeter(client, suffix: str, ring: list[tuple[float, float]]) -> str:
    entity_id = f"{PREFIX}-perimeter-{suffix}"
    details, shape = polygon_geo(ring, "GEO_TYPE_HAZARD")
    client.entities.publish_entity(
        entity_id=entity_id,
        description=f"WFIGS perimeter: Test Fire {suffix}\nirwin_id: TEST-{suffix}",
        is_live=True,
        expiry_time=datetime.now(UTC) + timedelta(hours=2),
        geo_details=details,
        geo_shape=shape,
        aliases=Aliases(name=f"Test Fire {suffix}"),
        ontology=Ontology(template="TEMPLATE_GEO", platform_type="Wildfire Perimeter"),
        provenance=provenance("test", "wfigs-perimeter"),
        data_classification=unclassified(),
    )
    return entity_id


def _station(client, suffix: str, *, lat: float, lon: float) -> str:
    entity_id = f"{PREFIX}-station-{suffix}"
    client.entities.publish_entity(
        entity_id=entity_id,
        description=f"Test Station {suffix}\nstation: {suffix}\ncity: Testville",
        is_live=True,
        expiry_time=datetime.now(UTC) + timedelta(hours=2),
        location=point_location(lat, lon),
        aliases=Aliases(name=f"Test Station {suffix}"),
        mil_view=MilView(disposition="DISPOSITION_FRIENDLY", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_ASSET", platform_type="Fire Station"),
        provenance=provenance("test", "lacofd-station"),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND"),
    )
    return entity_id


def _candidate(client, suffix: str, *, lat: float, lon: float, quality: int) -> str:
    entity_id = f"{PREFIX}-cand-{suffix}"
    client.entities.publish_entity(
        entity_id=entity_id,
        description=f"score: {quality / 15:.2f} (threshold 0.40)\nFRP sum: 4.20 MW",
        is_live=True,
        created_time=datetime.now(UTC) - timedelta(minutes=20),
        expiry_time=datetime.now(UTC) + timedelta(hours=1),
        location=point_location(lat, lon),
        aliases=Aliases(name=f"New start candidate {suffix}"),
        mil_view=MilView(disposition="DISPOSITION_SUSPICIOUS", environment="ENVIRONMENT_LAND"),
        ontology=Ontology(template="TEMPLATE_TRACK", platform_type="New Start Candidate"),
        tracked=Tracked(track_quality_wrapper=quality, sensor_hits=3),
        provenance=provenance("test", "ember-candidate"),
        data_classification=unclassified(),
        symbology=symbology("DISPOSITION_SUSPICIOUS", "ENVIRONMENT_LAND"),
    )
    return entity_id


def _create_task(client, objective_entity_id: str, name: str) -> str:
    created = client.tasks.create_task(
        display_name=f"Investigate {name}",
        description=f"Test Investigate task created by tests/test_map.py for {objective_entity_id}.",
        specification=GoogleProtobufAny(type=INVESTIGATE_URL, objective={"entity_id": objective_entity_id}),
        author=Principal(system=System(service_name="lattice-wildfire-test")),
        is_executed_elsewhere=False,
    )
    return created.version.task_id


@pytest.fixture
def picture_server(lattice) -> Iterator[str]:
    """The real map server on a free loopback port, serving the harness through the SDK client."""
    server = create_server(lattice, port=0, picture_ttl_s=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


# ---------------------------------------------------------------------------------------------
# /api/picture
# ---------------------------------------------------------------------------------------------


def test_picture_is_geojson_from_the_harness(picture_server: str, lattice) -> None:
    hotspot_id = _hotspot(lattice, "a", lat=34.2, lon=-118.3, frp=12.5)
    perimeter_id = _perimeter(
        lattice, "a", [(34.30, -118.40), (34.30, -118.30), (34.20, -118.30), (34.20, -118.40)]
    )
    station_id = _station(lattice, "a", lat=34.11, lon=-118.22)
    candidate_id = _candidate(lattice, "a", lat=34.25, lon=-118.35, quality=9)

    status, content_type, body = _get(f"{picture_server}/api/picture")
    assert status == 200
    assert content_type.startswith("application/json")
    document = json.loads(body)
    assert document["type"] == "FeatureCollection"

    features = {feature["id"]: feature for feature in document["features"]}
    # The harness is shared, so assert on the ids this test created only.
    assert {hotspot_id, perimeter_id, station_id, candidate_id} <= set(features)

    hotspot = features[hotspot_id]
    assert hotspot["type"] == "Feature"
    assert hotspot["geometry"]["type"] == "Point"
    lon, lat = hotspot["geometry"]["coordinates"]
    assert lon == pytest.approx(-118.3, abs=1e-6)
    assert lat == pytest.approx(34.2, abs=1e-6)
    hotspot_props = hotspot["properties"]
    assert hotspot_props["data_type"] == "firms-viirs-hotspot"
    assert hotspot_props["layer"] == "hotspot"
    assert hotspot_props["frp_mw"] == pytest.approx(12.5)
    assert hotspot_props["confidence"] == "high"
    assert hotspot_props["name"].startswith("VIIRS NOAA-20 hotspot")

    perimeter = features[perimeter_id]
    assert perimeter["geometry"]["type"] == "Polygon"
    rings = perimeter["geometry"]["coordinates"]
    assert len(rings) == 1
    ring = rings[0]
    assert len(ring) >= 4
    assert ring[0] == ring[-1]  # closed ring, RFC 7946
    assert ring[0] == [-118.40, 34.30]  # GeoJSON axis order is lon, lat
    assert perimeter["properties"]["data_type"] == "wfigs-perimeter"
    assert perimeter["properties"]["layer"] == "perimeter"
    assert perimeter["properties"]["rings"] == 1

    station = features[station_id]
    assert station["properties"]["layer"] == "station"
    assert station["properties"]["station"] == "a"
    assert station["properties"]["city"] == "Testville"

    candidate = features[candidate_id]
    assert candidate["properties"]["layer"] == "candidate"
    assert candidate["properties"]["score"] == pytest.approx(0.6, abs=1e-3)
    assert candidate["properties"]["sensor_hits"] == 3

    counts = document["meta"]["counts"]
    assert counts["firms-viirs-hotspot"] >= 1
    assert document["meta"]["aoi"]["min_lon"] < document["meta"]["aoi"]["max_lon"]


def test_picture_collection_skips_dead_expired_and_geometryless() -> None:
    """Lattice refuses ``isLive=false`` publishes, so liveness and expiry are exercised on the
    pure collection builder with SDK ``Entity`` objects instead of through the harness."""
    now = datetime.now(UTC)
    entities = [
        Entity(entity_id="lw-test-map-dead", is_live=False),
        Entity(entity_id="lw-test-map-expired", is_live=True, expiry_time=now - timedelta(seconds=1)),
        Entity(entity_id="lw-test-map-nogeo", is_live=True, expiry_time=now + timedelta(hours=1)),
    ]
    collection = picture_collection(entities, now=now)
    assert collection["type"] == "FeatureCollection"
    assert collection["features"] == []
    assert collection["meta"]["skipped"] == {"expired": 1, "no_geometry": 1, "not_live": 1}


# ---------------------------------------------------------------------------------------------
# /api/tasks and /api/brief
# ---------------------------------------------------------------------------------------------


def test_tasks_endpoint_reports_status(picture_server: str, lattice) -> None:
    candidate_id = _candidate(lattice, "tasks", lat=34.31, lon=-118.42, quality=6)
    task_id = _create_task(lattice, candidate_id, "tasks candidate")

    status, content_type, body = _get(f"{picture_server}/api/tasks")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    tasks = {task["task_id"]: task for task in payload["tasks"]}
    assert task_id in tasks
    task = tasks[task_id]
    assert task["objective_entity_id"] == candidate_id
    assert task["type"] == INVESTIGATE_URL
    assert task["status"].startswith("STATUS_")
    assert task["status_key"] == task["status"].removeprefix("STATUS_").lower()
    assert task["brief_path"] == brief_path(task_id)
    assert task["display_name"] == "Investigate tasks candidate"


def test_brief_endpoint_serves_uploaded_markdown_and_404s(picture_server: str, lattice) -> None:
    candidate_id = _candidate(lattice, "brief", lat=34.32, lon=-118.44, quality=12)
    task_id = _create_task(lattice, candidate_id, "brief candidate")
    markdown = f"# Brief {task_id}\n\nLOCATION: Test Canyon, 34.32, -118.44.\n"
    lattice.objects.upload_object(brief_path(task_id), request=markdown.encode("utf-8"))

    status, content_type, body = _get(f"{picture_server}/api/brief/{task_id}")
    assert status == 200
    assert content_type.startswith("text/markdown")
    assert body.decode("utf-8") == markdown

    missing = f"{PREFIX}-task-that-does-not-exist"
    status, content_type, body = _get(f"{picture_server}/api/brief/{missing}")
    assert status == 404
    assert content_type.startswith("application/json")
    error = json.loads(body)
    assert missing in error["error"]
    assert error["path"] == brief_path(missing)


# ---------------------------------------------------------------------------------------------
# Binding, static files, pure helpers
# ---------------------------------------------------------------------------------------------


def test_server_refuses_a_non_loopback_bind_by_default(lattice) -> None:
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("10.0.0.5")

    with pytest.raises(BindRefused):
        create_server(lattice, host="0.0.0.0", port=0)
    with pytest.raises(BindRefused):
        create_server(lattice, host="0.0.0.0", port=0, explicit_host=False)

    assert resolve_host(None, explicit=False) == ("127.0.0.1", None)

    host, warning = resolve_host("0.0.0.0", explicit=True)
    assert host == "0.0.0.0"
    assert warning is not None
    assert "not loopback" in warning
    assert "no authentication" in warning


def test_front_end_is_served_locally(picture_server: str) -> None:
    status, content_type, body = _get(f"{picture_server}/")
    assert status == 200
    assert content_type.startswith("text/html")
    html = body.decode("utf-8")
    assert "/vendor/leaflet/leaflet.js" in html
    assert "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" in html
    assert "unpkg.com" not in html
    assert html.count("card card-list") == 2  # candidates and tasks scroll on their own
    assert "card card-brief" in html  # the brief takes the remaining height

    status, content_type, body = _get(f"{picture_server}/vendor/leaflet/leaflet.js")
    assert status == 200
    assert content_type.startswith("text/javascript")
    assert b"Leaflet 1.9.4" in body

    status, content_type, _ = _get(f"{picture_server}/vendor/leaflet/leaflet.css")
    assert status == 200
    assert content_type.startswith("text/css")

    status, _, body = _get(f"{picture_server}/app.js")
    assert status == 200
    script = body.decode("utf-8")
    assert "innerHTML" not in script  # brief markdown is inserted with textContent only
    assert "L.control.layers" not in script  # one control: the legend, with checkboxes
    assert "legend-check" in script
    assert "invalidateSize" in script
    assert "/api/health" in script
    assert "/vendor/census/ca-counties-5m.geojson" in script
    assert "pane: 'stations'" in script
    assert "flyTo" in script  # selecting a candidate zooms the map to it
    assert "scrollIntoView" in script
    assert "brief-note is-loading" in script  # loading state before the brief text

    status, _, body = _get(f"{picture_server}/app.css")
    assert status == 200
    css = body.decode("utf-8")
    assert "grid-template-rows: minmax(0, 1fr)" in css  # the map row has a definite height
    assert "max-height: 38%" in css  # candidate and task cards scroll on their own
    assert "flex: 0 0 auto" in css  # list cards never shrink below their content
    assert "min-height: 30%" in css  # the brief always keeps at least 30% of the panel
    assert ".station-square" in css

    status, content_type, body = _get(f"{picture_server}/vendor/census/ca-counties-5m.geojson")
    assert status == 200
    assert content_type.startswith("application/geo+json")
    counties = json.loads(body)
    assert counties["type"] == "FeatureCollection"
    assert len(counties["features"]) == 58  # every California county
    geoids = {feature["properties"]["geoid"] for feature in counties["features"]}
    assert {"06037", "06059", "06065"} <= geoids  # Los Angeles, Orange, Riverside
    assert "public domain" in counties["metadata"]["license"].lower()
    assert len(body) < 300 * 1024

    status, content_type, _ = _get(f"{picture_server}/vendor/census/NOTICE.md")
    assert status == 200
    assert content_type.startswith("text/markdown")

    status, _, body = _get(f"{picture_server}/api/health")
    assert status == 200
    assert json.loads(body)["ok"] is True

    status, _, _ = _get(f"{picture_server}/../server.py")
    assert status == 404
    assert static_file("/../server.py") is None
    assert static_file("/vendor/leaflet/LICENSE") is not None
