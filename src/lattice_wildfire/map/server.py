"""Read-only local operating picture: a stdlib HTTP server over one Lattice entity stream.

The server only reads Lattice (``stream_entities``, ``query_tasks``, ``objects.get_object``) and
renders JSON plus a static front end. Nothing is published and no state is kept beyond a short
picture cache, so it needs no authentication — which is exactly why it binds loopback only. A
non-loopback bind is refused unless the caller asks for it explicitly (``--host``), and that
override prints a warning.

Endpoints
    GET /api/picture      GeoJSON FeatureCollection of the current picture, grouped by
                          ``provenance.data_type`` (points for tracks/assets, polygons for areas)
    GET /api/tasks        Recent Lattice tasks with their status
    GET /api/brief/{id}   Analyst brief markdown Objects ``lattice-wildfire/briefs/{id}.md``
    GET /api/health       Liveness plus the resolved AOI
    GET /                 Static front end (index.html, app.js, app.css, vendor/leaflet/...)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import socketserver
import threading
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from anduril import Entity, NotFoundError, Task
from anduril.core.api_error import ApiError

from lattice_wildfire.core.config import AOI, aoi_from_env
from lattice_wildfire.core.entities import data_type_of, lat_lon_of

log = logging.getLogger("lattice_wildfire.map")

INTEGRATION = "lattice-wildfire-map"
BRIEF_PREFIX = "lattice-wildfire/briefs/"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
PICTURE_TTL_S = 3.0
MAX_TASK_PAGES = 50
TASK_LIMIT = 50
DESCRIPTION_CHARS = 600
STATIC_DIR = Path(__file__).with_name("static")

#: ``provenance.data_type`` -> ``(layer key, GeoJSON geometry type)``. One layer per data_type,
#: so the map and the side panel use the contract vocabulary directly.
LAYERS: dict[str, tuple[str, str]] = {
    "firms-viirs-hotspot": ("hotspot", "Point"),
    "wfigs-incident": ("incident", "Point"),
    "calfire-incident": ("incident", "Point"),
    "wfigs-perimeter": ("perimeter", "Polygon"),
    "nws-red-flag": ("red-flag", "Polygon"),
    "lacofd-station": ("station", "Point"),
    "ember-candidate": ("candidate", "Point"),
    "kodros-analyst": ("analyst", "Point"),
}
UNKNOWN_LAYER = "other"
CANDIDATE_TYPE = "ember-candidate"
TRACK_QUALITY_MAX = 15

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".geojson": "application/geo+json",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_KV_LINE = re.compile(r"^\s*([a-z0-9_]+)\s*:\s*(.*?)\s*$")


class BindRefused(RuntimeError):
    """A non-loopback bind was requested without being asked for explicitly."""


# ---------------------------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------------------------


def is_loopback(host: str) -> bool:
    """True for ``127.0.0.0/8``, ``::1`` and ``localhost``. Anything unresolved is not loopback."""
    value = host.strip().strip("[]").lower()
    if value in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def resolve_host(host: str | None, *, explicit: bool) -> tuple[str, str | None]:
    """``(bind host, warning)``. Refuses a non-loopback host that was not passed explicitly.

    ``explicit`` means the caller named the host themselves (``lw-map --host 0.0.0.0``). Refusal
    covers the default path so a plain ``lw-map`` can never end up serving an unauthenticated
    picture on a real interface.
    """
    resolved = (host or DEFAULT_HOST).strip()
    if resolved in ("", "*"):
        resolved = DEFAULT_HOST
    if is_loopback(resolved):
        return resolved, None
    if not explicit:
        raise BindRefused(
            f"refusing to bind {resolved}: lw-map serves an unauthenticated picture. "
            f"It binds {DEFAULT_HOST} by default; pass --host {resolved} to override on purpose."
        )
    return resolved, (
        f"binding {resolved}, which is not loopback: the picture has no authentication, so anyone "
        "who can reach this port can read it."
    )


# ---------------------------------------------------------------------------------------------
# Pure geometry / entity helpers
# ---------------------------------------------------------------------------------------------


def description_fields(description: str | None) -> dict[str, str]:
    """``key: value`` lines of a feeds-style description (first occurrence wins)."""
    fields: dict[str, str] = {}
    for line in (description or "").splitlines():
        match = _KV_LINE.match(line)
        if match:
            fields.setdefault(match.group(1), match.group(2))
    return fields


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def ring_coordinates(positions: Iterable[Any] | None) -> list[list[float]]:
    """One Lattice ring as closed GeoJSON ``[[lon, lat], ...]`` (RFC 7946 axis order)."""
    coords: list[list[float]] = []
    for geo_position in positions or []:
        position = getattr(geo_position, "position", None)
        if position is None:
            continue
        lat, lon = position.latitude_degrees, position.longitude_degrees
        if lat is None or lon is None:
            continue
        coords.append([float(lon), float(lat)])
    if coords and coords[0] != coords[-1]:
        coords.append(list(coords[0]))
    return coords


def polygon_coordinates(entity: Entity) -> list[list[list[float]]]:
    """Exterior ring first, holes after, each closed. Empty when the entity has no usable polygon."""
    shape = entity.geo_shape
    polygon = shape.polygon if shape is not None else None
    if polygon is None:
        return []
    rings = (ring_coordinates(ring.positions) for ring in polygon.rings or [])
    return [ring for ring in rings if len(ring) >= 4]


def candidate_score(entity: Entity) -> float | None:
    """Ember Fusion score. ``tracked.track_quality_wrapper`` is ``round(score * 15)``; older or
    hand-published candidates carry ``score: 0.55`` in the description."""
    tracked = entity.tracked
    quality = getattr(tracked, "track_quality_wrapper", None)
    if quality is not None:
        return round(min(1.0, max(0.0, float(quality) / TRACK_QUALITY_MAX)), 3)
    score = _as_float(description_fields(entity.description).get("score"))
    return None if score is None else round(score, 3)


def hotspot_frp(entity: Entity) -> float | None:
    """Fire radiative power (MW) from the feeds description, if this entity carries one."""
    return _as_float(description_fields(entity.description).get("frp_mw"))


def layer_of(entity: Entity) -> str:
    return LAYERS.get(data_type_of(entity) or "", (UNKNOWN_LAYER, "Point"))[0]


def feature_for(entity: Entity, *, now: datetime | None = None) -> dict[str, Any] | None:
    """One GeoJSON feature, or ``None`` when the entity has no renderable geometry."""
    now = now or datetime.now(UTC)
    entity_id = entity.entity_id
    if not entity_id:
        return None
    if entity.is_live is False:
        return None
    if entity.expiry_time is not None and entity.expiry_time <= now:
        return None

    data_type = data_type_of(entity) or "unknown"
    shape = entity.geo_shape
    polygons = polygon_coordinates(entity) if shape is not None and shape.polygon is not None else []
    point = lat_lon_of(entity)

    if polygons:
        geometry: dict[str, Any] = {"type": "Polygon", "coordinates": polygons}
        kind = "Polygon"
    elif point is not None:
        geometry = {"type": "Point", "coordinates": [float(point[1]), float(point[0])]}
        kind = "Point"
    else:
        return None

    fields = description_fields(entity.description)
    provenance = entity.provenance
    properties: dict[str, Any] = {
        "entity_id": entity_id,
        "name": (entity.aliases.name if entity.aliases and entity.aliases.name else entity_id),
        "data_type": data_type,
        "layer": LAYERS.get(data_type, (UNKNOWN_LAYER, kind))[0],
        "geometry_kind": kind,
        "is_live": entity.is_live is not False,
        "disposition": entity.mil_view.disposition if entity.mil_view else None,
        "environment": entity.mil_view.environment if entity.mil_view else None,
        "platform_type": entity.ontology.platform_type if entity.ontology else None,
        "description": (entity.description or "")[:DESCRIPTION_CHARS],
        "created": _iso(entity.created_time),
        "expires": _iso(entity.expiry_time),
        "integration": provenance.integration_name if provenance else None,
        "source_update_time": _iso(provenance.source_update_time) if provenance else None,
        "rings": len(polygons),
    }
    if entity.tracked is not None:
        properties["sensor_hits"] = entity.tracked.sensor_hits
        properties["last_measurement_time"] = _iso(entity.tracked.last_measurement_time)
    if data_type == "firms-viirs-hotspot":
        properties["frp_mw"] = hotspot_frp(entity)
        properties["confidence"] = fields.get("confidence")
        properties["satellite"] = fields.get("satellite")
    if data_type == CANDIDATE_TYPE:
        properties["score"] = candidate_score(entity)
        properties["evidence"] = (entity.description or "").splitlines()[:6]
    for key in ("event", "irwin_id", "unique_id", "station", "city"):
        if key in fields:
            properties[key] = fields[key]
    return {"type": "Feature", "id": entity_id, "geometry": geometry, "properties": properties}


def picture_collection(
    entities: Sequence[Entity], *, aoi: AOI | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """GeoJSON FeatureCollection of every renderable entity plus a ``meta`` foreign member."""
    now = now or datetime.now(UTC)
    features: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for entity in sorted(entities, key=lambda e: (data_type_of(e) or "", e.entity_id or "")):
        feature = feature_for(entity, now=now)
        if feature is None:
            reason = "no_geometry"
            if entity.is_live is False:
                reason = "not_live"
            elif entity.expiry_time is not None and entity.expiry_time <= now:
                reason = "expired"
            skipped[reason] += 1
            continue
        features.append(feature)
        counts[feature["properties"]["data_type"]] += 1
        layers[feature["properties"]["layer"]] += 1
    meta: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "source": "entities.stream_entities(pre_existing_only=True)",
        "integration": INTEGRATION,
        "count": len(features),
        "counts": dict(sorted(counts.items())),
        "layers": dict(sorted(layers.items())),
        "skipped": dict(sorted(skipped.items())),
        "aoi": _aoi_json(aoi),
    }
    return {"type": "FeatureCollection", "features": features, "meta": meta}


def _aoi_json(aoi: AOI | None) -> dict[str, float] | None:
    if aoi is None:
        return None
    return {
        "min_lon": aoi.min_lon,
        "min_lat": aoi.min_lat,
        "max_lon": aoi.max_lon,
        "max_lat": aoi.max_lat,
    }


def task_view(task: Task) -> dict[str, Any]:
    """Compact, JSON-safe view of one task, with the status Lattice reports for it."""
    specification = task.specification
    objective = getattr(specification, "objective", None) if specification is not None else None
    objective_id = objective.get("entity_id") if isinstance(objective, dict) else None
    relations = task.relations
    assignee = getattr(relations, "assignee", None) if relations is not None else None
    assignee_system = getattr(assignee, "system", None) if assignee is not None else None
    status = task.status
    error = getattr(status, "task_error", None) if status is not None else None
    status_text = getattr(status, "status", None) if status is not None else None
    task_id = task.version.task_id if task.version is not None else None
    return {
        "task_id": task_id,
        "display_name": task.display_name,
        "type": getattr(specification, "type", None) if specification is not None else None,
        "status": status_text,
        "status_key": status_text.removeprefix("STATUS_").lower() if status_text else None,
        "status_version": task.version.status_version if task.version is not None else None,
        "objective_entity_id": objective_id,
        "assignee_entity_id": getattr(assignee_system, "entity_id", None)
        if assignee_system is not None
        else None,
        "created": _iso(task.create_time),
        "last_update": _iso(task.last_update_time),
        "error": getattr(error, "message", None) if error is not None else None,
        "brief_path": f"{BRIEF_PREFIX}{task_id}.md" if task_id else None,
    }


def tasks_payload(
    tasks: Iterable[Task], *, limit: int = TASK_LIMIT, now: datetime | None = None
) -> dict[str, Any]:
    """Recent tasks, newest first, with the status each one reports."""
    views = [task_view(task) for task in tasks]
    views = [view for view in views if view["task_id"]]
    oldest = datetime.min.replace(tzinfo=UTC)
    views.sort(key=lambda v: (v["created"] or oldest.isoformat(), v["task_id"]), reverse=True)
    statuses = Counter(view["status"] or "unknown" for view in views)
    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "count": len(views[:limit]),
        "total": len(views),
        "statuses": dict(sorted(statuses.items())),
        "tasks": views[:limit],
    }


# ---------------------------------------------------------------------------------------------
# Lattice I/O
# ---------------------------------------------------------------------------------------------


def snapshot_entities(client: Any) -> list[Entity]:
    """Collapse ``stream_entities(pre_existing_only=True)`` into one entity per id (last event wins)."""
    picture: dict[str, Entity] = {}
    for event in client.entities.stream_entities(pre_existing_only=True):
        if getattr(event, "event", None) != "entity":
            continue
        entity = getattr(event, "entity", None)
        if entity is None or not entity.entity_id:
            continue
        picture[entity.entity_id] = entity
    return list(picture.values())


def all_tasks(client: Any) -> list[Task]:
    """Every task the environment returns, paging up to ``MAX_TASK_PAGES``."""
    tasks: list[Task] = []
    token: str | None = None
    for _ in range(MAX_TASK_PAGES):
        page = client.tasks.query_tasks(page_token=token) if token else client.tasks.query_tasks()
        tasks.extend(page.tasks or [])
        token = page.next_page_token
        if not token:
            break
    return tasks


def brief_path(task_id: str) -> str:
    return f"{BRIEF_PREFIX}{task_id}.md"


def read_brief(client: Any, task_id: str) -> str | None:
    """Brief markdown for ``task_id``, or ``None`` when no such Object exists."""
    if not TASK_ID_RE.match(task_id):
        return None
    try:
        body = b"".join(client.objects.get_object(brief_path(task_id)))
    except NotFoundError:
        return None
    return body.decode("utf-8", "replace")


class PictureService:
    """API payload builders over one Lattice client, with a short picture cache."""

    def __init__(
        self,
        client: Any,
        *,
        aoi: AOI | None = None,
        picture_ttl_s: float = PICTURE_TTL_S,
        clock: Any = time.monotonic,
    ) -> None:
        self.client = client
        self.aoi = aoi if aoi is not None else aoi_from_env()
        self.picture_ttl_s = picture_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._picture: list[Entity] | None = None
        self._picture_at = 0.0

    def picture(self, *, refresh: bool = False) -> list[Entity]:
        with self._lock:
            now = self._clock()
            if refresh or self._picture is None or now - self._picture_at > self.picture_ttl_s:
                self._picture = snapshot_entities(self.client)
                self._picture_at = now
            return list(self._picture)

    def picture_payload(self) -> dict[str, Any]:
        return picture_collection(self.picture(), aoi=self.aoi)

    def tasks_payload(self, *, limit: int = TASK_LIMIT) -> dict[str, Any]:
        return tasks_payload(all_tasks(self.client), limit=limit)


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------


def static_file(url_path: str) -> Path | None:
    """Map a URL path to a file inside ``static/``; ``None`` for missing or escaping paths."""
    relative = unquote(url_path).lstrip("/") or "index.html"
    root = STATIC_DIR.resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


class _Handler(BaseHTTPRequestHandler):
    server_version = "lw-map/0.1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> PictureService:
        return self.server.service  # type: ignore[attr-defined]

    # -- dispatch ------------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("/index.html")
            elif path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "integration": INTEGRATION,
                        "aoi": _aoi_json(self.service.aoi),
                        "bind": getattr(self.server, "bind_host", None),
                    }
                )
            elif path == "/api/picture":
                self._send_json(self.service.picture_payload())
            elif path == "/api/tasks":
                self._send_json(self.service.tasks_payload())
            elif path.startswith("/api/brief/"):
                self._serve_brief(path[len("/api/brief/") :])
            elif path.startswith("/api/"):
                self._send_json({"error": f"unknown endpoint {path}"}, status=404)
            else:
                self._serve_static(path)
        except NotFoundError as exc:  # a race: object deleted between requests
            self._send_json({"error": str(exc)}, status=404)
        except ApiError as exc:
            log.warning("lattice error on %s: %s", path, exc)
            self._send_json({"error": f"Lattice API error {exc.status_code}: {exc.body}"}, status=502)
        except (BrokenPipeError, ConnectionResetError):  # the browser went away
            log.debug("client disconnected on %s", path)
        except Exception as exc:  # never let one request kill the thread silently
            log.exception("unhandled error on %s", path)
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    # -- endpoints -----------------------------------------------------------------------------

    def _serve_brief(self, raw_task_id: str) -> None:
        task_id = unquote(raw_task_id)
        if not TASK_ID_RE.match(task_id):
            self._send_json({"error": f"invalid task id {task_id!r}"}, status=400)
            return
        markdown = read_brief(self.service.client, task_id)
        if markdown is None:
            self._send_json(
                {"error": f"no brief object for task {task_id}", "path": brief_path(task_id)}, status=404
            )
            return
        self._send(
            200,
            markdown.encode("utf-8"),
            "text/markdown; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _serve_static(self, url_path: str) -> None:
        path = static_file(url_path)
        if path is None:
            self._send_json({"error": f"{url_path} not found"}, status=404)
            return
        cache = "public, max-age=86400" if "vendor" in path.parts else "no-store"
        self._send(
            200,
            path.read_bytes(),
            CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            {"Cache-Control": cache},
        )

    # -- responses -----------------------------------------------------------------------------

    def _send(
        self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than stderr by default
        log.debug("%s %s", self.address_string(), fmt % args)


class PictureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: PictureService
    bind_host: str = DEFAULT_HOST

    def server_bind(self) -> None:
        """Bind without ``HTTPServer.server_bind``'s ``socket.getfqdn`` reverse lookup.

        That lookup blocks for seconds (or longer) on a host with no working resolver, which would
        stall startup on a wildcard bind before the URL line is printed. Nothing here needs the FQDN.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _PictureHTTPServerV6(PictureHTTPServer):
    address_family = socket.AF_INET6


def create_server(
    client: Any,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    explicit_host: bool = False,
    aoi: AOI | None = None,
    picture_ttl_s: float = PICTURE_TTL_S,
) -> PictureHTTPServer:
    """Bind the picture server. ``port=0`` picks a free port (``server.server_port``)."""
    bind_host, warning = resolve_host(host, explicit=explicit_host)
    if warning:
        log.warning("%s", warning)
    server_cls = _PictureHTTPServerV6 if ":" in bind_host else PictureHTTPServer
    server = server_cls((bind_host, port), _Handler)
    server.service = PictureService(client, aoi=aoi, picture_ttl_s=picture_ttl_s)
    server.bind_host = bind_host
    return server


def serve(
    client: Any,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    explicit_host: bool = False,
    aoi: AOI | None = None,
    picture_ttl_s: float = PICTURE_TTL_S,
) -> None:
    """Run until interrupted."""
    server = create_server(
        client, host=host, port=port, explicit_host=explicit_host, aoi=aoi, picture_ttl_s=picture_ttl_s
    )
    bound = server.bind_host if ":" not in server.bind_host else f"[{server.bind_host}]"
    print(f"lw-map: read-only picture on http://{bound}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
