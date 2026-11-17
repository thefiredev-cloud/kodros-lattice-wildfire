from __future__ import annotations

import json
import random
import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from anduril import Entity, Location, Position

from lattice_wildfire.core import entities as ent
from lattice_wildfire.core import geo, layers
from lattice_wildfire.core.config import AOI, ConfigError, LatticeSettings, aoi_from_env
from lattice_wildfire.core.http import FetchError, arcgis_query, fetch_json, fetch_text
from lattice_wildfire.core.llm import LLMUnavailable, chat, chat_result, models

FIXTURES = Path(__file__).parent / "fixtures" / "core"
SECRET = "s3cr3t-do-not-print"
_ENV = (
    "LATTICE_BASE_URL",
    "LATTICE_ENDPOINT",
    "ENVIRONMENT_TOKEN",
    "SANDBOXES_TOKEN",
    "LATTICE_CLIENT_ID",
    "LATTICE_CLIENT_SECRET",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---------------------------------------------------------------- config
def test_settings_missing_everything_names_variables(clean_env):
    with pytest.raises(ConfigError) as info:
        LatticeSettings.from_env(dotenv=False)
    msg = str(info.value)
    assert "LATTICE_ENDPOINT" in msg and "ENVIRONMENT_TOKEN" in msg


def test_settings_endpoint_with_scheme_rejected_without_echoing_value(clean_env):
    clean_env.setenv("LATTICE_ENDPOINT", "https://abc.env.sandboxes.developer.anduril.com")
    clean_env.setenv("ENVIRONMENT_TOKEN", SECRET)
    with pytest.raises(ConfigError) as info:
        LatticeSettings.from_env(dotenv=False)
    assert "scheme" in str(info.value)
    assert "abc.env" not in str(info.value) and SECRET not in str(info.value)


def test_settings_client_id_without_secret(clean_env):
    clean_env.setenv("LATTICE_ENDPOINT", "abc.example.com")
    clean_env.setenv("LATTICE_CLIENT_ID", SECRET)
    with pytest.raises(ConfigError) as info:
        LatticeSettings.from_env(dotenv=False)
    assert "LATTICE_CLIENT_SECRET" in str(info.value)
    assert SECRET not in str(info.value)


def test_settings_endpoint_and_base_url_precedence(clean_env):
    clean_env.setenv("LATTICE_ENDPOINT", "abc.example.com")
    clean_env.setenv("ENVIRONMENT_TOKEN", SECRET)
    s = LatticeSettings.from_env(dotenv=False)
    assert s.base_url == "https://abc.example.com"
    assert s.auth_mode == "environment-token"
    clean_env.setenv("LATTICE_BASE_URL", "http://127.0.0.1:8765/")
    assert LatticeSettings.from_env(dotenv=False).base_url == "http://127.0.0.1:8765"


def test_settings_repr_hides_secrets(clean_env):
    clean_env.setenv("LATTICE_ENDPOINT", "abc.example.com")
    clean_env.setenv("ENVIRONMENT_TOKEN", SECRET)
    clean_env.setenv("SANDBOXES_TOKEN", SECRET + "-sbx")
    clean_env.setenv("LATTICE_CLIENT_ID", SECRET + "-id")
    clean_env.setenv("LATTICE_CLIENT_SECRET", SECRET + "-cs")
    s = LatticeSettings.from_env(dotenv=False)
    for text in (repr(s), str(s), f"{s}", repr([s])):
        assert SECRET not in text
        assert "<set>" in text
    assert s.sandboxes_token == SECRET + "-sbx"


def test_aoi_default_and_override(monkeypatch):
    monkeypatch.delenv("LW_AOI_BBOX", raising=False)
    a = aoi_from_env()
    assert (a.min_lon, a.min_lat, a.max_lon, a.max_lat) == (-118.95, 33.28, -117.65, 34.82)
    monkeypatch.setenv("LW_AOI_BBOX", "-119,34,-118,35")
    assert aoi_from_env() == AOI(min_lat=34, min_lon=-119, max_lat=35, max_lon=-118)
    monkeypatch.setenv("LW_AOI_BBOX", "1,2,3")
    with pytest.raises(ConfigError):
        aoi_from_env()
    with pytest.raises(ConfigError):
        AOI(min_lat=35, min_lon=-119, max_lat=34, max_lon=-118)


# ---------------------------------------------------------------- entities
@pytest.mark.parametrize(
    ("disposition", "environment", "function", "expected"),
    [
        ("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND", "------", "SFGP-----------"),
        ("DISPOSITION_HOSTILE", "ENVIRONMENT_AIR", "------", "SHAP-----------"),
        ("DISPOSITION_UNKNOWN", "ENVIRONMENT_SURFACE", "UC----", "SUSPUC---------"),
        ("DISPOSITION_NEUTRAL", "ENVIRONMENT_SUB_SURFACE", "------", "SNUP-----------"),
        ("DISPOSITION_PENDING", "ENVIRONMENT_LAND", "------", "SUGP-----------"),
        ("DISPOSITION_SUSPICIOUS", "ENVIRONMENT_LAND", "------", "SSGP-----------"),
        ("DISPOSITION_ASSUMED_FRIENDLY", "ENVIRONMENT_SPACE", "------", "SAPP-----------"),
        ("friendly", "land", "------", "SFGP-----------"),
    ],
)
def test_sidc_mapping(disposition, environment, function, expected):
    code = ent.sidc(disposition, environment, function)
    assert code == expected
    assert len(code) == 15


def test_sidc_rejects_unknown_literals():
    with pytest.raises(ValueError):
        ent.sidc("DISPOSITION_ENEMY", "ENVIRONMENT_LAND")
    with pytest.raises(ValueError):
        ent.sidc("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND", "UC")


def test_symbology_wraps_sidc():
    sym = ent.symbology("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND")
    assert sym.mil_std2525c.sidc == "SFGP-----------"


def test_provenance_expiry_classification():
    p = ent.provenance("feeds", "firms-viirs-hotspot")
    assert p.integration_name == "lattice-wildfire-feeds"
    assert p.data_type == "firms-viirs-hotspot"
    assert p.source_update_time.tzinfo is not None
    e = ent.expiry(60)
    assert timedelta(seconds=55) < e - ent.now_utc() <= timedelta(seconds=60)
    with pytest.raises(ValueError):
        ent.expiry(30 * 24 * 3600)
    with pytest.raises(ValueError):
        ent.expiry(0)
    assert ent.unclassified().default.level == "CLASSIFICATION_LEVELS_UNCLASSIFIED"


def test_polygon_geo_closes_ring_and_lat_lon_of():
    ring = [(34.0, -118.0), (34.0, -117.9), (34.1, -117.9), (34.1, -118.0)]
    details, shape = ent.polygon_geo(ring)
    assert details.type == "GEO_TYPE_GENERAL"
    positions = shape.polygon.rings[0].positions
    assert len(positions) == 5
    assert positions[0].position == positions[-1].position
    entity = Entity(entity_id="x", geo_shape=shape, geo_details=details)
    lat, lon = ent.lat_lon_of(entity)
    assert lat == pytest.approx(34.05) and lon == pytest.approx(-117.95)
    with pytest.raises(ValueError):
        ent.polygon_geo([(34.0, -118.0), (34.1, -118.0)])


def test_lat_lon_of_prefers_location_and_data_type_of():
    entity = Entity(
        entity_id="x",
        location=ent.point_location(34.1, -118.2, 300.0),
        provenance=ent.provenance("feeds", "wfigs-incident"),
    )
    assert ent.lat_lon_of(entity) == (34.1, -118.2)
    assert ent.data_type_of(entity) == "wfigs-incident"
    assert ent.lat_lon_of(Entity(entity_id="y", location=Location(position=Position()))) is None
    assert ent.data_type_of(Entity(entity_id="z")) is None


# ---------------------------------------------------------------- geo
def test_haversine_known_distances():
    # One degree of latitude on the mean-radius sphere.
    assert geo.haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-4)
    # LAX -> JFK great circle is about 3,983 km.
    assert geo.haversine_m(33.9425, -118.4081, 40.6413, -73.7781) == pytest.approx(3_983_000, rel=0.005)
    assert geo.haversine_m(34.1, -118.2, 34.1, -118.2) == 0


