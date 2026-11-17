"""Shared Lattice Talk tools: one implementation behind the MCP server and the chat CLI.

Pure functions work on a list of SDK `Entity` objects (a picture snapshot) or SDK `Task` objects.
`TalkTools` is the I/O edge: it snapshots the picture with `stream_entities(pre_existing_only=True)`,
reads entities, tasks and brief Objects, and creates Investigate tasks only when `confirm=True`.
Every tool returns a JSON-serialisable dict; failures come back as `{"error": ...}`, never raised.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from anduril import (
    Entity,
    GoogleProtobufAny,
    NotFoundError,
    Principal,
    Relations,
    System,
    Task,
)
from anduril.core.api_error import ApiError

from lattice_wildfire.core.config import AOI, aoi_from_env
from lattice_wildfire.core.entities import data_type_of
from lattice_wildfire.core.entities import lat_lon_of as entity_point
from lattice_wildfire.core.geo import (
    EARTH_RADIUS_M,
    bbox_contains,
    bearing_deg,
    centroid,
    haversine_m,
    point_in_polygon,
)

INTEGRATION = "lattice-wildfire-talk"
INVESTIGATE_URL = "type.googleapis.com/anduril.tasks.v2.Investigate"
BRIEF_PREFIX = "lattice-wildfire/briefs/"
STATION_DATA_TYPE = "lacofd-station"
TERMINAL_STATUSES = frozenset(
    {"STATUS_DONE_OK", "STATUS_DONE_NOT_OK", "STATUS_REPLACED", "STATUS_VERSION_REJECTED"}
)
DESCRIPTION_CHARS = 400
MAX_TASK_PAGES = 50

# ---------------------------------------------------------------------------------------------
# Pure helpers over SDK objects
# ---------------------------------------------------------------------------------------------


def snapshot_from_events(events: Iterable[Any]) -> list[Entity]:
    """Collapse a `stream_entities` event iterator into one entity per id (last event wins).

    Heartbeats are ignored; DELETED events remove the entity from the picture.
    """
    picture: dict[str, Entity] = {}
    for event in events:
        if getattr(event, "event", None) != "entity":
            continue
        entity = event.entity
        if entity is None or not entity.entity_id:
            continue
        if event.event_type == "EVENT_TYPE_DELETED":
            picture.pop(entity.entity_id, None)
        else:
            picture[entity.entity_id] = entity
    return list(picture.values())


def parse_bbox(bbox: str | Sequence[float] | None) -> AOI | None:
    """Parse `"min_lon,min_lat,max_lon,max_lat"` (the LW_AOI_BBOX / GeoJSON order) or a 4-number
    sequence in that order. None or "" stays None. Raises ValueError on a malformed box."""
    if bbox is None or bbox == "":
        return None
    parts = [float(p) for p in bbox.split(",")] if isinstance(bbox, str) else [float(p) for p in bbox]
    if len(parts) != 4:
        raise ValueError(f"bbox needs 4 numbers min_lon,min_lat,max_lon,max_lat, got {len(parts)}")
    min_lon, min_lat, max_lon, max_lat = parts
    return AOI(min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon)


def entity_name(entity: Entity) -> str | None:
    return entity.aliases.name if entity.aliases and entity.aliases.name else None


def entity_ring(entity: Entity) -> list[tuple[float, float]] | None:
    """Outer polygon ring as (lat, lon) pairs, or None when the entity has no polygon shape."""
    shape = entity.geo_shape
    if not shape or not shape.polygon or not shape.polygon.rings:
        return None
    ring: list[tuple[float, float]] = []
    for vertex in shape.polygon.rings[0].positions or []:
        pos = vertex.position
        if pos and pos.latitude_degrees is not None and pos.longitude_degrees is not None:
            ring.append((pos.latitude_degrees, pos.longitude_degrees))
    return ring if len(ring) >= 3 else None


def _segment_distance_m(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from (lat, lon) to segment a-b on a local equirectangular plane centred on the point."""
    k_lat = math.radians(1.0) * EARTH_RADIUS_M
    k_lon = k_lat * math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * k_lon, (a[0] - lat) * k_lat
    bx, by = (b[1] - lon) * k_lon, (b[0] - lat) * k_lat
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_to_entity_m(lat: float, lon: float, entity: Entity) -> float | None:
    """Metres from a point to an entity. Polygons: 0 inside, else distance to the nearest edge."""
    ring = entity_ring(entity)
    if ring:
        if point_in_polygon(lat, lon, ring):
            return 0.0
        closed = ring if ring[0] == ring[-1] else [*ring, ring[0]]
        return min(_segment_distance_m(lat, lon, closed[i], closed[i + 1]) for i in range(len(closed) - 1))
    point = entity_point(entity)
    if point is None:
        return None
    return haversine_m(lat, lon, point[0], point[1])


