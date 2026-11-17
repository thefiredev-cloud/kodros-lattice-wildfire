"""Perceivers: independent agents that each read the same Picture and emit Findings.

A perceiver never sees another perceiver's output; the fuser is the only place findings meet.
Each finding names one score component (see fuser.WEIGHTS) or a flag (`associated`,
`persistent_heat`), carries a value in 0..1 and one human-readable evidence line that ends up in
the candidate description.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from typing import Any, Protocol

from lattice_wildfire.core.geo import haversine_m

from .history import HeatHistory
from .picture import Cluster, Picture

# IncidentAssociator: a cluster this close to a known incident point is that incident.
INCIDENT_RADIUS_M = 3000.0
# ExposurePerceiver rings around the cluster centroid.
EXPOSURE_NEAR_M = 5000.0
EXPOSURE_FAR_M = 10000.0
# FRP (MW) at which the frp component saturates; log scale below it.
FRP_SATURATION_MW = 100.0

CONFIDENCE_VALUE = {"low": 0.3, "nominal": 0.6, "high": 1.0}
FHSZ_VALUE = {"very high": 1.0, "high": 0.66, "moderate": 0.33}

ASSOCIATED = "associated"
PERSISTENT_HEAT = "persistent_heat"

# PersistentHeatPerceiver: detections this close to the centroid on this many distinct
# acquisition dates within the prior window mark a fixed heat source (refinery, landfill flare).
PERSISTENT_RADIUS_M = 750.0
PERSISTENT_MIN_DATES = 3
PERSISTENT_WINDOW = timedelta(days=7)
# A persistent site is still held only while it looks like its own history. A fire growing at
# the site escapes the hold when its strongest pixel exceeds this multiple of the site's
# historical max FRP, or its pixels on one acquisition date exceed the site's historical max
# pixels per date by more than this margin.
GROWTH_FRP_RATIO = 2.0
GROWTH_PIXEL_MARGIN = 2


@dataclass(frozen=True)
class Finding:
    perceiver: str
    cluster_id: str
    kind: str  # a fuser.WEIGHTS key, or ASSOCIATED
    value: float  # 0..1
    evidence: str
    data: dict[str, Any] = field(default_factory=dict)


class Perceiver(Protocol):
    name: str

    def observe(self, picture: Picture) -> list[Finding]: ...


class HasLatLon(Protocol):
    name: str
    lat: float
    lon: float


FacilitySource = Sequence[HasLatLon] | Callable[[], Sequence[HasLatLon]]


def _load(source: FacilitySource) -> Sequence[HasLatLon]:
    return tuple(source() if callable(source) else source)


class HotspotClusterer:
    """Intensity of each cluster: detection confidence, fire radiative power, corroboration."""

    name = "hotspot-clusterer"

    def observe(self, picture: Picture) -> list[Finding]:
        findings = []
        for cluster in picture.clusters:
            findings.extend(self._cluster_findings(cluster))
        return findings

    def _cluster_findings(self, cluster: Cluster) -> list[Finding]:
        members = cluster.members
        confidences = [h.confidence for h in members if h.confidence]
        best = max(confidences, key=lambda c: CONFIDENCE_VALUE.get(c, 0.0), default=None)
        frps = [h.frp_mw for h in members if h.frp_mw is not None]
        frp_sum = sum(frps)
        passes = sorted({h.acquired for h in members})
        satellites = sorted({h.satellite for h in members if h.satellite})
        detections = len(members)

        frp_value = min(1.0, math.log10(1.0 + frp_sum) / math.log10(1.0 + FRP_SATURATION_MW)) if frps else 0.0
        corroboration = 0.7 * min(1.0, (len(passes) - 1) / 2) + 0.3 * min(1.0, (detections - 1) / 3)
        span = f"{passes[0]:%Y-%m-%d %H:%MZ}" + (
            f" to {passes[-1]:%Y-%m-%d %H:%MZ}" if len(passes) > 1 else ""
        )
        data = {
            "detections": detections,
            "passes": len(passes),
            "satellites": satellites,
            "frp_sum_mw": round(frp_sum, 2),
            "max_confidence": best,
            "first_seen": passes[0].isoformat(),
            "last_seen": passes[-1].isoformat(),
            "hotspot_ids": [h.entity_id for h in members],
        }
        return [
            Finding(
                self.name,
                cluster.cluster_id,
                "confidence",
                CONFIDENCE_VALUE.get(best or "", 0.0),
                f"max VIIRS confidence: {best or 'unknown'}",
                data,
            ),
            Finding(
                self.name,
                cluster.cluster_id,
                "frp",
                frp_value,
                f"FRP sum: {frp_sum:.2f} MW" if frps else "FRP sum: unknown",
                {},
            ),
            Finding(
                self.name,
                cluster.cluster_id,
                "corroboration",
                corroboration,
                f"{detections} detection(s), {len(passes)} pass(es), satellites "
                f"{', '.join(satellites) or 'unknown'}, {span}",
                {},
            ),
        ]


class IncidentAssociator:
    """Is the cluster an already-known fire? Inside a perimeter, or within 3 km of an incident."""

    name = "incident-associator"

    def observe(self, picture: Picture) -> list[Finding]:
        return [self._associate(picture, cluster) for cluster in picture.clusters]

    def _associate(self, picture: Picture, cluster: Cluster) -> Finding:
        probe_points = (cluster.centroid, *cluster.points)
        for perimeter in picture.perimeters:
            if any(perimeter.contains(lat, lon) for lat, lon in probe_points):
                return Finding(
                    self.name,
                    cluster.cluster_id,
                    ASSOCIATED,
                    1.0,
                    f"inside perimeter {perimeter.name} ({perimeter.entity_id})",
                    {"entity_id": perimeter.entity_id, "via": "perimeter"},
                )
        nearest = None
        for incident in picture.incidents:
            distance = min(haversine_m(lat, lon, incident.lat, incident.lon) for lat, lon in cluster.points)
            if nearest is None or (distance, incident.entity_id) < (nearest[0], nearest[1].entity_id):
                nearest = (distance, incident)
        if nearest is not None and nearest[0] <= INCIDENT_RADIUS_M:
            distance, incident = nearest
            return Finding(
                self.name,
                cluster.cluster_id,
                ASSOCIATED,
                1.0,
                f"{distance / 1000:.1f} km from incident {incident.name} ({incident.entity_id})",
                {"entity_id": incident.entity_id, "via": "incident", "distance_m": round(distance)},
            )
        detail = (
            f"; nearest incident {nearest[1].name} at {nearest[0] / 1000:.1f} km"
            if nearest is not None
            else ""
        )
        return Finding(
            self.name,
            cluster.cluster_id,
            ASSOCIATED,
            0.0,
            f"no known perimeter or incident within {INCIDENT_RADIUS_M / 1000:.0f} km{detail}",
            {
                "nearest_incident": nearest[1].entity_id if nearest else None,
                "nearest_distance_m": round(nearest[0]) if nearest else None,
            },
        )


class RedFlagContext:
    """Is the cluster inside an active NWS Red Flag Warning polygon?"""

    name = "red-flag-context"

    def observe(self, picture: Picture) -> list[Finding]:
        active = [a for a in picture.red_flags if a.expires is None or a.expires > picture.taken_at]
        findings = []
        for cluster in picture.clusters:
            probe_points = (cluster.centroid, *cluster.points)
            hit = next((a for a in active if any(a.contains(lat, lon) for lat, lon in probe_points)), None)
            if hit is None:
                findings.append(
                    Finding(
                        self.name,
                        cluster.cluster_id,
                        "red_flag",
                        0.0,
                        "not inside an active Red Flag Warning",
                        {},
                    )
                )
            else:
                findings.append(
                    Finding(
                        self.name,
                        cluster.cluster_id,
                        "red_flag",
                        1.0,
                        f"inside Red Flag Warning {hit.name} ({hit.entity_id})",
                        {"entity_id": hit.entity_id},
                    )
                )
        return findings


class PersistentHeatPerceiver:
    """Fixed heat sources: the 7-day FIRMS history shows detections at this spot on >= 3 dates.

    A persistent site is held only if it is not growing: current max FRP <= GROWTH_FRP_RATIO x
    the site's historical max FRP, and current pixels per acquisition date <= the site's
    historical max pixels per date + GROWTH_PIXEL_MARGIN. Otherwise it stays a candidate.

    `history` is a HeatHistory or a zero-argument loader (`history.firms_7d_history`). The
    cluster's own detections never count as history. If the history cannot be loaded the
    perceiver says "unknown" for every cluster and suppresses nothing.
    """

    name = "persistent-heat"

    def __init__(self, *, history: HeatHistory | Callable[[], HeatHistory]) -> None:
        self.history = history

    def observe(self, picture: Picture) -> list[Finding]:
        if not picture.clusters:
            return []
        try:
            history = self.history() if callable(self.history) else self.history
        except Exception as error:  # fetch or parse failure: unknown, never a suppression
            return [
                Finding(
                    self.name,
                    c.cluster_id,
                    PERSISTENT_HEAT,
                    0.0,
                    f"persistent heat: unknown (7-day FIRMS history unavailable: {type(error).__name__})",
                    {"status": "unknown", "error": str(error)},
                )
                for c in picture.clusters
            ]
        return [self._judge(cluster, history) for cluster in picture.clusters]

    def _judge(self, cluster: Cluster, history: HeatHistory) -> Finding:
        own = {h.entity_id for h in cluster.members}
        start = cluster.first_seen - PERSISTENT_WINDOW
        lat, lon = cluster.centroid
        past = [
            d
            for d in history.detections
            if d.entity_id not in own
            and start <= d.acquired <= cluster.last_seen
            and haversine_m(lat, lon, d.lat, d.lon) <= PERSISTENT_RADIUS_M
        ]
        dates = sorted({d.acq_date for d in past})
        stale = " (stale cache)" if history.stale else ""
        partial = ""
        if history.partial:
            partial = "; partial history: " + ", ".join(
                f"{f.satellite} unavailable ({f.error})" for f in history.failures
            )
        data: dict[str, Any] = {
            "dates": dates,
            "history_fetched_at": history.fetched_at.isoformat(),
            "stale": history.stale,
            "partial": history.partial,
            "history_sources": list(history.sources),
            "history_failures": {f.satellite: f.error for f in history.failures},
        }
        site = (
            f"prior detections within {PERSISTENT_RADIUS_M:.0f} m on {len(dates)} date(s) in 7 days"
            f"{': ' + ', '.join(dates) if dates else ''}{stale}{partial}"
        )
        if len(dates) < PERSISTENT_MIN_DATES:
            return Finding(
                self.name,
                cluster.cluster_id,
                PERSISTENT_HEAT,
                0.0,
                f"persistent heat: not a fixed heat source; {site}",
                {**data, "status": "clear"},
            )

        hist_frp = max(d.frp_mw for d in past)
        hist_pixels = max(Counter(d.acq_date for d in past).values())
        frps = [h.frp_mw for h in cluster.members if h.frp_mw is not None]
        current_frp = max(frps, default=0.0)
        current_pixels = max(Counter(h.acquired.astimezone(UTC).date() for h in cluster.members).values())
        ratio = current_frp / hist_frp if hist_frp > 0 else math.inf
        data |= {
            "history_max_frp_mw": hist_frp,
            "history_max_pixels_per_date": hist_pixels,
            "current_max_frp_mw": current_frp,
            "current_max_pixels_per_date": current_pixels,
            "frp_ratio": None if math.isinf(ratio) else round(ratio, 2),
        }
        growing = (
            f"persistent site but growing: FRP x{ratio:.1f}, pixels {current_pixels} vs {hist_pixels}"
            f"{partial}"
        )
        if ratio > GROWTH_FRP_RATIO or current_pixels > hist_pixels + GROWTH_PIXEL_MARGIN:
            return Finding(
                self.name, cluster.cluster_id, PERSISTENT_HEAT, 0.0, growing, {**data, "status": "growing"}
            )
        return Finding(
            self.name,
            cluster.cluster_id,
            PERSISTENT_HEAT,
            1.0,
            f"persistent heat: fixed heat source; {site}; FRP x{ratio:.1f} of site max "
            f"{hist_frp:g} MW, pixels {current_pixels} vs {hist_pixels}",
            {**data, "status": "persistent"},
        )


class ExposurePerceiver:
    """What is near the cluster: fire stations and hospitals within 5/10 km, FHSZ class.

    Facility sources are sequences or zero-argument loaders (e.g. `lambda: layers.hospitals(aoi)`)
    so network loads run on this perceiver's own thread; a failed load fails this perceiver
    only. `stations=None` uses the `lacofd-station` entities in the picture. `fhsz_lookup` is
    `core.layers.fhsz_at` in production; a failed lookup is reported, never guessed.
    """

    name = "exposure"

    def __init__(
        self,
        *,
        hospitals: FacilitySource,
        fhsz_lookup: Callable[[float, float], str | None],
        stations: FacilitySource | None = None,
    ) -> None:
        self.hospitals = hospitals
        self.stations = stations
        self.fhsz_lookup = fhsz_lookup

    def observe(self, picture: Picture) -> list[Finding]:
        if not picture.clusters:
            return []
        stations = picture.stations if self.stations is None else _load(self.stations)
        hospitals = _load(self.hospitals)
        findings = []
        for cluster in picture.clusters:
            lat, lon = cluster.centroid
            near_stations = _ring_counts(stations, lat, lon)
            near_hospitals = _ring_counts(hospitals, lat, lon)
            within_near = near_stations["near"] + near_hospitals["near"]
            within_far = near_stations["far"] + near_hospitals["far"]
            proximity = 1.0 if within_near else 0.5 if within_far else 0.0
            findings.append(
                Finding(
                    self.name,
                    cluster.cluster_id,
                    "proximity",
                    proximity,
                    f"stations within 5/10 km: {near_stations['near']}/{near_stations['far']}"
                    f"{_nearest_text(near_stations)}; hospitals within 5/10 km: "
                    f"{near_hospitals['near']}/{near_hospitals['far']}{_nearest_text(near_hospitals)}",
                    {"stations": _public(near_stations), "hospitals": _public(near_hospitals)},
                )
            )
            findings.append(self._fhsz(cluster, lat, lon))
        return findings

    def _fhsz(self, cluster: Cluster, lat: float, lon: float) -> Finding:
        try:
            hazard = self.fhsz_lookup(lat, lon)
        except Exception as error:  # one failed lookup must not drop the other clusters
            return Finding(
                self.name,
                cluster.cluster_id,
                "fhsz",
                0.0,
                f"FHSZ lookup failed: {type(error).__name__}",
                {"error": str(error)},
            )
        value = FHSZ_VALUE.get((hazard or "").strip().lower(), 0.0)
        return Finding(
            self.name,
            cluster.cluster_id,
            "fhsz",
            value,
            f"FHSZ at centroid: {hazard or 'none mapped'}",
            {"fhsz": hazard},
        )


def _ring_counts(facilities: Sequence[HasLatLon], lat: float, lon: float) -> dict[str, Any]:
    near = far = 0
    nearest = None
    for facility in facilities:
        distance = haversine_m(lat, lon, facility.lat, facility.lon)
        if distance <= EXPOSURE_NEAR_M:
            near += 1
        if distance <= EXPOSURE_FAR_M:
            far += 1
        if nearest is None or distance < nearest[0]:
            nearest = (distance, facility.name)
    return {"near": near, "far": far, "nearest": nearest}


def _nearest_text(counts: dict[str, Any]) -> str:
    nearest = counts["nearest"]
    return f" (nearest {nearest[1]}, {nearest[0] / 1000:.1f} km)" if nearest else ""


def _public(counts: dict[str, Any]) -> dict[str, Any]:
    nearest = counts["nearest"]
    return {
        "within_5km": counts["near"],
        "within_10km": counts["far"],
        "nearest": {"name": nearest[1], "distance_m": round(nearest[0])} if nearest else None,
    }


@dataclass(frozen=True)
class PerceptionResult:
    findings: tuple[Finding, ...]
    errors: dict[str, str]  # perceiver name -> error text


def run_perceivers(picture: Picture, perceivers: Sequence[Perceiver]) -> PerceptionResult:
    """Run every perceiver on its own thread over the same snapshot; order results by perceiver."""
    findings: list[Finding] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(perceivers)), thread_name_prefix="perceiver") as pool:
        futures = [(p.name, pool.submit(p.observe, picture)) for p in perceivers]
        for name, future in futures:
            try:
                findings.extend(future.result())
            except Exception as error:
                errors[name] = f"{type(error).__name__}: {error}"
    return PerceptionResult(tuple(findings), errors)