def test_bearing_cardinal_directions():
    assert geo.bearing_deg(0, 0, 1, 0) == pytest.approx(0)
    assert geo.bearing_deg(0, 0, 0, 1) == pytest.approx(90)
    assert geo.bearing_deg(0, 0, -1, 0) == pytest.approx(180)
    assert geo.bearing_deg(0, 0, 0, -1) == pytest.approx(270)


def test_polygon_helpers_and_bbox():
    square = [(0, 0), (0, 2), (2, 2), (2, 0), (0, 0)]
    assert geo.centroid(square) == pytest.approx((1.0, 1.0))
    assert geo.point_in_polygon(1, 1, square)
    assert not geo.point_in_polygon(3, 1, square)
    la = aoi_from_env()
    assert geo.bbox_contains(la, 34.05, -118.25)
    assert not geo.bbox_contains(la, 37.77, -122.42)


def test_geohash_reference_value():
    assert geo.geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert geo.geohash(34.1478, -118.1445, 7) == geo.geohash(34.1478, -118.1445, 9)[:7]


def _brute_clusters(points, radius):
    n = len(points)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if geo.haversine_m(*points[i], *points[j]) <= radius:
                parent[max(find(i), find(j))] = min(find(i), find(j))
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def test_cluster_points_single_linkage_and_deterministic():
    # A-B 900 m, B-C 900 m, A-C 1800 m: single linkage chains them at radius 1 km.
    pts = [(34.0, -118.0), (34.0081, -118.0), (34.0162, -118.0), (34.5, -118.5)]
    assert geo.cluster_points(pts, 1000) == [[0, 1, 2], [3]]
    rng = random.Random(20261109)
    cloud = [(34.0 + rng.random() * 0.3, -118.4 + rng.random() * 0.3) for _ in range(400)]
    first = geo.cluster_points(cloud, 1500)
    assert first == geo.cluster_points(list(cloud), 1500)
    assert first == _brute_clusters(cloud, 1500)
    assert sorted(i for c in first for i in c) == list(range(len(cloud)))
    assert geo.cluster_points([], 10) == []


