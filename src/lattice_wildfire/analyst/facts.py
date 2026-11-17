"""Pure fact gathering for Kodros Analyst briefs.

Everything here works on data that was already fetched: Lattice entities, NWS JSON
documents, facility lists and a fire hazard severity zone class. Nothing in this module
touches the network, the clock or the LLM, so a brief's facts are reproducible from its
inputs. ``gather_facts`` is the single entry point; the rest are its building blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lattice_wildfire.core.entities import data_type_of, lat_lon_of
from lattice_wildfire.core.geo import bearing_deg, haversine_m, point_in_polygon

FACTS_SCHEMA = "lattice-wildfire/analyst-facts/v1"

FORECAST_PERIODS = 3
NEAREST_FACILITIES = 3
NEARBY_RADIUS_KM = 10.0

# Lattice data types (CONTRACT vocabulary) that describe fire activity near a target.
FIRE_DATA_TYPES = frozenset(
    {
        "firms-viirs-hotspot",
        "wfigs-incident",
        "calfire-incident",
        "wfigs-perimeter",
        "nws-red-flag",
        "ember-candidate",
    }
)

_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


@dataclass(frozen=True)
class SourceRecord:
    """Where one input came from and when it was fetched (UTC ISO 8601)."""

    name: str
    url: str
    fetched_at: str | None
    from_cache: bool = False
    stale: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "fetched_at": self.fetched_at,
            "from_cache": self.from_cache,
            "stale": self.stale,
            "error": self.error,
        }


@dataclass(frozen=True)
class Target:
    """The thing being investigated: a Lattice entity or an operator-supplied point."""

    entity_id: str | None
    name: str
    lat: float
    lon: float
    data_type: str | None = None
    platform_type: str | None = None
    source_time: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class NearbyEntity:
    """A fire-related Lattice entity reduced to what distance math needs."""

    entity_id: str
    name: str
    data_type: str
    lat: float
    lon: float
    ring: tuple[tuple[float, float], ...] | None = None
    source_time: str | None = None


@dataclass
class AnalystInputs:
    """Everything ``gather_facts`` needs, already fetched."""

    target: Target
    nws_point: dict[str, Any] | None = None
    nws_forecast: dict[str, Any] | None = None
    nws_alerts: dict[str, Any] | None = None
    fhsz: str | None = None
    fhsz_known: bool = False  # True when the FHSZ lookup succeeded (None then means "not mapped")
    stations: Sequence[Any] | None = None  # core.layers.Facility; None = lookup failed
    hospitals: Sequence[Any] | None = None
    nearby: Sequence[NearbyEntity] | None = None  # None = Lattice picture unavailable
    sources: list[SourceRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- entities


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def entity_ring(entity: Any) -> tuple[tuple[float, float], ...] | None:
    """Outer polygon ring of a TEMPLATE_GEO entity as (lat, lon) pairs, else None."""
    shape = getattr(entity, "geo_shape", None)
    polygon = getattr(shape, "polygon", None) if shape is not None else None
    rings = getattr(polygon, "rings", None) if polygon is not None else None
    if not rings:
        return None
    ring: list[tuple[float, float]] = []
    for gp in rings[0].positions or []:
        pos = gp.position
        if pos is None or pos.latitude_degrees is None or pos.longitude_degrees is None:
            continue
        ring.append((float(pos.latitude_degrees), float(pos.longitude_degrees)))
    return tuple(ring) if len(ring) >= 3 else None


def _entity_point(entity: Any) -> tuple[float, float] | None:
    point = lat_lon_of(entity)  # location first, else geo shape point or polygon centroid
    return (float(point[0]), float(point[1])) if point is not None else None


def _entity_name(entity: Any) -> str:
    aliases = getattr(entity, "aliases", None)
    name = getattr(aliases, "name", None) if aliases is not None else None
    return name or entity.entity_id


def target_from_entity(entity: Any) -> Target:
    """Reduce the investigated Lattice entity to a ``Target``; needs a position or polygon."""
    point = _entity_point(entity)
    if point is None:
        raise ValueError(f"target entity {entity.entity_id} has no location or polygon")
    ontology = getattr(entity, "ontology", None)
    provenance = getattr(entity, "provenance", None)
    return Target(
        entity_id=entity.entity_id,
        name=_entity_name(entity),
        lat=point[0],
        lon=point[1],
        data_type=data_type_of(entity),
        platform_type=getattr(ontology, "platform_type", None) if ontology else None,
        source_time=_iso(getattr(provenance, "source_update_time", None)) if provenance else None,
        description=getattr(entity, "description", None),
    )


def summarize_fire_entity(entity: Any) -> NearbyEntity | None:
    """Keep only fire-related entities (CONTRACT data types) that have a position."""
    data_type = data_type_of(entity)
    if data_type not in FIRE_DATA_TYPES:
        return None
    point = _entity_point(entity)
    if point is None:
        return None
    provenance = getattr(entity, "provenance", None)
    return NearbyEntity(
        entity_id=entity.entity_id,
        name=_entity_name(entity),
        data_type=data_type,
        lat=point[0],
        lon=point[1],
        ring=entity_ring(entity),
        source_time=_iso(getattr(provenance, "source_update_time", None)) if provenance else None,
    )


# --------------------------------------------------------------------------- geometry


def cardinal(bearing: float) -> str:
    """16-point compass label for a bearing in degrees."""
    return _COMPASS[int((bearing % 360.0) / 22.5 + 0.5) % 16]


def _offset(lat: float, lon: float, other_lat: float, other_lon: float) -> dict[str, Any]:
    bearing = bearing_deg(lat, lon, other_lat, other_lon)
    return {
        "distance_km": round(haversine_m(lat, lon, other_lat, other_lon) / 1000.0, 1),
        "bearing_deg": round(bearing) % 360,
        "direction": cardinal(bearing),
    }


def nearest_facilities(
    lat: float, lon: float, facilities: Iterable[Any], n: int = NEAREST_FACILITIES
) -> list[dict[str, Any]]:
    """The ``n`` facilities closest to the point, with distance (km) and bearing from it."""
    ranked = sorted(facilities, key=lambda f: (haversine_m(lat, lon, f.lat, f.lon), str(f.id)))
    return [
        {"id": str(f.id), "name": f.name, "kind": f.kind, **_offset(lat, lon, f.lat, f.lon)}
        for f in ranked[:n]
    ]


def nearby_fire_activity(
    target: Target, nearby: Iterable[NearbyEntity], radius_km: float = NEARBY_RADIUS_KM
) -> list[dict[str, Any]]:
    """Fire entities whose point is within ``radius_km`` or whose polygon contains the target."""
    out: list[dict[str, Any]] = []
    for item in nearby:
        if item.entity_id == target.entity_id:
            continue
        inside = item.ring is not None and point_in_polygon(target.lat, target.lon, list(item.ring))
        offset = _offset(target.lat, target.lon, item.lat, item.lon)
        if not inside and offset["distance_km"] > radius_km:
            continue
        out.append(
            {
                "entity_id": item.entity_id,
                "name": item.name,
                "data_type": item.data_type,
                "target_inside_polygon": inside,
                **offset,
                "source_time": item.source_time,
            }
        )
    out.sort(key=lambda r: (not r["target_inside_polygon"], r["distance_km"], r["entity_id"]))
    return out


# --------------------------------------------------------------------------- NWS


def _last_segment(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1]


def nws_location(point_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Office, grid, zones and nearest named place from an NWS /points document."""
    if not point_doc:
        return None
    props = point_doc.get("properties") or {}
    rel = ((props.get("relativeLocation") or {}).get("properties")) or {}
    distance = (rel.get("distance") or {}).get("value")
    bearing = (rel.get("bearing") or {}).get("value")
    return {
        "nws_office": props.get("gridId") or props.get("cwa"),
        "grid": f"{props.get('gridId')}/{props.get('gridX')},{props.get('gridY')}"
        if props.get("gridId") is not None
        else None,
        "county_zone": _last_segment(props.get("county")),
        "fire_weather_zone": _last_segment(props.get("fireWeatherZone")),
        "forecast_zone": _last_segment(props.get("forecastZone")),
        "nearest_place": {
            "city": rel.get("city"),
            "state": rel.get("state"),
            # NWS reports distance/bearing from the named place to the requested point.
            "distance_km": round(distance / 1000.0, 1) if isinstance(distance, int | float) else None,
            "bearing_deg_from_place": round(bearing) if isinstance(bearing, int | float) else None,
        }
        if rel
        else None,
        "time_zone": props.get("timeZone"),
    }


