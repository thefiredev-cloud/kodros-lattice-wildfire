"""Fuser: deterministic combination of perceiver findings into scored assessments.

score = sum(WEIGHTS[k] * component_k), each component in 0..1, weights sum to 1.0.
Rationale for every weight lives in docs/apps/fusion.md. Clusters associated with a known
incident or perimeter are scored for the record but never published as candidates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from anduril import Aliases, Entity, MilView, Ontology, Tracked

from lattice_wildfire.core.entities import (
    point_location,
    provenance,
    symbology,
    unclassified,
)

from .perceivers import ASSOCIATED, PERSISTENT_HEAT, PerceptionResult
from .picture import CANDIDATE, Cluster, Picture

PKG = "fusion"
DEFAULT_THRESHOLD = 0.4
# Candidates are republished every run while they qualify; a cluster that stops qualifying
# (perimeter appeared, hotspots expired) lapses within this many seconds.
CANDIDATE_TTL_S = 3600
# A candidate that stopped qualifying is republished once with this expiry instead. Lattice has no
# delete, and both Lattice and the harness reject isLive=false, so a lapse in seconds is the retraction.
RETIRED_TTL_S = 5
RETIRED_PREFIX = "retired:"
RETIRED_GONE = "gone"
TRACK_QUALITY_MAX = 15  # SDK: Tracked.track_quality_wrapper "Quality score, 0-15"

WEIGHTS: dict[str, float] = {
    "confidence": 0.25,  # VIIRS per-pixel confidence: the sensor's own false-alarm screen
    "frp": 0.20,  # fire radiative power: energy release, separates fires from warm ground
    "corroboration": 0.15,  # repeat passes and extra pixels: persistence beats one-off glints
    "red_flag": 0.20,  # NWS Red Flag Warning: weather that turns a start into a run
    "fhsz": 0.12,  # CAL FIRE Fire Hazard Severity Zone at the centroid: fuel and terrain
    "proximity": 0.08,  # stations/hospitals within 5/10 km: people and structures nearby
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "fusion weights must sum to 1.0"

INCIDENT_ASSOCIATOR = "incident-associator"

# Assessment.status values, in the order the fuser checks them.
ASSOCIATION_UNKNOWN = "association-unknown"
STATUS_ASSOCIATED = "associated"
STATUS_PERSISTENT = "persistent-heat"
BELOW_THRESHOLD = "below-threshold"
CANDIDATE_STATUS = "candidate"

# Statuses of a cluster this integration already published: the candidate no longer holds.
RETIRE_STATUSES = frozenset({STATUS_ASSOCIATED, STATUS_PERSISTENT, BELOW_THRESHOLD})
FEEDS_NOT_READY = (
    "feeds not ready: no incident or perimeter in the snapshot and no feeds readiness marker; not published"
)


@dataclass(frozen=True)
class Assessment:
    cluster: Cluster
    score: float
    components: dict[str, float]
    associated_with: str | None
    evidence: tuple[str, ...]
    publish: bool
    reason: str
    status: str

    def to_json(self) -> dict[str, Any]:
        c = self.cluster
        return {
            "cluster_id": c.cluster_id,
            "centroid": {"lat": round(c.centroid[0], 6), "lon": round(c.centroid[1], 6)},
            "first_seen": c.first_seen.isoformat(),
            "last_seen": c.last_seen.isoformat(),
            "hotspot_ids": [h.entity_id for h in c.members],
            "score": self.score,
            "components": self.components,
            "associated_with": self.associated_with,
            "publish": self.publish,
            "status": self.status,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


def association_gated(picture: Picture, feeds_ready: bool = True) -> bool:
    """True when there is nothing to associate against and the feeds readiness marker is absent.

    On the first cycle of a run the feed apps may not have published anything yet. An empty
    incident/perimeter set then means "unknown", never "no known fire here", so nothing is
    published and no candidate is retired (absence is unproven).
    """
    if feeds_ready:
        return False
    return not picture.incidents and not picture.perimeters


def fuse(
    picture: Picture,
    perception: PerceptionResult,
    threshold: float = DEFAULT_THRESHOLD,
    *,
    feeds_ready: bool = True,
) -> tuple[Assessment, ...]:
    by_cluster: dict[str, list] = {c.cluster_id: [] for c in picture.clusters}
    for finding in perception.findings:
        if finding.cluster_id in by_cluster:
            by_cluster[finding.cluster_id].append(finding)
    failures = tuple(f"perceiver {name} failed: {error}" for name, error in sorted(perception.errors.items()))
    gated = association_gated(picture, feeds_ready)

    assessments = []
    for cluster in picture.clusters:
        findings = by_cluster[cluster.cluster_id]
        components = {k: 0.0 for k in WEIGHTS}
        for finding in findings:
            if finding.kind in components:
                components[finding.kind] = max(components[finding.kind], min(1.0, max(0.0, finding.value)))
        score = round(sum(WEIGHTS[k] * v for k, v in components.items()), 4)

        association = [f for f in findings if f.kind == ASSOCIATED]
        associated = next((f for f in association if f.value >= 1.0), None)
        associated_with = associated.data.get("entity_id") if associated else None

        persistent = next((f for f in findings if f.kind == PERSISTENT_HEAT and f.value >= 1.0), None)

        if gated:
            status, reason = ASSOCIATION_UNKNOWN, FEEDS_NOT_READY
        elif INCIDENT_ASSOCIATOR in perception.errors or not association:
            status, reason = (
                ASSOCIATION_UNKNOWN,
                "association unknown (incident associator failed); not published",
            )
        elif associated is not None:
            status, reason = (
                STATUS_ASSOCIATED,
                f"associated with {associated_with}; known fire, not a new start",
            )
        elif persistent is not None:
            dates = persistent.data.get("dates", [])
            missing = persistent.data.get("history_failures") or {}
            gap = (
                f"; partial 7-day history: {', '.join(sorted(missing))} unavailable"
                if missing and persistent.data.get("partial")
                else ""
            )
            status, reason = (
                STATUS_PERSISTENT,
                (
                    f"persistent heat source: prior detections on {len(dates)} dates in 7 days "
                    f"({', '.join(dates)}){gap}; not a new start"
                ),
            )
        elif score < threshold:
            status, reason = BELOW_THRESHOLD, f"score {score:.2f} below threshold {threshold:.2f}"
        else:
            status, reason = CANDIDATE_STATUS, f"unassociated, score {score:.2f} >= threshold {threshold:.2f}"
        publish = status == CANDIDATE_STATUS

        evidence = (
            f"score: {score:.2f} (threshold {threshold:.2f})",
            *(
                ("feeds readiness: marker absent and snapshot has no incident or perimeter entity",)
                if gated
                else ()
            ),
            *(f.evidence for f in findings),
            *failures,
        )
        assessments.append(
            Assessment(
                cluster=cluster,
                score=score,
                components={k: round(v, 4) for k, v in components.items()},
                associated_with=associated_with,
                evidence=evidence,
                publish=publish,
                reason=reason,
                status=status,
            )
        )
    return tuple(assessments)


def track_quality(score: float) -> int:
    return max(0, min(TRACK_QUALITY_MAX, round(score * TRACK_QUALITY_MAX)))


def build_candidate_entity(assessment: Assessment, now: datetime) -> dict[str, Any]:
    """`publish_entity` keyword arguments for one `ember-candidate` track."""
    cluster = assessment.cluster
    lat, lon = cluster.centroid
    suffix = cluster.cluster_id.removeprefix("lw-cand-")
    return {
        "entity_id": cluster.cluster_id,
        "description": "\n".join(assessment.evidence),
        "is_live": True,
        "created_time": cluster.first_seen,
        "expiry_time": now + timedelta(seconds=CANDIDATE_TTL_S),
        "location": point_location(lat, lon),
        "aliases": Aliases(name=f"New start candidate {suffix}"),
        "mil_view": MilView(disposition="DISPOSITION_SUSPICIOUS", environment="ENVIRONMENT_LAND"),
        "ontology": Ontology(template="TEMPLATE_TRACK", platform_type="New Start Candidate"),
        "tracked": Tracked(
            track_quality_wrapper=track_quality(assessment.score),
            sensor_hits=len(cluster.members),
            last_measurement_time=cluster.last_seen,
        ),
        # source_update_time must advance on every republish or Lattice drops the update.
        "provenance": provenance(PKG, CANDIDATE, source_time=now),
        "data_classification": unclassified(),
        "symbology": symbology("DISPOSITION_SUSPICIOUS", "ENVIRONMENT_LAND"),
    }


def retirements(
    picture: Picture,
    assessments: Sequence[Assessment],
    candidates: Mapping[str, Entity],
    *,
    feeds_ready: bool = True,
) -> dict[str, str]:
    """Earlier published candidates this cycle retracts: ``{entity_id: reason}``.

    A cluster that is now `associated`, `persistent-heat` or `below-threshold` no longer holds as a
    new start, and a cluster gone from the picture (hotspots expired, merged into an earlier anchor
    or withdrawn) has nothing left to track. A cluster still `candidate` is republished by the normal
    path. Nothing is retired while `association_gated` is true: an empty snapshot proves nothing.
    """
    if association_gated(picture, feeds_ready):
        return {}
    by_id = {a.cluster.cluster_id: a for a in assessments}
    retired: dict[str, str] = {}
    for entity_id in sorted(candidates):
        assessment = by_id.get(entity_id)
        if assessment is None:
            retired[entity_id] = RETIRED_GONE
        elif assessment.status in RETIRE_STATUSES:
            retired[entity_id] = assessment.status
    return retired


def build_retirement_entity(entity: Entity, reason: str, now: datetime) -> dict[str, Any]:
    """`publish_entity` keyword arguments retracting one earlier `ember-candidate` track.

    Same ``entity_id``, the original description plus one ``retired: {reason}`` line, and a
    ``RETIRED_TTL_S`` expiry: Lattice has no delete, so a short expiry is the retraction.
    """
    kept = [line for line in (entity.description or "").splitlines() if not line.startswith(RETIRED_PREFIX)]
    kwargs: dict[str, Any] = {
        "entity_id": entity.entity_id,
        "description": "\n".join([*kept, f"{RETIRED_PREFIX} {reason}"]),
        "is_live": True,
        "created_time": entity.created_time or now,
        "expiry_time": now + timedelta(seconds=RETIRED_TTL_S),
        # source_update_time must advance on the retirement too or Lattice drops it.
        "provenance": provenance(PKG, CANDIDATE, source_time=now),
    }
    for field in (
        "location",
        "geo_shape",
        "geo_details",
        "aliases",
        "tracked",
        "mil_view",
        "ontology",
        "symbology",
        "data_classification",
    ):
        value = getattr(entity, field)
        if value is not None:
            kwargs[field] = value
    return kwargs
