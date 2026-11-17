"""Picture: one immutable snapshot of the wildfire entities Ember Fusion reasons over.

Everything here is pure. `picture_from_entities` turns SDK `Entity` objects (from the
Lattice entity stream) into typed records grouped by `provenance.data_type`, drops dead or
expired entities, and clusters the hotspots once so every perceiver sees the same clusters.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from anduril import Entity

from lattice_wildfire.core.entities import data_type_of, lat_lon_of
from lattice_wildfire.core.geo import cluster_points, geohash, point_in_polygon

# Data types read from Lattice (docs/CONTRACT.md shared vocabulary).
HOTSPOT = "firms-viirs-hotspot"
INCIDENT_TYPES = ("wfigs-incident", "calfire-incident")
PERIMETER = "wfigs-perimeter"
RED_FLAG = "nws-red-flag"
STATION = "lacofd-station"
CANDIDATE = "ember-candidate"

# Single-linkage clustering radius between hotspot detections (lane brief). VIIRS I-band
# pixels are ~375 m at nadir and grow to ~800 m at the scan edge, so 1.5 km joins adjacent and
# next-but-one pixels of one fire across passes without merging separate fires a few km apart.
CLUSTER_RADIUS_M = 1500.0
# geohash precision for candidate ids: 7 chars is a ~153 m x 153 m cell.
CANDIDATE_GEOHASH_PRECISION = 7

# FIRMS publishes track_quality_wrapper low=1, nominal=2, high=3 (Fireline Feeds contract).
_QUALITY_TO_CONFIDENCE = {1: "low", 2: "nominal", 3: "high"}
_CONFIDENCE_WORDS = {
    "l": "low",
    "low": "low",
    "n": "nominal",
    "nominal": "nominal",
    "h": "high",
    "high": "high",
}
_KV_LINE = re.compile(r"^\s*([a-z0-9_]+)\s*:\s*(.*?)\s*$")

LatLon = tuple[float, float]
Ring = tuple[LatLon, ...]


@dataclass(frozen=True)
class Hotspot:
    entity_id: str
    lat: float
    lon: float
    acquired: datetime
    satellite: str | None
    frp_mw: float | None
    confidence: str | None  # low | nominal | high


@dataclass(frozen=True)
class Incident:
    entity_id: str
    name: str
    lat: float
    lon: float
    data_type: str


@dataclass(frozen=True)
class Area:
    """A polygon entity: first ring exterior, later rings holes (Lattice GeoPolygon, RFC 7946)."""

    entity_id: str
    name: str
    rings: tuple[Ring, ...]
    data_type: str
    expires: datetime | None = None

    def contains(self, lat: float, lon: float) -> bool:
        if not self.rings or not point_in_polygon(lat, lon, list(self.rings[0])):
            return False
        return not any(point_in_polygon(lat, lon, list(hole)) for hole in self.rings[1:])


@dataclass(frozen=True)
class Station:
    entity_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Cluster:
    """Hotspots joined by single linkage. `cluster_id` doubles as the candidate entity id."""

    cluster_id: str
    members: tuple[Hotspot, ...]
    centroid: LatLon
    anchor: Hotspot  # earliest detection; fixes the id while later passes join the cluster

    @property
    def first_seen(self) -> datetime:
        return min(h.acquired for h in self.members)

    @property
    def last_seen(self) -> datetime:
        return max(h.acquired for h in self.members)

    @property
    def points(self) -> tuple[LatLon, ...]:
        return tuple((h.lat, h.lon) for h in self.members)


@dataclass(frozen=True)
class Picture:
    taken_at: datetime
    hotspots: tuple[Hotspot, ...] = ()
    incidents: tuple[Incident, ...] = ()
    perimeters: tuple[Area, ...] = ()
    red_flags: tuple[Area, ...] = ()
    stations: tuple[Station, ...] = ()
    clusters: tuple[Cluster, ...] = ()
    # Why entities were left out, e.g. {"expired": 3, "hotspot-without-acquired-time": 1}.
    skipped: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "hotspots": len(self.hotspots),
            "clusters": len(self.clusters),
            "incidents": len(self.incidents),
            "perimeters": len(self.perimeters),
            "red_flags": len(self.red_flags),
            "stations": len(self.stations),
        }


def candidate_id(anchor: Hotspot) -> str:
    """`lw-cand-{YYYYMMDD}-{geohash7}` from the cluster's earliest detection (UTC date)."""
    day = anchor.acquired.astimezone(UTC).strftime("%Y%m%d")
    return f"lw-cand-{day}-{geohash(anchor.lat, anchor.lon, CANDIDATE_GEOHASH_PRECISION)}"


