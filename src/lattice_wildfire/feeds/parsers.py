"""Pure parsers: raw feed payloads -> typed records. No I/O.

Each parser keeps every record the source delivered. Records that cannot be published (no id, no
location, no geometry) are kept with ``None`` fields so the pipeline can count why they were skipped.
A structurally wrong payload (missing CSV header, not a FeatureCollection) raises ``ValueError``.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from .records import (
    CalFireIncident,
    Hotspot,
    NwsAlert,
    NwsZone,
    Perimeter,
    PolygonPart,
    Ring,
    WfigsIncident,
)

FIRMS_COLUMNS = (
    "latitude",
    "longitude",
    "bright_ti4",
    "scan",
    "track",
    "acq_date",
    "acq_time",
    "satellite",
    "confidence",
    "version",
    "bright_ti5",
    "frp",
    "daynight",
)
# The 24 h text files spell confidence out; the FIRMS area API abbreviates it.
_VIIRS_CONFIDENCE = {
    "l": "low",
    "n": "nominal",
    "h": "high",
    "low": "low",
    "nominal": "nominal",
    "high": "high",
}

RED_FLAG_EVENT = "Red Flag Warning"


# ---------------------------------------------------------------- shared helpers


def normalize_irwin_id(value: Any) -> str | None:
    """IRWIN GUIDs arrive as ``{ABCD-...}`` in some layers and bare in others; strip braces, upper-case."""
    if value is None:
        return None
    text = str(value).strip().strip("{}").strip().upper()
    return text or None


def epoch_ms(value: Any) -> datetime | None:
    """ArcGIS date fields are epoch milliseconds (UTC)."""
    if value is None or value == "":
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def iso_time(value: Any) -> datetime | None:
    """RFC 3339 / ISO 8601 with ``Z`` or offset -> aware UTC datetime. Empty -> None."""
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return parsed.astimezone(UTC)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _features(data: Any, what: str) -> list[dict]:
    if (
        not isinstance(data, dict)
        or data.get("type") != "FeatureCollection"
        or not isinstance(data.get("features"), list)
    ):
        raise ValueError(f"{what}: expected a GeoJSON FeatureCollection")
    return data["features"]


def _ring(coords: list) -> Ring:
    """GeoJSON ``[lon, lat(, z)]`` positions -> ``[(lat, lon)]``."""
    return [(float(pos[1]), float(pos[0])) for pos in coords]


def geojson_polygon_parts(geometry: dict | None) -> list[PolygonPart]:
    """Polygon / MultiPolygon / GeometryCollection -> list of parts (each a list of rings). Other types -> []."""
    if not geometry:
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [[_ring(r) for r in coords]] if coords else []
    if kind == "MultiPolygon":
        return [[_ring(r) for r in poly] for poly in coords if poly]
    if kind == "GeometryCollection":
        return [part for sub in geometry.get("geometries") or [] for part in geojson_polygon_parts(sub)]
    return []


def _point(geometry: dict | None) -> tuple[float, float] | None:
    if geometry and geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"][:2]
        if lon is not None and lat is not None:
            return float(lat), float(lon)
    return None


# ---------------------------------------------------------------- FIRMS


def parse_firms_csv(text: str) -> list[Hotspot]:
    """FIRMS VIIRS active-fire CSV (24 h text file or area API) -> hotspots."""
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames
    if not header:
        raise ValueError("FIRMS CSV: missing header")
    missing = [c for c in FIRMS_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"FIRMS CSV: missing columns {missing}")
    out: list[Hotspot] = []
    for line_no, row in enumerate(reader, start=2):
        try:
            acq_time = row["acq_time"].strip().zfill(4)
            acq_date = row["acq_date"].strip()
            acquired = datetime.strptime(f"{acq_date} {acq_time}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
            confidence_raw = row["confidence"].strip()
            out.append(
                Hotspot(
                    satellite=row["satellite"].strip(),
                    acq_date=acq_date,
                    acq_time=acq_time,
                    acquired=acquired,
                    lat=float(row["latitude"]),
                    lon=float(row["longitude"]),
                    bright_ti4_k=float(row["bright_ti4"]),
                    bright_ti5_k=float(row["bright_ti5"]),
                    frp_mw=float(row["frp"]),
                    scan_km=float(row["scan"]),
                    track_km=float(row["track"]),
                    confidence=_VIIRS_CONFIDENCE.get(confidence_raw.lower(), confidence_raw),
                    version=row["version"].strip(),
                    daynight=row["daynight"].strip(),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"FIRMS CSV line {line_no}: {exc}") from exc
    return out


# ---------------------------------------------------------------- WFIGS


def parse_wfigs_incidents(data: Any) -> list[WfigsIncident]:
    """WFIGS Incident Locations (Current) GeoJSON -> incidents."""
    out: list[WfigsIncident] = []
    for feature in _features(data, "WFIGS incidents"):
        p = feature.get("properties") or {}
        point = _point(feature.get("geometry"))
        if point is None and p.get("InitialLatitude") is not None and p.get("InitialLongitude") is not None:
            point = (float(p["InitialLatitude"]), float(p["InitialLongitude"]))
        out.append(
            WfigsIncident(
                irwin_id=normalize_irwin_id(p.get("IrwinID")),
                name=_str(p.get("IncidentName")) or "Unnamed incident",
                lat=point[0] if point else None,
                lon=point[1] if point else None,
                unique_fire_id=_str(p.get("UniqueFireIdentifier")),
                type_category=_str(p.get("IncidentTypeCategory")),
                size_acres=_float(p.get("IncidentSize")),
                discovery_acres=_float(p.get("DiscoveryAcres")),
                percent_contained=_float(p.get("PercentContained")),
                discovered=epoch_ms(p.get("FireDiscoveryDateTime")),
                modified=epoch_ms(p.get("ModifiedOnDateTime_dt")),
                containment=epoch_ms(p.get("ContainmentDateTime")),
                control=epoch_ms(p.get("ControlDateTime")),
                fire_out=epoch_ms(p.get("FireOutDateTime")),
                cause=_str(p.get("FireCause")),
                county=_str(p.get("POOCounty")),
                city=_str(p.get("POOCity")),
                state=_str(p.get("POOState")),
                protecting_unit=_str(p.get("POOProtectingUnit")),
                personnel=_int(p.get("TotalIncidentPersonnel")),
                complexity=_str(p.get("IncidentComplexityLevel")),
            )
        )
    return out


def parse_wfigs_perimeters(data: Any) -> list[Perimeter]:
    """WFIGS Interagency Perimeters (Current) GeoJSON -> perimeters (``poly_`` / ``attr_`` fields)."""
    out: list[Perimeter] = []
    for feature in _features(data, "WFIGS perimeters"):
        p = feature.get("properties") or {}
        out.append(
            Perimeter(
                irwin_id=normalize_irwin_id(p.get("poly_IRWINID") or p.get("attr_IrwinID")),
                object_id=_int(p.get("OBJECTID")),
                name=_str(p.get("poly_IncidentName")) or _str(p.get("attr_IncidentName")) or "Unnamed fire",
                parts=geojson_polygon_parts(feature.get("geometry")),
                gis_acres=_float(p.get("poly_GISAcres")),
                incident_size_acres=_float(p.get("attr_IncidentSize")),
                percent_contained=_float(p.get("attr_PercentContained")),
                feature_category=_str(p.get("poly_FeatureCategory")),
                map_method=_str(p.get("poly_MapMethod")),
                polygon_time=epoch_ms(p.get("poly_PolygonDateTime")),
                date_current=epoch_ms(p.get("poly_DateCurrent")),
                county=_str(p.get("attr_POOCounty")),
                state=_str(p.get("attr_POOState")),
            )
        )
    return out


# ---------------------------------------------------------------- NWS


def parse_nws_alerts(data: Any, event: str | None = RED_FLAG_EVENT) -> list[NwsAlert]:
    """api.weather.gov active alerts GeoJSON -> alerts of ``event`` (``None`` keeps every event)."""
    out: list[NwsAlert] = []
    for feature in _features(data, "NWS alerts"):
        p = feature.get("properties") or {}
        if event is not None and p.get("event") != event:
            continue
        alert_id = _str(p.get("id")) or _str(feature.get("id"))
        if alert_id is None:
            raise ValueError("NWS alert without id")
        out.append(
            NwsAlert(
                alert_id=alert_id,
                event=_str(p.get("event")) or "Alert",
                headline=_str(p.get("headline")),
                area_desc=_str(p.get("areaDesc")) or "",
                severity=_str(p.get("severity")),
                certainty=_str(p.get("certainty")),
                urgency=_str(p.get("urgency")),
                sender_name=_str(p.get("senderName")),
                sent=iso_time(p.get("sent")),
                effective=iso_time(p.get("effective")),
                onset=iso_time(p.get("onset")),
                expires=iso_time(p.get("expires")),
                ends=iso_time(p.get("ends")),
                parts=geojson_polygon_parts(feature.get("geometry")),
                affected_zones=[str(z) for z in p.get("affectedZones") or []],
            )
        )
    return out


def parse_nws_zone(data: Any, url: str | None = None) -> NwsZone:
    """api.weather.gov ``/zones/{type}/{id}`` GeoJSON Feature -> zone with polygon parts."""
    if not isinstance(data, dict) or data.get("type") != "Feature":
        raise ValueError("NWS zone: expected a GeoJSON Feature")
    p = data.get("properties") or {}
    zone_id = _str(p.get("id"))
    if zone_id is None:
        raise ValueError("NWS zone without properties.id")
    return NwsZone(
        url=url or _str(data.get("id")) or zone_id,
        zone_id=zone_id,
        zone_type=_str(p.get("type")),
        name=_str(p.get("name")) or zone_id,
        parts=geojson_polygon_parts(data.get("geometry")),
    )


# ---------------------------------------------------------------- CAL FIRE


def parse_calfire_incidents(data: Any) -> list[CalFireIncident]:
    """incidents.fire.ca.gov ``IncidentApi/List`` JSON array -> incidents."""
    if not isinstance(data, list):
        raise ValueError("CAL FIRE incidents: expected a JSON array")
    out: list[CalFireIncident] = []
    for item in data:
        unique_id = _str(item.get("UniqueId"))
        if unique_id is None:
            raise ValueError(f"CAL FIRE incident without UniqueId: {item.get('Name')!r}")
        lat, lon = _float(item.get("Latitude")), _float(item.get("Longitude"))
        # The list API reports 0,0 for incidents it has not located yet.
        if lat == 0 and lon == 0:
            lat = lon = None
        out.append(
            CalFireIncident(
                unique_id=unique_id,
                name=_str(item.get("Name")) or "Unnamed incident",
                lat=lat,
                lon=lon,
                is_active=bool(item.get("IsActive")),
                acres_burned=_float(item.get("AcresBurned")),
                percent_contained=_float(item.get("PercentContained")),
                county=_str(item.get("County")),
                location=_str(item.get("Location")),
                admin_unit=_str(item.get("AdminUnit")),
                incident_type=_str(item.get("Type")),
                url=_str(item.get("Url")),
                started=iso_time(item.get("Started")),
                updated=iso_time(item.get("Updated")),
                extinguished=iso_time(item.get("ExtinguishedDate")),
                final=item.get("Final"),
                calfire_incident=item.get("CalFireIncident"),
            )
        )
    return out
