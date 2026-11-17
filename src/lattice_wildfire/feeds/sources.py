"""Feed sources: fetch (I/O edge) -> parse -> select for the AOI -> publish payloads.

``select_*`` functions are pure: records + AOI -> ``(payloads, skipped)`` where ``skipped`` counts why
records were not published. ``collect_*`` functions add the network fetch and return a ``Batch``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import now_utc
from lattice_wildfire.core.geo import bbox_contains
from lattice_wildfire.core.http import Fetched, arcgis_query, fetch_json, fetch_text
from lattice_wildfire.core.layers import Facility, fire_stations

from . import builders
from .geometry import part_intersects_aoi, ranked_parts
from .parsers import (
    RED_FLAG_EVENT,
    parse_calfire_incidents,
    parse_firms_csv,
    parse_nws_alerts,
    parse_nws_zone,
    parse_wfigs_incidents,
    parse_wfigs_perimeters,
)
from .records import (
    CalFireIncident,
    Hotspot,
    NwsAlert,
    NwsZone,
    Perimeter,
    WfigsIncident,
)

log = logging.getLogger(__name__)

FIRMS_URLS = {
    "N20": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv",
    "N21": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv",
    "N": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv",
}
# ArcGIS generalization distance in degrees (outSR 4326) for perimeter geometry. At ~35N that is
# ~50 m: enough to drop vertex-level noise, small enough that a small fire keeps its shape.
PERIMETER_MAX_ALLOWABLE_OFFSET_DEG = 0.0005
WFIGS_INCIDENTS_LAYER = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0"
WFIGS_PERIMETERS_LAYER = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
CALFIRE_URL = "https://incidents.fire.ca.gov/umbraco/api/IncidentApi/List"
NWS_ZONE_TTL_S = 24 * 3600  # zone polygons change only with NWS zone revisions

Payload = dict[str, Any]


@dataclass(frozen=True)
class FetchInfo:
    url: str
    fetched_at: datetime
    from_cache: bool
    stale: bool

    @classmethod
    def of(cls, url: str, fetched: Fetched) -> FetchInfo:
        return cls(url=url, fetched_at=fetched.fetched_at, from_cache=fetched.from_cache, stale=fetched.stale)


@dataclass
class Batch:
    source: str
    received: int
    payloads: list[Payload]
    skipped: Counter = field(default_factory=Counter)
    fetches: list[FetchInfo] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        return any(f.stale for f in self.fetches)


def _dedupe(payloads: Iterable[Payload], skipped: Counter) -> list[Payload]:
    seen: set[str] = set()
    out: list[Payload] = []
    for p in payloads:
        if p["entity_id"] in seen:
            skipped["duplicate_id"] += 1
            continue
        seen.add(p["entity_id"])
        out.append(p)
    return out


def _in_aoi(aoi: AOI, lat: float | None, lon: float | None) -> bool:
    return lat is not None and lon is not None and bbox_contains(aoi, lat, lon)


# ---------------------------------------------------------------- pure selection


def select_hotspots(records: list[Hotspot], aoi: AOI, now: datetime) -> tuple[list[Payload], Counter]:
    skipped: Counter = Counter()
    payloads = []
    for h in records:
        if not _in_aoi(aoi, h.lat, h.lon):
            skipped["out_of_aoi"] += 1
            continue
        payloads.append(builders.build_hotspot_entity(h, now))
    return _dedupe(payloads, skipped), skipped


def select_wfigs_incidents(
    records: list[WfigsIncident], aoi: AOI, now: datetime
) -> tuple[list[Payload], Counter]:
    skipped: Counter = Counter()
    payloads = []
    for inc in records:
        if not inc.irwin_id:
            skipped["no_irwin_id"] += 1
        elif inc.lat is None or inc.lon is None:
            skipped["no_location"] += 1
        elif not _in_aoi(aoi, inc.lat, inc.lon):
            skipped["out_of_aoi"] += 1
        else:
            payloads.append(builders.build_wfigs_incident_entity(inc, now))
    return _dedupe(payloads, skipped), skipped


def select_calfire_incidents(
    records: list[CalFireIncident], aoi: AOI, now: datetime
) -> tuple[list[Payload], Counter]:
    skipped: Counter = Counter()
    payloads = []
    for inc in records:
        if not inc.is_active:
            skipped["inactive"] += 1
        elif inc.lat is None or inc.lon is None:
            skipped["no_location"] += 1
        elif not _in_aoi(aoi, inc.lat, inc.lon):
            skipped["out_of_aoi"] += 1
        else:
            payloads.append(builders.build_calfire_entity(inc, now))
    return _dedupe(payloads, skipped), skipped


def _in_aoi_geo(payload: Payload, aoi: AOI) -> bool:
    """Bounding-box overlap between a TEMPLATE_GEO payload's exterior ring and the AOI."""
    ring = payload["geo_shape"].polygon.rings[0].positions
    exterior = [(p.position.latitude_degrees, p.position.longitude_degrees) for p in ring]
    return part_intersects_aoi([exterior], aoi)


