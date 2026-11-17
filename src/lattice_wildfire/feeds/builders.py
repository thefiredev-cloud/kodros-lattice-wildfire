"""Pure builders: one record -> ``publish_entity`` keyword arguments (SDK model objects).

Every payload follows docs/CONTRACT.md: deterministic ``entity_id``, ``is_live=True``, future
``expiry_time``, provenance ``lattice-wildfire-feeds`` + data_type, non-empty ``aliases.name``,
UNCLASSIFIED marking, and SIDC symbology on tracks and assets.

``provenance.source_update_time`` is the publish time, not the source time: Entity Manager discards
an update whose source_update_time did not change, so a heartbeat republish with the source time
would never extend the expiry. Source times live in ``created_time``, ``tracked.last_measurement_time``
and the ``key: value`` description lines.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from anduril import (
    Aliases,
    AlternateId,
    ComponentHealth,
    ComponentMessage,
    GeoPolygonPosition,
    Health,
    LinearRing,
    MilView,
    Ontology,
    Position,
    Tracked,
)

from lattice_wildfire.core.entities import (
    now_utc,
    point_location,
    polygon_geo,
    provenance,
    symbology,
    unclassified,
)
from lattice_wildfire.core.layers import Facility

from .geometry import ranked_parts
from .records import (
    SATELLITE_NAMES,
    CalFireIncident,
    Hotspot,
    NwsAlert,
    NwsZone,
    Perimeter,
    PolygonPart,
    WfigsIncident,
)

PKG = "feeds"

DT_HOTSPOT = "firms-viirs-hotspot"
DT_WFIGS_INCIDENT = "wfigs-incident"
DT_CALFIRE_INCIDENT = "calfire-incident"
DT_PERIMETER = "wfigs-perimeter"
DT_RED_FLAG = "nws-red-flag"
DT_STATION = "lacofd-station"

HOTSPOT_TTL_S = 6 * 3600
INCIDENT_TTL_S = 3600
PERIMETER_TTL_S = 3600
RED_FLAG_TTL_S = 3600
STATION_TTL_S = 600

GEO_TYPE = "GEO_TYPE_HAZARD"
UNKNOWN_LAND = ("DISPOSITION_UNKNOWN", "ENVIRONMENT_LAND")
FRIENDLY_LAND = ("DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND")

# VIIRS confidence class -> Lattice track quality (0 tentative .. 15 firm).
TRACK_QUALITY = {"low": 1, "nominal": 2, "high": 3}
INCIDENT_TRACK_QUALITY = 3
INCIDENT_TYPES = {"WF": "Wildfire", "RX": "Prescribed Fire", "CX": "Complex"}


# ---------------------------------------------------------------- helpers


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _num(value: float | None) -> str | None:
    """Plain decimal, never exponent notation: ``385.0`` -> ``385``, ``25349.505227`` -> ``25349.505``."""
    if value is None:
        return None
    value = round(float(value), 3)
    return str(int(value)) if value.is_integer() else str(value)


def describe(title: str, fields: list[tuple[str, Any]]) -> str:
    """Title line, then one ``key: value`` line per field that has a value (stable, machine-readable)."""
    lines = [title]
    for key, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _expiry_at(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def _provenance(data_type: str, source_id: str):
    return provenance(PKG, data_type).model_copy(update={"source_id": source_id})


def _alt_ids(*pairs: tuple[str | None, str]) -> list[AlternateId]:
    return [AlternateId(id=value, type=kind) for value, kind in pairs if value]


def _linear_ring(ring: list[tuple[float, float]]) -> LinearRing:
    return LinearRing(
        positions=[
            GeoPolygonPosition(position=Position(latitude_degrees=lat, longitude_degrees=lon))
            for lat, lon in ring
        ]
    )


def geo_polygon(part: PolygonPart):
    """Canonical part (exterior first, holes after) -> ``(GeoDetails, GeoShape)`` with GEO_TYPE_HAZARD."""
    details, shape = polygon_geo(part[0], GEO_TYPE)
    if len(part) > 1:
        rings = [*shape.polygon.rings, *(_linear_ring(hole) for hole in part[1:])]
        shape = shape.model_copy(update={"polygon": shape.polygon.model_copy(update={"rings": rings})})
    return details, shape


def _track_common(disposition_environment: tuple[str, str]) -> dict[str, Any]:
    disposition, environment = disposition_environment
    return {
        "mil_view": MilView(disposition=disposition, environment=environment),
        "symbology": symbology(disposition, environment),
        "data_classification": unclassified(),
    }


# ---------------------------------------------------------------- FIRMS


def build_hotspot_entity(h: Hotspot, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    sat_name = SATELLITE_NAMES.get(h.satellite, h.satellite)
    return {
        "entity_id": h.entity_id,
        "is_live": True,
        "created_time": h.acquired,
        "expiry_time": _expiry_at(now, HOTSPOT_TTL_S),
        "provenance": _provenance(DT_HOTSPOT, h.source_id),
        "aliases": Aliases(
            name=f"VIIRS {h.satellite} hotspot {h.acq_date} {h.acq_time[:2]}:{h.acq_time[2:]}Z"
        ),
        "description": describe(
            f"FIRMS VIIRS active fire detection ({sat_name})",
            [
                ("satellite", h.satellite),
                ("acquired", _iso(h.acquired)),
                ("frp_mw", _num(h.frp_mw)),
                ("confidence", h.confidence),
                ("bright_ti4_k", _num(h.bright_ti4_k)),
                ("bright_ti5_k", _num(h.bright_ti5_k)),
                ("scan_km", _num(h.scan_km)),
                ("track_km", _num(h.track_km)),
                ("daynight", h.daynight),
                ("version", h.version),
            ],
        ),
        "location": point_location(h.lat, h.lon),
        "ontology": Ontology(
            template="TEMPLATE_TRACK", platform_type="Wildfire Hotspot", specific_type=f"VIIRS {h.satellite}"
        ),
        "tracked": Tracked(
            track_quality_wrapper=TRACK_QUALITY.get(h.confidence, 2), last_measurement_time=h.acquired
        ),
        **_track_common(UNKNOWN_LAND),
    }


# ---------------------------------------------------------------- WFIGS incidents


def build_wfigs_incident_entity(inc: WfigsIncident, now: datetime | None = None) -> dict[str, Any]:
    if not inc.irwin_id or inc.lat is None or inc.lon is None:
        raise ValueError(f"WFIGS incident {inc.name!r} lacks IrwinID or location")
    now = now or now_utc()
    kind = INCIDENT_TYPES.get(inc.type_category or "", inc.type_category)
    label = f"{inc.name} ({inc.unique_fire_id})" if inc.unique_fire_id else inc.name
    return {
        "entity_id": inc.entity_id,
        "is_live": True,
        **({"created_time": inc.discovered} if inc.discovered else {}),
        "expiry_time": _expiry_at(now, INCIDENT_TTL_S),
        "provenance": _provenance(DT_WFIGS_INCIDENT, inc.irwin_id),
        "aliases": Aliases(
            name=label,
            alternate_ids=_alt_ids(
                (inc.irwin_id, "ALT_ID_TYPE_TRACK_ID_1"), (inc.unique_fire_id, "ALT_ID_TYPE_TRACK_ID_2")
            ),
        ),
        "description": describe(
            f"WFIGS incident: {inc.name}",
            [
                ("irwin_id", inc.irwin_id),
                ("unique_fire_id", inc.unique_fire_id),
                ("type", f"{inc.type_category} ({kind})" if inc.type_category else None),
                ("size_acres", _num(inc.size_acres)),
                ("discovery_acres", _num(inc.discovery_acres)),
                ("percent_contained", _num(inc.percent_contained)),
                ("discovered", _iso(inc.discovered)),
                ("contained", _iso(inc.containment)),
                ("controlled", _iso(inc.control)),
                ("fire_out", _iso(inc.fire_out)),
                ("modified", _iso(inc.modified)),
                ("cause", inc.cause),
                ("complexity", inc.complexity),
                ("personnel", inc.personnel),
                ("county", inc.county),
                ("city", inc.city),
                ("state", inc.state),
                ("protecting_unit", inc.protecting_unit),
            ],
        ),
        "location": point_location(inc.lat, inc.lon),
        "ontology": Ontology(
            template="TEMPLATE_TRACK", platform_type="Wildfire Incident", specific_type=kind
        ),
        "tracked": Tracked(
            track_quality_wrapper=INCIDENT_TRACK_QUALITY,
            **({"last_measurement_time": inc.modified} if inc.modified else {}),
        ),
        **_track_common(UNKNOWN_LAND),
    }


# ---------------------------------------------------------------- CAL FIRE


def build_calfire_entity(inc: CalFireIncident, now: datetime | None = None) -> dict[str, Any]:
    if inc.lat is None or inc.lon is None:
        raise ValueError(f"CAL FIRE incident {inc.name!r} has no location")
    now = now or now_utc()
    return {
        "entity_id": inc.entity_id,
        "is_live": True,
        **({"created_time": inc.started} if inc.started else {}),
        "expiry_time": _expiry_at(now, INCIDENT_TTL_S),
        "provenance": _provenance(DT_CALFIRE_INCIDENT, inc.unique_id),
        "aliases": Aliases(name=inc.name),
        "description": describe(
            f"CAL FIRE incident: {inc.name}",
            [
                ("unique_id", inc.unique_id),
                ("type", inc.incident_type),
                ("active", "yes" if inc.is_active else "no"),
                ("acres_burned", _num(inc.acres_burned)),
                ("percent_contained", _num(inc.percent_contained)),
                ("started", _iso(inc.started)),
                ("updated", _iso(inc.updated)),
                ("extinguished", _iso(inc.extinguished)),
                ("county", inc.county),
                ("location", inc.location),
                ("admin_unit", inc.admin_unit),
                ("url", inc.url),
            ],
        ),
        "location": point_location(inc.lat, inc.lon),
        "ontology": Ontology(
            template="TEMPLATE_TRACK", platform_type="Wildfire Incident", specific_type=inc.incident_type
        ),
        "tracked": Tracked(
            track_quality_wrapper=INCIDENT_TRACK_QUALITY,
            **({"last_measurement_time": inc.updated} if inc.updated else {}),
        ),
        **_track_common(UNKNOWN_LAND),
    }


# ---------------------------------------------------------------- WFIGS perimeters


def build_perimeter_entities(per: Perimeter, now: datetime | None = None) -> list[dict[str, Any]]:
    """One TEMPLATE_GEO entity per polygon part; largest part keeps the contract id."""
    if per.key is None:
        raise ValueError(f"perimeter {per.name!r} has neither IRWIN id nor OBJECTID")
    now = now or now_utc()
    parts = ranked_parts(per.parts)
    out: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        details, shape = geo_polygon(part)
        suffix = f" (part {index + 1}/{len(parts)})" if len(parts) > 1 else ""
        out.append(
            {
                "entity_id": per.entity_id(index),
                "is_live": True,
                "expiry_time": _expiry_at(now, PERIMETER_TTL_S),
                "provenance": _provenance(DT_PERIMETER, per.key),
                "aliases": Aliases(
                    name=f"{per.name} perimeter{suffix}",
                    alternate_ids=_alt_ids((per.irwin_id, "ALT_ID_TYPE_TRACK_ID_1")),
                ),
                "description": describe(
                    f"WFIGS perimeter: {per.name}{suffix}",
                    [
                        ("irwin_id", per.irwin_id),
                        ("object_id", per.object_id),
                        ("part", f"{index + 1}/{len(parts)}"),
                        ("gis_acres", _num(per.gis_acres)),
                        ("incident_size_acres", _num(per.incident_size_acres)),
                        ("percent_contained", _num(per.percent_contained)),
                        ("feature_category", per.feature_category),
                        ("map_method", per.map_method),
                        ("polygon_time", _iso(per.polygon_time)),
                        ("date_current", _iso(per.date_current)),
                        ("county", per.county),
                        ("state", per.state),
                    ],
                ),
                "ontology": Ontology(template="TEMPLATE_GEO", platform_type="Wildfire Perimeter"),
                "geo_details": details,
                "geo_shape": shape,
                "data_classification": unclassified(),
            }
        )
    return out


# ---------------------------------------------------------------- NWS red flag


def alert_key(alert_id: str) -> str:
    return hashlib.sha256(alert_id.encode()).hexdigest()[:16]


def _alert_geometries(
    alert: NwsAlert, zones: Sequence[NwsZone]
) -> list[tuple[str, str, NwsZone | None, int, int, PolygonPart]]:
    """``(entity_id, area label, zone, part index, part count, part)`` for every polygon to publish.

    An alert with its own geometry keeps ``lw-nws-{hash}`` (extra parts ``-{n}``); a zone-based alert
    gets ``lw-nws-{hash}-{zone id}`` per zone (extra parts ``-{zone id}-{n}``).
    """
    base = f"lw-nws-{alert_key(alert.alert_id)}"
    out = []
    if alert.parts:
        parts = ranked_parts(alert.parts)
        for index, part in enumerate(parts):
            out.append(
                (
                    base if index == 0 else f"{base}-{index + 1}",
                    alert.area_desc,
                    None,
                    index,
                    len(parts),
                    part,
                )
            )
        return out
    for zone in zones:
        parts = ranked_parts(zone.parts)
        zone_base = f"{base}-{zone.zone_id}"
        for index, part in enumerate(parts):
            out.append(
                (
                    zone_base if index == 0 else f"{zone_base}-{index + 1}",
                    zone.name,
                    zone,
                    index,
                    len(parts),
                    part,
                )
            )
    return out


def build_red_flag_entities(
    alert: NwsAlert, now: datetime | None = None, zones: Sequence[NwsZone] = ()
) -> list[dict[str, Any]]:
    """One TEMPLATE_GEO entity per alert polygon part, or per resolved zone part when the alert has no
    geometry of its own. Expiry = min(alert end, now + 1 h)."""
    now = now or now_utc()
    end = alert.end_time
    if end is not None and end <= now:
        raise ValueError(f"alert {alert.alert_id} ended at {_iso(end)}")
    expires = _expiry_at(now, RED_FLAG_TTL_S)
    if end is not None and end < expires:
        expires = end
    out: list[dict[str, Any]] = []
    for entity_id, area_label, zone, index, count, part in _alert_geometries(alert, zones):
        details, shape = geo_polygon(part)
        suffix = f" (part {index + 1}/{count})" if count > 1 else ""
        area = f"{alert.event}: {area_label}" if area_label else alert.event
        limit = 120 - len(suffix)
        name = (area[: limit - 3] + "...") if len(area) > limit else area
        out.append(
            {
                "entity_id": entity_id,
                "is_live": True,
                "expiry_time": expires,
                "provenance": _provenance(DT_RED_FLAG, alert.alert_id),
                "aliases": Aliases(name=name + suffix),
                "description": describe(
                    alert.headline or alert.event,
                    [
                        ("event", alert.event),
                        ("alert_id", alert.alert_id),
                        ("area", alert.area_desc),
                        ("zone", f"{zone.zone_id} ({zone.name})" if zone else None),
                        ("zone_url", zone.url if zone else None),
                        ("severity", alert.severity),
                        ("certainty", alert.certainty),
                        ("urgency", alert.urgency),
                        ("sender", alert.sender_name),
                        ("sent", _iso(alert.sent)),
                        ("onset", _iso(alert.onset)),
                        ("ends", _iso(alert.ends)),
                        ("expires", _iso(alert.expires)),
                        ("part", f"{index + 1}/{count}" if count > 1 else None),
                    ],
                ),
                "ontology": Ontology(template="TEMPLATE_GEO", platform_type=alert.event),
                "geo_details": details,
                "geo_shape": shape,
                "data_classification": unclassified(),
            }
        )
    return out


# ---------------------------------------------------------------- LACoFD stations


# Station-layer attributes worth carrying (the LMS layer has ~35 fields, most are catalog plumbing).
STATION_ATTRS = ("addrln1", "city", "zip", "phones", "hours", "ext_id", "OBJECTID", "source")


def station_number(fac: Facility) -> str:
    """``Facility.id`` is the station number parsed from the name (``oid-<OBJECTID>`` if absent)."""
    return str(fac.id)


def build_station_entity(fac: Facility, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    number = station_number(fac)
    status = "HEALTH_STATUS_HEALTHY"
    display = fac.name if number.startswith("oid-") else f"LACoFD Station {number}"
    attrs = fac.attrs or {}
    disposition, environment = FRIENDLY_LAND
    return {
        "entity_id": f"lw-station-{number}",
        "is_live": True,
        "expiry_time": _expiry_at(now, STATION_TTL_S),
        "provenance": _provenance(DT_STATION, number),
        "aliases": Aliases(name=display),
        "description": describe(
            fac.name or display,
            [("station", number), *((key.lower(), attrs.get(key)) for key in STATION_ATTRS)],
        ),
        "location": point_location(fac.lat, fac.lon),
        "ontology": Ontology(template="TEMPLATE_ASSET", platform_type="Fire Station"),
        "mil_view": MilView(
            disposition=disposition,
            environment=environment,
            nationality="NATIONALITY_UNITED_STATES_OF_AMERICA",
        ),
        "symbology": symbology(disposition, environment),
        "data_classification": unclassified(),
        # Health reflects this integration's view: the station is listed in the county layer and the
        # feed that reports it is online. Stations carry no sensors, so no ``sensors`` component.
        "health": Health(
            health_status=status,
            connection_status="CONNECTION_STATUS_ONLINE",
            update_time=now,
            components=[
                ComponentHealth(
                    id="egis-listing",
                    name="LA County eGIS station listing",
                    health=status,
                    messages=[
                        ComponentMessage(
                            status=status, message="Listed in the LA County eGIS fire station layer"
                        )
                    ],
                )
            ],
        ),
    }
