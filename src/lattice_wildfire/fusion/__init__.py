"""App 2 Ember Fusion: parallel perceivers over a Lattice snapshot, deterministic fuser,
`ember-candidate` new-start tracks published back to Lattice."""

from .fuser import (
    DEFAULT_THRESHOLD,
    WEIGHTS,
    Assessment,
    build_candidate_entity,
    build_retirement_entity,
    fuse,
    retirements,
    track_quality,
)
from .history import HeatHistory, SourceFailure, firms_7d_history, history_from_csv, merged_history
from .perceivers import (
    ExposurePerceiver,
    Finding,
    HotspotClusterer,
    IncidentAssociator,
    PerceptionResult,
    PersistentHeatPerceiver,
    RedFlagContext,
    run_perceivers,
)
from .picture import Cluster, Picture, candidate_id, cluster_hotspots, picture_from_entities

__all__ = [
    "DEFAULT_THRESHOLD",
    "WEIGHTS",
    "Assessment",
    "Cluster",
    "ExposurePerceiver",
    "Finding",
    "HeatHistory",
    "HotspotClusterer",
    "IncidentAssociator",
    "PerceptionResult",
    "PersistentHeatPerceiver",
    "Picture",
    "RedFlagContext",
    "SourceFailure",
    "build_candidate_entity",
    "build_retirement_entity",
    "candidate_id",
    "cluster_hotspots",
    "firms_7d_history",
    "fuse",
    "history_from_csv",
    "merged_history",
    "picture_from_entities",
    "retirements",
    "run_perceivers",
    "track_quality",
]