# ---------------------------------------------------------------- layers (real captured responses)
def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_fire_station_fixture_parses_and_dedupes():
    raw = _fixture("lacounty_fire_stations_2026-11-09.geojson")
    assert len(raw["features"]) == 173  # Station 182 appears twice in the live layer
    stations = layers.facilities_from_geojson(raw, "fire_station")
    assert len(stations) == 172
    assert all(s.kind == "fire_station" for s in stations)
    assert all(s.attrs["org_name"] == layers.LACOFD_ORG_NAME for s in stations)
    assert all(s.id.isdigit() or s.id[:-1].isdigit() for s in stations)
    assert len({s.id for s in stations}) == len(stations)
    la = aoi_from_env()
    assert all(geo.bbox_contains(la, s.lat, s.lon) for s in stations)
    by_id = {s.id: s for s in stations}
    assert by_id["63"].name == "Los Angeles County Fire Department - Station 63"
    assert by_id["182"].attrs["OBJECTID"] == 16579  # the newer of the two records
    assert by_id["182"].name == "Los Angeles County Fire Department - Station 182"


def test_hospital_and_heliport_fixtures_parse():
    for name, kind in (
        ("lacounty_hospitals_2026-11-09.geojson", "hospital"),
        ("lacounty_heliports_2026-11-09.geojson", "heliport"),
    ):
        raw = _fixture(name)
        parsed = layers.facilities_from_geojson(raw, kind)
        distinct = {str(f["properties"]["ext_id"]) for f in raw["features"]}
        assert len(parsed) == len(distinct) > 100
        assert all(f.name and f.kind == kind for f in parsed)