def _keep_in_aoi(payloads: list[Payload], aoi: AOI, skipped: Counter) -> list[Payload]:
    kept = [p for p in payloads if _in_aoi_geo(p, aoi)]
    if not kept:
        skipped["out_of_aoi"] += 1
    return kept


def select_perimeters(records: list[Perimeter], aoi: AOI, now: datetime) -> tuple[list[Payload], Counter]:
    skipped: Counter = Counter()
    payloads: list[Payload] = []
    for per in records:
        if per.key is None:
            skipped["no_id"] += 1
        elif not ranked_parts(per.parts):
            skipped["no_geometry"] += 1
        else:
            payloads.extend(_keep_in_aoi(builders.build_perimeter_entities(per, now), aoi, skipped))
    return _dedupe(payloads, skipped), skipped


def zone_urls_to_resolve(records: list[NwsAlert], now: datetime) -> list[str]:
    """Zone URLs of live alerts that carry no geometry of their own, in first-seen order."""
    urls: dict[str, None] = {}
    for alert in records:
        end = alert.end_time
        if alert.parts or (end is not None and end <= now):
            continue
        urls.update(dict.fromkeys(alert.affected_zones))
    return list(urls)


def select_red_flags(
    records: list[NwsAlert], aoi: AOI, now: datetime, zones: Mapping[str, NwsZone | None] | None = None
) -> tuple[list[Payload], Counter]:
    """``zones`` maps zone URL -> resolved zone (``None`` = fetch failed). Without it, zone-based alerts
    count as ``no_geometry``. Per-zone failures count as ``zone_fetch_failed`` / ``zone_no_geometry``;
    an alert left with no polygon at all counts once as ``no_geometry``."""
    skipped: Counter = Counter()
    payloads: list[Payload] = []
    for alert in records:
        end = alert.end_time
        if end is not None and end <= now:
            skipped["ended"] += 1
            continue
        if ranked_parts(alert.parts):
            payloads.extend(_keep_in_aoi(builders.build_red_flag_entities(alert, now), aoi, skipped))
            continue
        resolved: list[NwsZone] = []
        for url in alert.affected_zones if zones is not None else ():
            zone = zones.get(url)
            if zone is None:
                skipped["zone_fetch_failed"] += 1
            elif not ranked_parts(zone.parts):
                skipped["zone_no_geometry"] += 1
            else:
                resolved.append(zone)
        if not resolved:
            skipped["no_geometry"] += 1
            continue
        payloads.extend(_keep_in_aoi(builders.build_red_flag_entities(alert, now, resolved), aoi, skipped))
    return _dedupe(payloads, skipped), skipped


def select_stations(records: list[Facility], aoi: AOI, now: datetime) -> tuple[list[Payload], Counter]:
    """The county layer lists some stations twice (e.g. Station 182); the newest record (highest
    OBJECTID) wins so the published position does not depend on server ordering."""
    skipped: Counter = Counter()
    payloads = []
    newest_first = sorted(records, key=lambda f: (f.id, -int((f.attrs or {}).get("OBJECTID") or 0)))
    for fac in newest_first:
        if not _in_aoi(aoi, fac.lat, fac.lon):
            skipped["out_of_aoi"] += 1
            continue
        payloads.append(builders.build_station_entity(fac, now))
    return _dedupe(payloads, skipped), skipped


# ---------------------------------------------------------------- collectors (network)