def forecast_periods(forecast_doc: dict[str, Any] | None, n: int = FORECAST_PERIODS) -> list[dict[str, Any]]:
    """The next ``n`` NWS forecast periods with temperature, wind and precipitation chance."""
    if not forecast_doc:
        return []
    periods = (forecast_doc.get("properties") or {}).get("periods") or []
    out = []
    for p in sorted(periods, key=lambda p: p.get("number", 0))[:n]:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        out.append(
            {
                "name": p.get("name"),
                "start": p.get("startTime"),
                "end": p.get("endTime"),
                "temperature": p.get("temperature"),
                "temperature_unit": p.get("temperatureUnit"),
                "wind_speed": p.get("windSpeed"),
                "wind_direction": p.get("windDirection"),
                "precip_chance_pct": pop,
                "short_forecast": p.get("shortForecast"),
            }
        )
    return out


_ALERT_PRIORITY = ("Red Flag Warning", "Fire Weather Watch", "Extreme Fire Danger", "Fire Warning")


def active_alerts(alerts_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Active NWS alerts, fire-weather events first, then by onset."""
    if not alerts_doc:
        return []
    out = []
    for feature in alerts_doc.get("features") or []:
        p = feature.get("properties") or {}
        if p.get("status") not in (None, "Actual"):
            continue
        out.append(
            {
                "id": p.get("id"),
                "event": p.get("event"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "headline": p.get("headline"),
                "onset": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
                "sender": p.get("senderName"),
            }
        )

    def rank(alert: dict[str, Any]) -> tuple[int, str]:
        event = alert.get("event") or ""
        fire = next((i for i, name in enumerate(_ALERT_PRIORITY) if name == event), len(_ALERT_PRIORITY))
        return fire, alert.get("onset") or ""

    return sorted(out, key=rank)


def _forecast_meta(forecast_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not forecast_doc:
        return None
    props = forecast_doc.get("properties") or {}
    return {"updated": props.get("updateTime"), "generated": props.get("generatedAt")}


# --------------------------------------------------------------------------- facts

FHSZ_HAZARD_CLASSES = frozenset({"Moderate", "High", "Very High"})


def fhsz_status(fhsz_class: str | None) -> str:
    """Plain reading of a successful FHSZ lookup (``NonWildland`` is mapped but not a hazard class)."""
    if fhsz_class is None:
        return "not in a mapped FHSZ"
    if fhsz_class in FHSZ_HAZARD_CLASSES:
        return "fire hazard severity zone"
    return "mapped, not a fire hazard class"


def _target_facts(target: Target) -> dict[str, Any]:
    return {
        "entity_id": target.entity_id,
        "name": target.name,
        "data_type": target.data_type,
        "platform_type": target.platform_type,
        "lat": round(target.lat, 5),
        "lon": round(target.lon, 5),
        "source_time": target.source_time,
        "description": target.description,
    }


def _unknowns(inputs: AnalystInputs, forecast: list[dict[str, Any]]) -> list[str]:
    unknowns: list[str] = [
        f"{s.name}: not available ({s.error})" for s in inputs.sources if s.error is not None
    ]
    if inputs.nws_forecast is not None and not forecast:
        unknowns.append("NWS forecast returned no periods")
    if inputs.target.data_type == "firms-viirs-hotspot":
        unknowns.append("Satellite thermal detection only; not ground-confirmed")
    if inputs.target.data_type == "ember-candidate":
        unknowns.append("Fused new-start candidate; not ground-confirmed")
    unknowns.append("Fire size, rate of spread, fuel moisture and cause are not in the gathered sources")
    return unknowns


def gather_facts(inputs: AnalystInputs, *, task_id: str | None, generated_at: str) -> dict[str, Any]:
    """Structured, source-attributed facts for one target. Pure: same inputs, same facts.

    Every value comes from ``inputs``; a missing input becomes ``None`` (or an empty list
    when the lookup succeeded and found nothing) plus an entry in ``unknowns``.
    """
    target = inputs.target
    forecast = forecast_periods(inputs.nws_forecast)
    stations = (
        nearest_facilities(target.lat, target.lon, inputs.stations) if inputs.stations is not None else None
    )
    hospitals = (
        nearest_facilities(target.lat, target.lon, inputs.hospitals) if inputs.hospitals is not None else None
    )
    nearby = nearby_fire_activity(target, inputs.nearby) if inputs.nearby is not None else None
    return {
        "schema": FACTS_SCHEMA,
        "task_id": task_id,
        "generated_at": generated_at,
        "target": _target_facts(target),
        "location": nws_location(inputs.nws_point),
        "fuel": {
            "fhsz": inputs.fhsz if inputs.fhsz_known else None,
            "fhsz_status": fhsz_status(inputs.fhsz) if inputs.fhsz_known else "unavailable",
        },
        "weather": {
            "forecast": _forecast_meta(inputs.nws_forecast),
            "periods": forecast,
            "alerts": active_alerts(inputs.nws_alerts) if inputs.nws_alerts is not None else None,
        },
        "exposures": {"hospitals": hospitals},
        "resources": {"fire_stations": stations},
        "nearby_fire_activity": {"radius_km": NEARBY_RADIUS_KM, "entities": nearby},
        "unknowns": _unknowns(inputs, forecast),
        "sources": [s.as_dict() for s in inputs.sources],
    }
