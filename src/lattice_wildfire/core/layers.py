"""Real LA County eGIS layers (fields verified live 2026-11-09).

Facilities come from the LA County Location Management System (LMS) public map service; fire hazard
severity zones from the LA County Hazards map service (composite of CAL FIRE's LRA and SRA zones).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import AOI, aoi_from_env
from .http import arcgis_query

LMS = "https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/LMS_Data_Public/MapServer"
FIRE_STATIONS_URL = f"{LMS}/137"
HOSPITALS_URL = f"{LMS}/81"
HELIPORTS_URL = f"{LMS}/183"
# Composite of "Fire Hazard Severity Zones LRA" (18) and "SRA" (19); fields HAZ_CLASS, HAZ_CODE, SRA.
FHSZ_URL = "https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/Hazards/MapServer/2"

LACOFD_ORG_NAME = "Los Angeles County Fire Department"
_STATION_RE = re.compile(r"\bStation\s+(\d+[A-Za-z]?)\b")
_FACILITY_TTL_S = 24 * 3600  # facility lists change rarely
_FHSZ_TTL_S = 7 * 24 * 3600


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    kind: str
    lat: float
    lon: float
    attrs: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


def station_number(name: str) -> str | None:
    """``"Los Angeles County Fire Department - Station 63"`` -> ``"63"``."""
    m = _STATION_RE.search(name or "")
    return m.group(1) if m else None


def facilities_from_geojson(collection: dict, kind: str) -> list[Facility]:
    """Parse an LMS-layer GeoJSON FeatureCollection into facilities (point features only).

    The LMS layers carry duplicate records for one facility (e.g. Station 182 twice on 2026-11-09);
    duplicates of an id collapse to the record with the latest ``date_updated``.
    """
    best: dict[str, Facility] = {}
    for feat in collection.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        props = dict(feat.get("properties") or {})
        name = " ".join((props.get("name") or "").split())
        if kind == "fire_station":
            number = station_number(name)
            props["station_number"] = number
            fid = number or f"oid-{props.get('OBJECTID', feat.get('id'))}"
        else:
            ext = props.get("ext_id")
            fid = str(ext) if ext not in (None, "") else f"oid-{props.get('OBJECTID', feat.get('id'))}"
        facility = Facility(id=fid, name=name, kind=kind, lat=float(lat), lon=float(lon), attrs=props)
        current = best.get(fid)
        if current is None or (props.get("date_updated") or 0) > (current.attrs.get("date_updated") or 0):
            best[fid] = facility
    return sorted(best.values(), key=lambda f: (f.kind, f.name, f.id))


def fire_stations(aoi: AOI | None = None) -> list[Facility]:
    """LA County Fire Department stations (``org_name`` filter) inside the AOI. ``id`` is the station number."""
    fc = arcgis_query(
        FIRE_STATIONS_URL,
        where=f"org_name = '{LACOFD_ORG_NAME}'",
        geometry_bbox=aoi or aoi_from_env(),
        ttl_s=_FACILITY_TTL_S,
    )
    return facilities_from_geojson(fc.data, "fire_station")


def hospitals(aoi: AOI | None = None) -> list[Facility]:
    fc = arcgis_query(HOSPITALS_URL, geometry_bbox=aoi or aoi_from_env(), ttl_s=_FACILITY_TTL_S)
    return facilities_from_geojson(fc.data, "hospital")


def heliports(aoi: AOI | None = None) -> list[Facility]:
    fc = arcgis_query(HELIPORTS_URL, geometry_bbox=aoi or aoi_from_env(), ttl_s=_FACILITY_TTL_S)
    return facilities_from_geojson(fc.data, "heliport")


_FHSZ_RANK = {"Very High": 3, "High": 2, "Moderate": 1}


def fhsz_class_from_geojson(collection: dict) -> str | None:
    """Most severe ``HAZ_CLASS`` among intersecting zones (e.g. ``"Very High"``, ``"NonWildland"``)."""
    classes = [(f.get("properties") or {}).get("HAZ_CLASS") for f in collection.get("features") or []]
    classes = [c for c in classes if c]
    if not classes:
        return None
    return max(classes, key=lambda c: (_FHSZ_RANK.get(c, 0), c))


def fhsz_at(lat: float, lon: float) -> str | None:
    """CAL FIRE Fire Hazard Severity Zone class at a point in LA County, or ``None`` outside mapped zones."""
    fc = arcgis_query(
        FHSZ_URL,
        out_fields="HAZ_CLASS,HAZ_CODE,SRA",
        point=(lat, lon),
        return_geometry=False,
        ttl_s=_FHSZ_TTL_S,
    )
    return fhsz_class_from_geojson(fc.data)