def collect_firms(aoi: AOI, ttl_s: int) -> Batch:
    now = now_utc()
    records: list[Hotspot] = []
    fetches = []
    for url in FIRMS_URLS.values():
        fetched = fetch_text(url, ttl_s=ttl_s)
        fetches.append(FetchInfo.of(url, fetched))
        records.extend(parse_firms_csv(fetched.data))
    payloads, skipped = select_hotspots(records, aoi, now)
    return Batch("firms", len(records), payloads, skipped, fetches)


def collect_wfigs(aoi: AOI, ttl_s: int) -> Batch:
    now = now_utc()
    fetched = arcgis_query(WFIGS_INCIDENTS_LAYER, geometry_bbox=aoi, ttl_s=ttl_s)
    records = parse_wfigs_incidents(fetched.data)
    payloads, skipped = select_wfigs_incidents(records, aoi, now)
    return Batch("wfigs", len(records), payloads, skipped, [FetchInfo.of(WFIGS_INCIDENTS_LAYER, fetched)])


def collect_perimeters(aoi: AOI, ttl_s: int) -> Batch:
    now = now_utc()
    fetched = arcgis_query(
        WFIGS_PERIMETERS_LAYER,
        geometry_bbox=aoi,
        max_allowable_offset=PERIMETER_MAX_ALLOWABLE_OFFSET_DEG,
        ttl_s=ttl_s,
    )
    records = parse_wfigs_perimeters(fetched.data)
    payloads, skipped = select_perimeters(records, aoi, now)
    return Batch(
        "perimeters", len(records), payloads, skipped, [FetchInfo.of(WFIGS_PERIMETERS_LAYER, fetched)]
    )


def resolve_zones(urls: Iterable[str], fetches: list[FetchInfo]) -> dict[str, NwsZone | None]:
    """Fetch each zone (24 h cache). A failed or malformed zone maps to ``None``; nothing raises."""
    zones: dict[str, NwsZone | None] = {}
    for url in urls:
        try:
            fetched = fetch_json(url, ttl_s=NWS_ZONE_TTL_S)
            zones[url] = parse_nws_zone(fetched.data, url)
        except Exception as exc:  # one bad zone must not drop the alert's other zones
            log.warning("zone %s unavailable: %s: %s", url, type(exc).__name__, exc)
            zones[url] = None
            continue
        fetches.append(FetchInfo.of(url, fetched))
    return zones


def collect_redflag(aoi: AOI, ttl_s: int, now: datetime | None = None) -> Batch:
    now = now or now_utc()
    params = {"event": RED_FLAG_EVENT, "status": "actual"}
    fetched = fetch_json(NWS_ALERTS_URL, params=params, ttl_s=ttl_s)
    fetches = [FetchInfo.of(NWS_ALERTS_URL, fetched)]
    records = parse_nws_alerts(fetched.data, event=RED_FLAG_EVENT)
    zones = resolve_zones(zone_urls_to_resolve(records, now), fetches)
    payloads, skipped = select_red_flags(records, aoi, now, zones)
    return Batch("redflag", len(records), payloads, skipped, fetches)


def collect_calfire(aoi: AOI, ttl_s: int) -> Batch:
    now = now_utc()
    fetched = fetch_json(CALFIRE_URL, params={"inactive": "false"}, ttl_s=ttl_s)
    records = parse_calfire_incidents(fetched.data)
    payloads, skipped = select_calfire_incidents(records, aoi, now)
    return Batch("calfire", len(records), payloads, skipped, [FetchInfo.of(CALFIRE_URL, fetched)])


def collect_stations(aoi: AOI, ttl_s: int) -> Batch:
    now = now_utc()
    records = fire_stations(aoi)
    payloads, skipped = select_stations(records, aoi, now)
    return Batch("stations", len(records), payloads, skipped)


COLLECTORS: dict[str, Callable[[AOI, int], Batch]] = {
    "firms": collect_firms,
    "wfigs": collect_wfigs,
    "perimeters": collect_perimeters,
    "redflag": collect_redflag,
    "calfire": collect_calfire,
    "stations": collect_stations,
}

# Shortest expiry a source publishes; the heartbeat interval must stay below it.
SOURCE_TTL_S = {
    "firms": builders.HOTSPOT_TTL_S,
    "wfigs": builders.INCIDENT_TTL_S,
    "perimeters": builders.PERIMETER_TTL_S,
    "redflag": builders.RED_FLAG_TTL_S,
    "calfire": builders.INCIDENT_TTL_S,
    "stations": builders.STATION_TTL_S,
}
