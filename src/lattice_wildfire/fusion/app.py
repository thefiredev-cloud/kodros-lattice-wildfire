"""Ember Fusion I/O edge: read the Lattice entity stream, perceive, fuse, publish, record."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from anduril import Entity, Lattice
from anduril.core import ApiError

from lattice_wildfire.core import layers
from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import data_type_of

from .fuser import (
    DEFAULT_THRESHOLD,
    STATUS_PERSISTENT,
    Assessment,
    build_candidate_entity,
    build_retirement_entity,
    fuse,
    retirements,
)
from .history import firms_7d_history
from .perceivers import (
    ExposurePerceiver,
    HotspotClusterer,
    IncidentAssociator,
    Perceiver,
    PerceptionResult,
    PersistentHeatPerceiver,
    RedFlagContext,
    run_perceivers,
)
from .picture import (
    CANDIDATE,
    HOTSPOT,
    INCIDENT_TYPES,
    PERIMETER,
    RED_FLAG,
    STATION,
    Picture,
    picture_from_entities,
)

log = logging.getLogger("lattice_wildfire.fusion")

INPUT_TYPES = frozenset({HOTSPOT, *INCIDENT_TYPES, PERIMETER, RED_FLAG, STATION})
# The snapshot also carries our own live candidates, so a cycle can retract the ones that lapsed.
SNAPSHOT_TYPES = INPUT_TYPES | {CANDIDATE}
CANDIDATE_INTEGRATION = "lattice-wildfire-fusion"
# Fireline Feeds writes this after its first full cycle; its presence means "nothing published yet"
# is a real absence of incidents and perimeters, not a startup race.
FEEDS_READY_SUBPATH = Path("feeds") / "ready.json"
SNAPSHOT_TIMEOUT_S = 120


def default_picture_path() -> Path:
    state = Path(os.environ.get("LW_STATE_DIR", Path.home() / ".local/state/lattice-wildfire"))
    return state.expanduser() / "fusion" / "picture.jsonl"


def feeds_ready_marker() -> Path | None:
    """`$LW_STATE_DIR/feeds/ready.json`, or None when LW_STATE_DIR is unset (gate skipped)."""
    state = os.environ.get("LW_STATE_DIR")
    if not state:
        return None
    return Path(state).expanduser() / FEEDS_READY_SUBPATH


def feeds_ready(marker: Path | None = None) -> bool:
    """Whether Fireline Feeds has completed a cycle: the marker exists, or there is nowhere to look."""
    path = feeds_ready_marker() if marker is None else marker
    return path is None or path.is_file()


PerceiverFactory = Callable[[Picture], Sequence[Perceiver]]


def default_perceivers(aoi: AOI | None, picture: Picture) -> list[Perceiver]:
    """Production perceivers. Stations come from Lattice when Fireline Feeds published them."""
    return [
        HotspotClusterer(),
        IncidentAssociator(),
        RedFlagContext(),
        PersistentHeatPerceiver(history=firms_7d_history),
        ExposurePerceiver(
            hospitals=lambda: layers.hospitals(aoi),
            stations=None if picture.stations else (lambda: layers.fire_stations(aoi)),
            fhsz_lookup=layers.fhsz_at,
        ),
    ]


def snapshot_entities(client: Lattice, timeout_s: int = SNAPSHOT_TIMEOUT_S) -> list[Entity]:
    """Every pre-existing input entity, via stream_entities(pre_existing_only=True)."""
    entities: dict[str, Entity] = {}
    events = client.entities.stream_entities(
        pre_existing_only=True, request_options={"timeout_in_seconds": timeout_s}
    )
    for event in events:
        _apply_event(entities, event)
    return list(entities.values())


def _apply_event(entities: dict[str, Entity], event: Any) -> None:
    if event.event != "entity" or event.entity is None or not event.entity.entity_id:
        return
    if data_type_of(event.entity) not in SNAPSHOT_TYPES:
        return
    if event.event_type == "EVENT_TYPE_DELETED":
        entities.pop(event.entity.entity_id, None)
    else:
        entities[event.entity.entity_id] = event.entity


@dataclass
class RunReport:
    run_at: datetime
    threshold: float
    picture: Picture
    perception: PerceptionResult
    assessments: tuple[Assessment, ...]
    published: list[str] = field(default_factory=list)
    publish_errors: dict[str, str] = field(default_factory=dict)
    retired: dict[str, str] = field(default_factory=dict)
    feeds_ready: bool = True
    dry_run: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at.isoformat(),
            "threshold": self.threshold,
            "dry_run": self.dry_run,
            "feeds_ready": self.feeds_ready,
            "counts": self.picture.counts(),
            "skipped": self.picture.skipped,
            "perceiver_errors": self.perception.errors,
            "published": self.published,
            "publish_errors": self.publish_errors,
            "retired": self.retired,
            "assessments": [a.to_json() for a in self.assessments],
        }

    def summary(self) -> str:
        counts = self.picture.counts()
        candidates = sum(a.publish for a in self.assessments)
        associated = sum(a.associated_with is not None for a in self.assessments)
        persistent = sum(a.status == STATUS_PERSISTENT for a in self.assessments)
        retired = f"retired={len(self.retired)}"
        if self.retired:
            retired += " (" + ", ".join(f"{k}: {v}" for k, v in sorted(self.retired.items())) + ")"
        return (
            f"hotspots={counts['hotspots']} clusters={counts['clusters']} associated={associated} "
            f"persistent_heat={persistent} candidates={candidates} published={len(self.published)} "
            f"publish_errors={len(self.publish_errors)} perceiver_errors={len(self.perception.errors)} "
            f"feeds_ready={'true' if self.feeds_ready else 'false'} {retired}"
        )


def assess(
    entities: Iterable[Entity],
    perceivers_for: PerceiverFactory,
    threshold: float = DEFAULT_THRESHOLD,
    now: datetime | None = None,
    *,
    feeds_ready: bool = True,
) -> tuple[Picture, PerceptionResult, tuple[Assessment, ...]]:
    """Snapshot -> parallel perceivers -> deterministic fuser. `perceivers_for(picture)` builds the set."""
    picture = picture_from_entities(entities, now=now)
    perceivers: Sequence[Perceiver] = perceivers_for(picture)
    perception = run_perceivers(picture, perceivers)
    return picture, perception, fuse(picture, perception, threshold, feeds_ready=feeds_ready)


def publish_candidates(
    client: Lattice, assessments: Iterable[Assessment], now: datetime
) -> tuple[list[str], dict[str, str]]:
    published: list[str] = []
    errors: dict[str, str] = {}
    for assessment in assessments:
        if not assessment.publish:
            continue
        entity_id = assessment.cluster.cluster_id
        try:
            client.entities.publish_entity(**build_candidate_entity(assessment, now))
        except ApiError as error:
            errors[entity_id] = f"HTTP {error.status_code}: {error.body}"
            log.error("publish %s failed: HTTP %s", entity_id, error.status_code)
        except httpx.HTTPError as error:
            errors[entity_id] = f"{type(error).__name__}: {error}"
            log.error("publish %s failed: %s", entity_id, type(error).__name__)
        else:
            published.append(entity_id)
    return published, errors


def live_candidates(entities: Iterable[Entity], now: datetime) -> dict[str, Entity]:
    """`ember-candidate` tracks this integration published that are still live in the snapshot."""
    found: dict[str, Entity] = {}
    for entity in entities:
        if not entity.entity_id or data_type_of(entity) != CANDIDATE:
            continue
        prov = entity.provenance
        if prov is None or prov.integration_name != CANDIDATE_INTEGRATION:
            continue
        if entity.is_live is False:
            continue
        if entity.expiry_time is not None and entity.expiry_time <= now:
            continue
        found[entity.entity_id] = entity
    return found


def publish_retirements(
    client: Lattice, candidates: Mapping[str, Entity], retired: Mapping[str, str], now: datetime
) -> dict[str, str]:
    """Republish each retired candidate once with a short expiry. Errors are keyed by entity id."""
    errors: dict[str, str] = {}
    for entity_id in sorted(retired):
        try:
            client.entities.publish_entity(
                **build_retirement_entity(candidates[entity_id], retired[entity_id], now)
            )
        except ApiError as error:
            errors[entity_id] = f"retire: HTTP {error.status_code}: {error.body}"
            log.error("retire %s failed: HTTP %s", entity_id, error.status_code)
        except httpx.HTTPError as error:
            errors[entity_id] = f"retire: {type(error).__name__}: {error}"
            log.error("retire %s failed: %s", entity_id, type(error).__name__)
        else:
            log.info("retired %s: %s", entity_id, retired[entity_id])
    return errors


def append_picture(path: Path, report: RunReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report.to_json(), sort_keys=True) + "\n")


def run_cycle(
    client: Lattice,
    entities: Iterable[Entity],
    *,
    aoi: AOI | None,
    threshold: float = DEFAULT_THRESHOLD,
    picture_path: Path | None = None,
    dry_run: bool = False,
    perceivers_for: PerceiverFactory | None = None,
    ready_marker: Path | None = None,
) -> RunReport:
    now = datetime.now(UTC)
    snapshot = list(entities)
    ready = feeds_ready(ready_marker)
    build = perceivers_for or (lambda picture: default_perceivers(aoi, picture))
    picture, perception, assessments = assess(snapshot, build, threshold, now, feeds_ready=ready)
    report = RunReport(now, threshold, picture, perception, assessments, feeds_ready=ready, dry_run=dry_run)
    candidates = live_candidates(snapshot, now)
    planned = retirements(picture, assessments, candidates, feeds_ready=ready)
    if dry_run:
        report.retired = planned
    else:
        report.published, report.publish_errors = publish_candidates(client, assessments, now)
        retire_errors = publish_retirements(client, candidates, planned, now)
        report.publish_errors.update(retire_errors)
        report.retired = {k: v for k, v in planned.items() if k not in retire_errors}
    if picture_path is not None:
        append_picture(picture_path, report)
    return report


def run_once(client: Lattice, **kwargs: Any) -> RunReport:
    return run_cycle(client, snapshot_entities(client), **kwargs)


class LiveEntityCache:
    """Input entities kept current by a background stream_entities consumer that reconnects."""

    def __init__(self, client: Lattice) -> None:
        self._client = client
        self._entities: dict[str, Entity] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.synced = threading.Event()  # set once the first heartbeat follows the backlog
        self._thread = threading.Thread(target=self._run, name="fusion-stream", daemon=True)

    def start(self) -> LiveEntityCache:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> list[Entity]:
        with self._lock:
            return list(self._entities.values())

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                for event in self._client.entities.stream_entities(heartbeat_interval_ms=10_000):
                    if self._stop.is_set():
                        return
                    if event.event == "heartbeat":
                        self.synced.set()
                        continue
                    with self._lock:
                        _apply_event(self._entities, event)
                    backoff = 1.0
            except Exception as error:  # network drop or server restart: reconnect
                log.warning(
                    "entity stream dropped (%s); reconnecting in %.0fs", type(error).__name__, backoff
                )
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 60.0)


def run_forever(client: Lattice, *, interval_s: float, **kwargs: Any) -> None:
    cache = LiveEntityCache(client).start()
    cache.synced.wait(timeout=SNAPSHOT_TIMEOUT_S)
    try:
        while True:
            started = time.monotonic()
            report = run_cycle(client, cache.snapshot(), **kwargs)
            log.info("fusion cycle: %s", report.summary())
            time.sleep(max(0.0, interval_s - (time.monotonic() - started)))
    finally:
        cache.stop()
