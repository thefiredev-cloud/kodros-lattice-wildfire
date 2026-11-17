"""Typed records parsed from the public fire feeds.

Coordinates are stored as ``(lat, lon)`` tuples. Polygon parts are lists of rings: the first ring is
the exterior, any further rings are holes, exactly as the source GeoJSON delivered them (orientation is
normalized later, when the Lattice payload is built).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

LatLon = tuple[float, float]
Ring = list[LatLon]
PolygonPart = list[Ring]

# FIRMS satellite codes in the VIIRS active-fire files.
SATELLITE_NAMES = {"N": "Suomi NPP", "N20": "NOAA-20", "N21": "NOAA-21"}


@dataclass(frozen=True)
class Hotspot:
    """One FIRMS VIIRS active-fire pixel."""

    satellite: str
    acq_date: str  # YYYY-MM-DD as delivered
    acq_time: str  # HHMM UTC, zero padded
    acquired: datetime
    lat: float
    lon: float
    bright_ti4_k: float
    bright_ti5_k: float
    frp_mw: float
    scan_km: float
    track_km: float
    confidence: str  # low | nominal | high (VIIRS); unknown values kept raw
    version: str
    daynight: str

    @property
    def source_id(self) -> str:
        return f"{self.satellite}-{self.acq_date}T{self.acq_time}-{self.lat:.4f}-{self.lon:.4f}"

    @property
    def entity_id(self) -> str:
        return f"lw-firms-{self.satellite}-{self.acq_date}{self.acq_time}-{self.lat:.4f}-{self.lon:.4f}"


@dataclass(frozen=True)
class WfigsIncident:
    """One WFIGS current incident location (IRWIN-backed)."""

    irwin_id: str | None  # normalized: no braces, upper case
    name: str
    lat: float | None
    lon: float | None
    unique_fire_id: str | None
    type_category: str | None  # WF, RX, CX
    size_acres: float | None
    discovery_acres: float | None
    percent_contained: float | None
    discovered: datetime | None
    modified: datetime | None
    containment: datetime | None
    control: datetime | None
    fire_out: datetime | None
    cause: str | None
    county: str | None
    city: str | None
    state: str | None
    protecting_unit: str | None
    personnel: int | None
    complexity: str | None

    @property
    def entity_id(self) -> str:
        return f"lw-wfigs-{self.irwin_id}"


@dataclass(frozen=True)
class Perimeter:
    """One WFIGS interagency perimeter, possibly multi-part."""

    irwin_id: str | None  # normalized
    object_id: int | None
    name: str
    parts: list[PolygonPart]
    gis_acres: float | None
    incident_size_acres: float | None
    percent_contained: float | None
    feature_category: str | None
    map_method: str | None
    polygon_time: datetime | None
    date_current: datetime | None
    county: str | None
    state: str | None

    @property
    def key(self) -> str | None:
        if self.irwin_id:
            return self.irwin_id
        return str(self.object_id) if self.object_id is not None else None

    def entity_id(self, part_index: int) -> str:
        """Part 0 (the largest) carries the contract id; other parts get ``-{n}`` with n from 2."""
        base = f"lw-perim-{self.key}"
        return base if part_index == 0 else f"{base}-{part_index + 1}"


@dataclass(frozen=True)
class NwsAlert:
    """One active NWS CAP alert (any event). Zone-based alerts have no ``parts`` of their own; their
    ``affected_zones`` URLs resolve to zone polygons (``NwsZone``)."""

    alert_id: str
    event: str
    headline: str | None
    area_desc: str
    severity: str | None
    certainty: str | None
    urgency: str | None
    sender_name: str | None
    sent: datetime | None
    effective: datetime | None
    onset: datetime | None
    expires: datetime | None
    ends: datetime | None
    parts: list[PolygonPart] = field(default_factory=list)
    affected_zones: list[str] = field(default_factory=list)

    @property
    def end_time(self) -> datetime | None:
        """Hazard end if published, else message expiry."""
        return self.ends or self.expires


@dataclass(frozen=True)
class NwsZone:
    """One api.weather.gov zone (fire, forecast or county) with its polygon parts."""

    url: str
    zone_id: str  # e.g. CAZ288
    zone_type: str | None  # fire, public, county
    name: str
    parts: list[PolygonPart] = field(default_factory=list)


@dataclass(frozen=True)
class CalFireIncident:
    """One CAL FIRE incident from the incidents.fire.ca.gov list API."""

    unique_id: str
    name: str
    lat: float | None
    lon: float | None
    is_active: bool
    acres_burned: float | None
    percent_contained: float | None
    county: str | None
    location: str | None
    admin_unit: str | None
    incident_type: str | None
    url: str | None
    started: datetime | None
    updated: datetime | None
    extinguished: datetime | None
    final: bool | None
    calfire_incident: bool | None

    @property
    def entity_id(self) -> str:
        return f"lw-calfire-{self.unique_id}"