def investigate_capable(entity: Entity) -> bool:
    catalog = entity.task_catalog
    return bool(
        catalog and any(d.task_specification_url == INVESTIGATE_URL for d in catalog.task_definitions or [])
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def summarize(entity: Entity, origin: tuple[float, float] | None = None) -> dict[str, Any]:
    """Compact, citation-friendly view of one entity."""
    point = entity_point(entity)
    out: dict[str, Any] = {
        "entity_id": entity.entity_id,
        "name": entity_name(entity),
        "data_type": data_type_of(entity),
        "platform_type": entity.ontology.platform_type if entity.ontology else None,
        "template": entity.ontology.template if entity.ontology else None,
        "disposition": entity.mil_view.disposition if entity.mil_view else None,
        "lat": round(point[0], 5) if point else None,
        "lon": round(point[1], 5) if point else None,
        "is_polygon": entity_ring(entity) is not None,
        "source": entity.provenance.integration_name if entity.provenance else None,
        "source_update_time": _iso(entity.provenance.source_update_time) if entity.provenance else None,
        "expiry_time": _iso(entity.expiry_time),
        "investigate_capable": investigate_capable(entity),
    }
    if entity.health:
        out["health"] = entity.health.health_status
        out["connection"] = entity.health.connection_status
    if entity.description:
        text = entity.description
        out["description"] = text if len(text) <= DESCRIPTION_CHARS else text[:DESCRIPTION_CHARS] + "..."
    if origin is not None:
        dist = distance_to_entity_m(origin[0], origin[1], entity)
        out["distance_km"] = None if dist is None else round(dist / 1000.0, 3)
        if point is not None and dist:
            out["bearing_deg"] = round(bearing_deg(origin[0], origin[1], point[0], point[1]), 1)
    return out


def _in_aoi(entity: Entity, aoi: AOI) -> bool:
    ring = entity_ring(entity)
    if ring:
        return any(bbox_contains(aoi, lat, lon) for lat, lon in ring) or bbox_contains(aoi, *centroid(ring))
    point = entity_point(entity)
    return point is not None and bbox_contains(aoi, point[0], point[1])


def picture_view(
    entities: Sequence[Entity],
    aoi: AOI | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Entities in the AOI (and of the given data_types), with counts per data_type."""
    wanted = {k.strip() for k in kinds or [] if k.strip()}
    selected = [
        e for e in entities if (not wanted or data_type_of(e) in wanted) and (aoi is None or _in_aoi(e, aoi))
    ]
    selected.sort(key=lambda e: (data_type_of(e) or "", e.entity_id or ""))
    counts = Counter(data_type_of(e) or "unknown" for e in selected)
    return {
        "bbox": None if aoi is None else list(aoi.envelope()),
        "kinds": sorted(wanted) or None,
        "total": len(selected),
        "counts": dict(sorted(counts.items())),
        "truncated": len(selected) > limit,
        "entities": [summarize(e) for e in selected[:limit]],
    }


def near_entities(
    entities: Sequence[Entity],
    lat: float,
    lon: float,
    radius_km: float,
    kinds: Sequence[str] | None = None,
    exclude_ids: Iterable[str] = (),
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Entities within `radius_km` (inclusive) of a point, nearest first."""
    if radius_km < 0:
        raise ValueError("radius_km must be >= 0")
    wanted = {k for k in kinds or [] if k}
    excluded = set(exclude_ids)
    radius_m = radius_km * 1000.0
    hits: list[tuple[float, Entity]] = []
    for e in entities:
        if e.entity_id in excluded or (wanted and data_type_of(e) not in wanted):
            continue
        dist = distance_to_entity_m(lat, lon, e)
        if dist is not None and dist <= radius_m:
            hits.append((dist, e))
    hits.sort(key=lambda h: (h[0], h[1].entity_id or ""))
    return [summarize(e, origin=(lat, lon)) for _, e in hits[:limit]]


_STATION_NUMBER_RE = re.compile(r"(\d+[A-Za-z]?)")


def _station_key(value: str) -> str | None:
    """`"Station 011"`, `"11"`, `11` -> `"11"`; keeps a letter suffix (`"63a"` -> `"63A"`)."""
    match = _STATION_NUMBER_RE.search(value)
    if not match:
        return None
    number = match.group(1).upper()
    return number.lstrip("0") or "0"


def find_station(entities: Sequence[Entity], station_number: str | int) -> Entity | None:
    """Fire station entity by number. Matches `lw-station-{n}` ignoring leading zeros."""
    wanted = _station_key(str(station_number))
    if wanted is None:
        return None
    for e in entities:
        if data_type_of(e) != STATION_DATA_TYPE or not e.entity_id:
            continue
        if (
            e.entity_id.startswith("lw-station-")
            and _station_key(e.entity_id[len("lw-station-") :]) == wanted
        ):
            return e
    return None


def choose_assignee(entities: Sequence[Entity], target: Entity) -> Entity | None:
    """Nearest live Investigate-capable asset to the target (ties and unknown positions by id)."""
    target_point = entity_point(target)
    candidates = [
        e
        for e in entities
        if investigate_capable(e)
        and e.entity_id != target.entity_id
        and e.is_live is not False
        and not (e.health and e.health.connection_status == "CONNECTION_STATUS_OFFLINE")
    ]

    def rank(e: Entity) -> tuple[float, str]:
        point = entity_point(e)
        if target_point is None or point is None:
            return (math.inf, e.entity_id or "")
        return (haversine_m(target_point[0], target_point[1], point[0], point[1]), e.entity_id or "")

    return min(candidates, key=rank, default=None)


def task_spec(task: Task) -> dict[str, Any]:
    return task.specification.dict() if task.specification else {}


def objective_entity_id(task: Task) -> str | None:
    objective = task_spec(task).get("objective")
    return objective.get("entity_id") if isinstance(objective, dict) else None


def task_id_of(task: Task) -> str | None:
    return task.version.task_id if task.version else None


def task_status_of(task: Task) -> str | None:
    return task.status.status if task.status else None


def task_summary(task: Task) -> dict[str, Any]:
    assignee = task.relations.assignee if task.relations else None
    error = task.status.task_error if task.status else None
    return {
        "task_id": task_id_of(task),
        "display_name": task.display_name,
        "type": task_spec(task).get("@type"),
        "objective_entity_id": objective_entity_id(task),
        "assignee_entity_id": assignee.system.entity_id if assignee and assignee.system else None,
        "status": task_status_of(task),
        "status_version": task.version.status_version if task.version else None,
        "created": _iso(task.create_time),
        "last_update": _iso(task.last_update_time),
        "error": error.message if error and getattr(error, "message", None) else None,
    }


def investigations_for(tasks: Iterable[Task], entity_id: str) -> list[Task]:
    """Investigate tasks whose objective is `entity_id`, newest first."""
    hits = [
        t
        for t in tasks
        if task_spec(t).get("@type") == INVESTIGATE_URL and objective_entity_id(t) == entity_id
    ]
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(hits, key=lambda t: t.create_time or oldest, reverse=True)


def open_investigations(tasks: Iterable[Task], entity_id: str) -> list[Task]:
    return [t for t in investigations_for(tasks, entity_id) if task_status_of(t) not in TERMINAL_STATUSES]


def investigation_request(target: Entity, assignee: Entity) -> dict[str, Any]:
    """`tasks.create_task` keyword arguments for an Investigate task on `target`."""
    name = entity_name(target) or target.entity_id
    return {
        "display_name": f"Investigate {name}",
        "description": (
            f"Investigate {target.entity_id} ({data_type_of(target) or 'entity'}), "
            f"requested through {INTEGRATION}."
        ),
        "specification": GoogleProtobufAny(type=INVESTIGATE_URL, objective={"entity_id": target.entity_id}),
        "author": Principal(system=System(service_name=INTEGRATION)),
        "relations": Relations(assignee=Principal(system=System(entity_id=assignee.entity_id))),
        "is_executed_elsewhere": False,
    }


# ---------------------------------------------------------------------------------------------
# OpenAI tool specs (the MCP server registers the same names; tests check they match)
# ---------------------------------------------------------------------------------------------


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _fn(
        "wildfire_picture",
        "Current Lattice wildfire picture: counts per data_type and entity summaries inside a bbox "
        "(default: the configured AOI, Los Angeles County). Data types include firms-viirs-hotspot, "
        "wfigs-incident, calfire-incident, wfigs-perimeter, nws-red-flag, lacofd-station, "
        "ember-candidate, kodros-analyst.",
        {
            "bbox": {
                "type": "string",
                "description": "min_lon,min_lat,max_lon,max_lat (west,south,east,north); omit for the AOI",
            },
            "kinds": {"type": "array", "items": {"type": "string"}, "description": "data_type filter"},
            "limit": {"type": "integer", "description": "max entities returned (default 50)"},
        },
        [],
    ),
    _fn(
        "get_entity",
        "Full summary of one Lattice entity by entity_id.",
        {"entity_id": {"type": "string"}},
        ["entity_id"],
    ),
    _fn(
        "near",
        "Entities within radius_km of a lat/lon, nearest first, with distance_km and bearing_deg. "
        "Polygons count when the point is inside or within radius of an edge.",
        {
            "lat": {"type": "number"},
            "lon": {"type": "number"},
            "radius_km": {"type": "number"},
            "kinds": {"type": "array", "items": {"type": "string"}, "description": "data_type filter"},
        },
        ["lat", "lon", "radius_km"],
    ),
    _fn(
        "near_station",
        "Entities within radius_km (default 10) of an LA County Fire station by station number.",
        {
            "station_number": {"type": "string"},
            "radius_km": {"type": "number"},
            "kinds": {"type": "array", "items": {"type": "string"}},
        },
        ["station_number"],
    ),
    _fn(
        "latest_brief",
        "Latest analyst brief (markdown + facts JSON) for a task_id, or for the newest Investigate "
        "task whose objective is entity_id. Give one of the two.",
        {"entity_id": {"type": "string"}, "task_id": {"type": "string"}},
        [],
    ),
    _fn(
        "task_status",
        "Status, assignee and objective of one Lattice task.",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _fn(
        "request_investigation",
        "Plan an Investigate task on entity_id. Dry run unless confirm is true; with confirm it "
        "creates the task in Lattice. Refuses when an open Investigate task already targets the entity.",
        {
            "entity_id": {"type": "string"},
            "confirm": {"type": "boolean", "description": "true creates the task; default false"},
            "assignee_entity_id": {"type": "string", "description": "optional Investigate-capable asset"},
        },
        ["entity_id"],
    ),
]

TOOL_NAMES: tuple[str, ...] = tuple(spec["function"]["name"] for spec in TOOL_SPECS)


# ---------------------------------------------------------------------------------------------
# I/O edge
# ---------------------------------------------------------------------------------------------


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, **extra}


def _api_error(exc: ApiError) -> str:
    return f"Lattice API error {exc.status_code}: {exc.body}"


class TalkTools:
    """The seven Lattice Talk tools over one SDK client. Methods return JSON-serialisable dicts."""

    def __init__(
        self,
        client: Any,
        aoi: AOI | None = None,
        picture_ttl_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.aoi = aoi if aoi is not None else aoi_from_env()
        self.picture_ttl_s = picture_ttl_s
        self._clock = clock
        self._picture: list[Entity] | None = None
        self._picture_at = 0.0

    # -- reads -------------------------------------------------------------------------------

    def picture(self, refresh: bool = False) -> list[Entity]:
        """Snapshot of every pre-existing entity, cached for `picture_ttl_s` seconds."""
        now = self._clock()
        if refresh or self._picture is None or now - self._picture_at > self.picture_ttl_s:
            self._picture = snapshot_from_events(self.client.entities.stream_entities(pre_existing_only=True))
            self._picture_at = now
        return self._picture

    def _all_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        token: str | None = None
        for _ in range(MAX_TASK_PAGES):
            page = (
                self.client.tasks.query_tasks(page_token=token) if token else self.client.tasks.query_tasks()
            )
            tasks.extend(page.tasks or [])
            token = page.next_page_token
            if not token:
                break
        return tasks

    def _read_object(self, path: str) -> bytes:
        return b"".join(self.client.objects.get_object(path))

    # -- tools -------------------------------------------------------------------------------

    def wildfire_picture(
        self, bbox: str | Sequence[float] | None = None, kinds: Sequence[str] | None = None, limit: int = 50
    ) -> dict[str, Any]:
        try:
            aoi = parse_bbox(bbox) or self.aoi
        except ValueError as exc:
            return _error(str(exc))
        return picture_view(self.picture(), aoi=aoi, kinds=kinds, limit=max(1, int(limit)))

    def get_entity(self, entity_id: str) -> dict[str, Any]:
        try:
            entity = self.client.entities.get_entity(entity_id)
        except NotFoundError:
            return _error(f"entity {entity_id} not found")
        except ApiError as exc:
            return _error(_api_error(exc))
        return summarize(entity)

    def near(
        self, lat: float, lon: float, radius_km: float, kinds: Sequence[str] | None = None
    ) -> dict[str, Any]:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return _error(f"invalid point {lat},{lon}")
        if radius_km < 0:
            return _error("radius_km must be >= 0")
        hits = near_entities(self.picture(), lat, lon, radius_km, kinds=kinds)
        return {"center": [lat, lon], "radius_km": radius_km, "count": len(hits), "entities": hits}

    def near_station(
        self, station_number: str | int, radius_km: float = 10.0, kinds: Sequence[str] | None = None
    ) -> dict[str, Any]:
        entities = self.picture()
        station = find_station(entities, station_number)
        if station is None:
            return _error(f"station {station_number} is not in the Lattice picture")
        point = entity_point(station)
        if point is None:
            return _error(f"station {station.entity_id} has no location")
        hits = near_entities(
            entities, point[0], point[1], radius_km, kinds=kinds, exclude_ids={station.entity_id}
        )
        return {
            "station": summarize(station),
            "radius_km": radius_km,
            "count": len(hits),
            "entities": hits,
        }

    def latest_brief(self, entity_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        if not entity_id and not task_id:
            return _error("give entity_id or task_id")
        tasks_seen: list[dict[str, Any]] = []
        if task_id:
            candidate_ids = [task_id]
        else:
            try:
                investigations = investigations_for(self._all_tasks(), entity_id or "")
            except ApiError as exc:
                return _error(_api_error(exc))
            tasks_seen = [task_summary(t) for t in investigations]
            candidate_ids = [tid for t in investigations if (tid := task_id_of(t))]
        for tid in candidate_ids:
            base = f"{BRIEF_PREFIX}{tid}"
            try:
                markdown = self._read_object(f"{base}.md").decode("utf-8")
            except NotFoundError:
                continue
            except ApiError as exc:
                return _error(_api_error(exc))
            try:
                facts = json.loads(self._read_object(f"{base}.json"))
            except NotFoundError:
                facts = None
            except (ApiError, ValueError) as exc:
                facts = {"unreadable": str(exc)}
            return {"task_id": tid, "path": f"{base}.md", "markdown": markdown, "facts": facts}
        target = f"task {task_id}" if task_id else f"entity {entity_id}"
        return _error(f"no brief found for {target}", investigate_tasks=tasks_seen)

    def task_status(self, task_id: str) -> dict[str, Any]:
        try:
            return task_summary(self.client.tasks.get_task(task_id))
        except NotFoundError:
            return _error(f"task {task_id} not found")
        except ApiError as exc:
            return _error(_api_error(exc))

    # -- the one write -----------------------------------------------------------------------

    def request_investigation(
        self, entity_id: str, confirm: bool = False, assignee_entity_id: str | None = None
    ) -> dict[str, Any]:
        try:
            target = self.client.entities.get_entity(entity_id)
        except NotFoundError:
            return _error(f"entity {entity_id} not found")
        except ApiError as exc:
            return _error(_api_error(exc))

        try:
            already = open_investigations(self._all_tasks(), entity_id)
        except ApiError as exc:
            return _error(_api_error(exc))
        if already:
            return {
                "created": False,
                "reason": "an open Investigate task already targets this entity",
                "existing": [task_summary(t) for t in already],
            }

        if assignee_entity_id:
            try:
                assignee = self.client.entities.get_entity(assignee_entity_id)
            except NotFoundError:
                return _error(f"assignee {assignee_entity_id} not found")
            except ApiError as exc:
                return _error(_api_error(exc))
            if not investigate_capable(assignee):
                return _error(f"assignee {assignee_entity_id} does not advertise {INVESTIGATE_URL}")
        else:
            assignee = choose_assignee(self.picture(refresh=True), target)
            if assignee is None:
                return _error("no live Investigate-capable asset in the Lattice picture")

        request = investigation_request(target, assignee)
        plan = {
            "display_name": request["display_name"],
            "type": INVESTIGATE_URL,
            "objective_entity_id": target.entity_id,
            "assignee_entity_id": assignee.entity_id,
        }
        if not confirm:
            return {
                "created": False,
                "dry_run": True,
                "would_create": plan,
                "note": "Dry run. Nothing was sent to Lattice. Call again with confirm=true to create the task.",
            }
        try:
            created = self.client.tasks.create_task(**request)
            created_id = task_id_of(created)
            readback = self.client.tasks.get_task(created_id) if created_id else created
        except ApiError as exc:
            return _error(_api_error(exc), would_create=plan)
        return {"created": True, "task": task_summary(readback)}

    # -- dispatch ----------------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool by name with keyword arguments (used by the chat loop)."""
        if name not in TOOL_NAMES:
            return _error(f"unknown tool {name}; tools are {', '.join(TOOL_NAMES)}")
        method = getattr(self, name)
        try:
            bound = inspect.signature(method).bind(**(arguments or {}))
        except TypeError as exc:
            return _error(f"bad arguments for {name}: {exc}")
        try:
            return method(*bound.args, **bound.kwargs)
        except ValueError as exc:
            return _error(str(exc))