def test_fhsz_fixture_classes():
    assert (
        layers.fhsz_class_from_geojson(_fixture("lacounty_fhsz_34.2000_-118.2000_2026-11-09.geojson"))
        == "Very High"
    )
    assert (
        layers.fhsz_class_from_geojson(_fixture("lacounty_fhsz_34.1478_-118.1445_2026-11-09.geojson"))
        == "NonWildland"
    )
    assert layers.fhsz_class_from_geojson({"type": "FeatureCollection", "features": []}) is None
    assert layers.station_number("Los Angeles County Fire Department - Station 101") == "101"


# ---------------------------------------------------------------- http cache (local real server)
class _Origin:
    """A tiny local HTTP origin serving fixed bodies; ArcGIS paging is emulated for one path."""

    def __init__(self) -> None:
        origin = self
        self.hits = 0
        self.paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                origin.hits += 1
                origin.paths.append(self.path)
                self.server_version = "origin"
                if self.path.startswith("/layer/0/query"):
                    offset = int(self.path.split("resultOffset=")[1].split("&")[0])
                    feats = [
                        {"type": "Feature", "id": i, "geometry": None, "properties": {"n": i}}
                        for i in range(offset, min(offset + 2, 5))
                    ]
                    body = {"type": "FeatureCollection", "features": feats}
                    if offset + 2 < 5:
                        body["properties"] = {"exceededTransferLimit": True}
                    data = json.dumps(body).encode()
                elif self.path.startswith("/error"):
                    data = json.dumps({"error": {"code": 400, "message": "bad"}}).encode()
                else:
                    data = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_fetch_json_cache_then_stale_fallback(tmp_path):
    origin = _Origin()
    try:
        first = fetch_json(f"{origin.url}/a", {"x": 1}, ttl_s=300, cache_dir=tmp_path)
        assert first.data == {"ok": True, "path": "/a?x=1"}
        assert (first.from_cache, first.stale) == (False, False)
        second = fetch_json(f"{origin.url}/a", {"x": 1}, ttl_s=300, cache_dir=tmp_path)
        assert (second.from_cache, second.stale) == (True, False)
        assert origin.hits == 1
        text = fetch_text(f"{origin.url}/t", cache_dir=tmp_path)
        assert json.loads(text.data)["path"] == "/t"
    finally:
        origin.stop()
    stale = fetch_json(f"{origin.url}/a", {"x": 1}, ttl_s=0, cache_dir=tmp_path)
    assert (stale.from_cache, stale.stale) == (True, True)
    assert stale.data == first.data
    with pytest.raises(FetchError):
        fetch_json(f"{origin.url}/never-fetched", cache_dir=tmp_path)


def test_arcgis_query_pages_and_rejects_errors(tmp_path):
    origin = _Origin()
    try:
        fc = arcgis_query(f"{origin.url}/layer/0", page_size=2, cache_dir=tmp_path)
        assert [f["properties"]["n"] for f in fc.data["features"]] == [0, 1, 2, 3, 4]
        assert fc.from_cache is False
        with pytest.raises(FetchError):
            arcgis_query(f"{origin.url}/error", cache_dir=tmp_path)
    finally:
        origin.stop()


def test_arcgis_query_sends_max_allowable_offset(tmp_path):
    origin = _Origin()
    try:
        fc = arcgis_query(
            f"{origin.url}/layer/0", page_size=2, max_allowable_offset=0.0001, cache_dir=tmp_path
        )
        assert len(fc.data["features"]) == 5
        assert len(origin.paths) == 3  # the query string is on every page, not only the first
        assert all("maxAllowableOffset=0.0001" in p for p in origin.paths)
        assert all("outSR=4326" in p and "f=geojson" in p for p in origin.paths)

        origin.paths.clear()
        arcgis_query(f"{origin.url}/layer/0", where="1=1 AND 2=2", page_size=5, cache_dir=tmp_path)
        assert origin.paths and all("maxAllowableOffset" not in p for p in origin.paths)

        with pytest.raises(ValueError):
            arcgis_query(f"{origin.url}/layer/0", max_allowable_offset=0, cache_dir=tmp_path)
    finally:
        origin.stop()