def cluster_hotspots(hotspots: Iterable[Hotspot], radius_m: float = CLUSTER_RADIUS_M) -> tuple[Cluster, ...]:
    """Deterministic clustering: input order never changes membership, ids or output order."""
    ordered = sorted(hotspots, key=lambda h: h.entity_id)
    if not ordered:
        return ()
    groups = cluster_points([(h.lat, h.lon) for h in ordered], radius_m)
    clusters = []
    for group in groups:
        members = tuple(sorted((ordered[i] for i in group), key=lambda h: (h.acquired, h.entity_id)))
        anchor = members[0]
        centroid = (
            sum(h.lat for h in members) / len(members),
            sum(h.lon for h in members) / len(members),
        )
        clusters.append(Cluster(candidate_id(anchor), members, centroid, anchor))
    return tuple(sorted(clusters, key=lambda c: c.cluster_id))


def picture_from_entities(entities: Iterable[Entity], now: datetime | None = None) -> Picture:
    now = now or datetime.now(UTC)
    skipped: Counter[str] = Counter()
    hotspots: dict[str, Hotspot] = {}
    incidents: dict[str, Incident] = {}
    perimeters: dict[str, Area] = {}
    red_flags: dict[str, Area] = {}
    stations: dict[str, Station] = {}

    for entity in entities:
        data_type = data_type_of(entity)
        if not entity.entity_id or data_type is None:
            skipped["no-id-or-data-type"] += 1
            continue
        if data_type == CANDIDATE:
            continue  # our own output is never an input
        if entity.is_live is False or (entity.expiry_time is not None and entity.expiry_time <= now):
            skipped["expired-or-not-live"] += 1
            continue
        if data_type == HOTSPOT:
            hotspot = hotspot_from_entity(entity)
            if hotspot is None:
                skipped["hotspot-without-location-or-acquired-time"] += 1
            else:
                hotspots[hotspot.entity_id] = hotspot
        elif data_type in INCIDENT_TYPES:
            point = lat_lon_of(entity)
            if point is None:
                skipped["incident-without-location"] += 1
            else:
                incidents[entity.entity_id] = Incident(
                    entity.entity_id, _name(entity), point[0], point[1], data_type
                )
        elif data_type in (PERIMETER, RED_FLAG):
            rings = rings_of(entity)
            if not rings:
                skipped[f"{data_type}-without-polygon"] += 1
                continue
            area = Area(entity.entity_id, _name(entity), rings, data_type, entity.expiry_time)
            (perimeters if data_type == PERIMETER else red_flags)[area.entity_id] = area
        elif data_type == STATION:
            point = lat_lon_of(entity)
            if point is not None:
                stations[entity.entity_id] = Station(entity.entity_id, _name(entity), point[0], point[1])

    def by_id(items: dict) -> tuple:
        return tuple(items[k] for k in sorted(items))

    hotspot_tuple = by_id(hotspots)
    return Picture(
        taken_at=now,
        hotspots=hotspot_tuple,
        incidents=by_id(incidents),
        perimeters=by_id(perimeters),
        red_flags=by_id(red_flags),
        stations=by_id(stations),
        clusters=cluster_hotspots(hotspot_tuple),
        skipped=dict(sorted(skipped.items())),
    )


def hotspot_from_entity(entity: Entity) -> Hotspot | None:
    """Read a Fireline Feeds hotspot: `key: value` description lines, tracked, ontology."""
    point = lat_lon_of(entity)
    if point is None:
        return None
    kv = description_fields(entity.description)
    tracked = entity.tracked

    acquired = _parse_time(kv.get("acquired"))
    if acquired is None and tracked is not None and tracked.last_measurement_time is not None:
        acquired = tracked.last_measurement_time
    if acquired is None:
        return None
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=UTC)

    satellite = kv.get("satellite")
    specific = entity.ontology.specific_type if entity.ontology else None
    if not satellite and specific and specific.startswith("VIIRS "):
        satellite = specific.removeprefix("VIIRS ").strip()

    confidence = _CONFIDENCE_WORDS.get((kv.get("confidence") or "").lower())
    if confidence is None and tracked is not None:
        confidence = _QUALITY_TO_CONFIDENCE.get(tracked.track_quality_wrapper or 0)

    return Hotspot(
        entity_id=entity.entity_id,
        lat=point[0],
        lon=point[1],
        acquired=acquired,
        satellite=satellite or None,
        frp_mw=_parse_float(kv.get("frp_mw")),
        confidence=confidence,
    )


def rings_of(entity: Entity) -> tuple[Ring, ...]:
    shape = entity.geo_shape
    polygon = shape.polygon if shape is not None else None
    if polygon is None or not polygon.rings:
        return ()
    rings = []
    for ring in polygon.rings:
        points = tuple(
            (p.position.latitude_degrees, p.position.longitude_degrees)
            for p in ring.positions or []
            if p.position is not None
            and p.position.latitude_degrees is not None
            and p.position.longitude_degrees is not None
        )
        if len(points) >= 3:
            rings.append(points)
    return tuple(rings)


def description_fields(description: str | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (description or "").splitlines():
        match = _KV_LINE.match(line)
        if match:
            fields.setdefault(match.group(1), match.group(2))
    return fields


def _name(entity: Entity) -> str:
    return (entity.aliases.name if entity.aliases and entity.aliases.name else None) or entity.entity_id


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None
