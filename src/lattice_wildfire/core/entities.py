"""Entity component builders shared by every app. Enum literals come from the installed SDK."""

from __future__ import annotations

import typing
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from anduril import (
    Classification,
    ClassificationInformation,
    Entity,
    GeoDetails,
    GeoPolygon,
    GeoPolygonPosition,
    GeoShape,
    LinearRing,
    Location,
    MilStd2525C,
    MilView,
    Position,
    Provenance,
    Symbology,
)

from .geo import centroid

MAX_EXPIRY = timedelta(days=30)


def _literals(model: type, field: str) -> tuple[str, ...]:
    """String literals the SDK declares for an enum-typed field (its type is ``Literal[...] | Any``)."""
    found: list[str] = []
    for arg in typing.get_args(model.model_fields[field].annotation):
        if typing.get_origin(arg) is typing.Literal:
            found.extend(typing.get_args(arg))
    return tuple(found)


DISPOSITIONS = _literals(MilView, "disposition")
ENVIRONMENTS = _literals(MilView, "environment")
GEO_TYPES = _literals(GeoDetails, "type")

# MIL-STD-2525C position 2 (standard identity). Reference mapping from lattice-sdk-ai entity-fields.md:
# friendly->F, hostile->H, neutral->N, unknown/pending->U; assumed friend (A) and suspect (S) are the
# 2525C identities for the two remaining Lattice dispositions.
_AFFILIATION = {
    "DISPOSITION_FRIENDLY": "F",
    "DISPOSITION_ASSUMED_FRIENDLY": "A",
    "DISPOSITION_HOSTILE": "H",
    "DISPOSITION_SUSPICIOUS": "S",
    "DISPOSITION_NEUTRAL": "N",
    "DISPOSITION_UNKNOWN": "U",
    "DISPOSITION_PENDING": "U",
}
# MIL-STD-2525C position 3 (battle dimension): surface->S, air->A, land->G, subsurface->U per the
# reference; space->P and unknown->Z per 2525C.
_DIMENSION = {
    "ENVIRONMENT_AIR": "A",
    "ENVIRONMENT_SURFACE": "S",
    "ENVIRONMENT_LAND": "G",
    "ENVIRONMENT_SUB_SURFACE": "U",
    "ENVIRONMENT_SPACE": "P",
    "ENVIRONMENT_UNKNOWN": "Z",
}
assert set(_AFFILIATION) == set(DISPOSITIONS), "SDK dispositions changed; update _AFFILIATION"
assert set(_DIMENSION) == set(ENVIRONMENTS), "SDK environments changed; update _DIMENSION"


def now_utc() -> datetime:
    return datetime.now(UTC)


def expiry(seconds: int) -> datetime:
    """Expiry time ``seconds`` from now. Lattice requires it in the future and under 30 days out."""
    if seconds <= 0:
        raise ValueError("expiry seconds must be positive")
    delta = timedelta(seconds=seconds)
    if delta >= MAX_EXPIRY:
        raise ValueError("expiry must be less than 30 days out")
    return now_utc() + delta


def provenance(pkg: str, data_type: str, source_time: datetime | None = None) -> Provenance:
    """Provenance with integration name ``lattice-wildfire-{pkg}``. ``source_time`` defaults to now."""
    if not pkg or not data_type:
        raise ValueError("pkg and data_type must be non-empty")
    if source_time is not None and source_time.tzinfo is None:
        raise ValueError("source_time must be timezone-aware")
    return Provenance(
        integration_name=f"lattice-wildfire-{pkg}",
        data_type=data_type,
        source_update_time=source_time or now_utc(),
    )


def unclassified() -> Classification:
    return Classification(default=ClassificationInformation(level="CLASSIFICATION_LEVELS_UNCLASSIFIED"))


def _full(value: str, prefix: str, allowed: tuple[str, ...]) -> str:
    literal = value if value.startswith(prefix) else prefix + value.upper()
    if literal not in allowed:
        raise ValueError(f"unknown {prefix.rstrip('_').lower()} {value!r}; expected one of {allowed}")
    return literal


def sidc(disposition: str, environment: str, function: str = "------") -> str:
    """15-character MIL-STD-2525C SIDC: ``S`` + identity + dimension + ``P`` + 6-char function + ``-----``.

    ``disposition``/``environment`` accept the SDK literal (``DISPOSITION_FRIENDLY``) or its suffix
    (``friendly``).
    """
    affiliation = _AFFILIATION[_full(disposition, "DISPOSITION_", DISPOSITIONS)]
    dimension = _DIMENSION[_full(environment, "ENVIRONMENT_", ENVIRONMENTS)]
    if len(function) != 6 or not all(c.isalnum() or c == "-" for c in function):
        raise ValueError("function must be 6 characters of A-Z, 0-9 or '-'")
    code = f"S{affiliation}{dimension}P{function.upper()}-----"
    assert len(code) == 15
    return code


def symbology(disposition: str, environment: str, function: str = "------") -> Symbology:
    return Symbology(mil_std2525c=MilStd2525C(sidc=sidc(disposition, environment, function)))


def point_location(lat: float, lon: float, alt_hae_m: float | None = None) -> Location:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    return Location(
        position=Position(
            latitude_degrees=float(lat), longitude_degrees=float(lon), altitude_hae_meters=alt_hae_m
        )
    )


def polygon_geo(
    ring: Sequence[tuple[float, float]], geo_type: str = "GEO_TYPE_GENERAL"
) -> tuple[GeoDetails, GeoShape]:
    """``(GeoDetails, GeoShape)`` for a TEMPLATE_GEO polygon from a ``[(lat, lon), ...]`` ring.

    The ring is closed (first position repeated last) if it is not already.
    """
    if geo_type not in GEO_TYPES:
        raise ValueError(f"unknown geo type {geo_type!r}; expected one of {GEO_TYPES}")
    pts = [(float(lat), float(lon)) for lat, lon in ring]
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    if len(set(pts)) < 3:
        raise ValueError("a polygon ring needs at least 3 distinct positions")
    positions = [
        GeoPolygonPosition(position=Position(latitude_degrees=lat, longitude_degrees=lon)) for lat, lon in pts
    ]
    shape = GeoShape(polygon=GeoPolygon(rings=[LinearRing(positions=positions)]))
    return GeoDetails(type=geo_type), shape


def data_type_of(entity: Entity) -> str | None:
    prov = entity.provenance
    return prov.data_type if prov is not None else None


def _pos_latlon(pos: Position | None) -> tuple[float, float] | None:
    if pos is None or pos.latitude_degrees is None or pos.longitude_degrees is None:
        return None
    return (pos.latitude_degrees, pos.longitude_degrees)


def lat_lon_of(entity: Entity) -> tuple[float, float] | None:
    """Entity position: ``location.position`` first, else the geo shape (point, polygon/line centroid)."""
    if entity.location is not None:
        found = _pos_latlon(entity.location.position)
        if found is not None:
            return found
    shape = entity.geo_shape
    if shape is None:
        return None
    if shape.point is not None:
        return _pos_latlon(shape.point.position)
    if shape.polygon is not None and shape.polygon.rings:
        outer = shape.polygon.rings[0].positions or []
        pts = [p for p in (_pos_latlon(gp.position) for gp in outer) if p is not None]
        return centroid(pts) if pts else None
    if shape.line is not None and shape.line.positions:
        pts = [p for p in (_pos_latlon(pos) for pos in shape.line.positions) if p is not None]
        if pts:
            return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return None