# ---------------------------------------------------------------- llm
class _LLMStub:
    """Local stdlib HTTP server replaying the captured Kodros cluster bodies (no cluster needed)."""

    def __init__(self, chat_body: dict | None = None) -> None:
        self.chat_body = (
            chat_body if chat_body is not None else _fixture("kodros_chat_completion_2026-11-09.json")
        )
        self.models_body = _fixture("kodros_models_2026-11-09.json")
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(stub.models_body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append(json.loads(raw or b"{}"))
                self._send(stub.chat_body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_chat_raises_llm_unavailable_on_closed_port(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("KODROS_LLM_URL", f"http://127.0.0.1:{port}/v1")
    with pytest.raises(LLMUnavailable):
        chat([{"role": "user", "content": "Say OK"}])
    with pytest.raises(LLMUnavailable):
        models()


def test_chat_result_reports_served_model_and_usage(monkeypatch):
    stub = _LLMStub()
    try:
        monkeypatch.setenv("KODROS_LLM_URL", stub.url)
        monkeypatch.delenv("KODROS_LLM_MODEL", raising=False)
        result = chat_result([{"role": "user", "content": "Nearest city to the Eaton Fire ignition area?"}])
        # Captured 2026-11-09 from the live cluster: the alias was cluster-current, the served weights
        # are what the response body reports and what a caller records as provenance.
        assert result.model == "DeepSeek-V4.1-Flash-1017-EXL3"
        assert (
            result.text
            == "The nearest Los Angeles County city to the 2025 Eaton Fire ignition area is Pasadena."
        )
        assert result.usage["total_tokens"] == 293
        assert result.usage["completion_tokens_details"]["reasoning_tokens"] == 207
        assert 0 <= result.latency_s < 30
        assert stub.requests[0]["model"] == "cluster-current"
        assert stub.requests[0]["stream"] is False
        assert stub.requests[0]["messages"][0]["role"] == "user"
        assert chat([{"role": "user", "content": "again"}]) == result.text
        assert len(stub.requests) == 2
    finally:
        stub.stop()


def test_chat_result_requires_model_id(monkeypatch):
    body = _fixture("kodros_chat_completion_2026-11-09.json")
    for mutate, expected in (
        (lambda b: b.pop("model"), "no model id"),
        (lambda b: b["choices"][0]["message"].update(content="   "), "empty message"),
        (lambda b: b["choices"][0]["message"].pop("content"), "empty message"),
    ):
        broken = json.loads(json.dumps(body))
        mutate(broken)
        stub = _LLMStub(broken)
        try:
            monkeypatch.setenv("KODROS_LLM_URL", stub.url)
            with pytest.raises(LLMUnavailable) as info:
                chat_result([{"role": "user", "content": "x"}])
            assert expected in str(info.value)
        finally:
            stub.stop()


def test_models_lists_alias_and_loaded_weights(monkeypatch):
    stub = _LLMStub()
    try:
        monkeypatch.setenv("KODROS_LLM_URL", stub.url)
        listed = models()
        assert [m.id for m in listed] == ["DeepSeek-V4.1-Flash-1017-EXL3", "cluster-current"]
        assert listed[0].loaded_model is None
        assert listed[1].owned_by == "tensorfold"
        assert listed[1].loaded_model == "DeepSeek-V4.1-Flash-1017-EXL3"
    finally:
        stub.stop()


def test_cluster_points_wraps_longitudes_near_the_pole():
    points = [(89.99, -179.999), (89.99, 179.999), (89.98, 0)]

    assert geo.cluster_points(points, 5) == [[0, 1], [2]]


def test_cluster_points_handles_continental_radius():
    points = [(0, 0), (5, 0), (20, 0)]

    assert geo.cluster_points(points, 1_200_000) == [[0, 1], [2]]
