"""Build the planner `Snapshot` from Lattice.

`build_snapshot` is pure (SDK models in, planner model out). `fetch_entities` and
`fetch_open_tasks` are the only network calls.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from anduril import Entity, Lattice, Task
from anduril.tasks.types.task_query_status_filter import TaskQueryStatusFilter

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import data_type_of, lat_lon_of
from lattice_wildfire.core.geo import bbox_contains

from .model import (
    CANDIDATE_DATA_TYPE,
    INVESTIGATE_URL,
    OPEN_STATUSES,
    RED_FLAG_DATA_TYPE,
    STATION_DATA_TYPE,
    Asset,
    Candidate,
    OpenTask,
    Site,
    Snapshot,
    Zone,
    is_retired,
    is_terminal,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- pure


def _name(entity: Entity) -> str:
    return (entity.aliases.name if entity.aliases and entity.aliases.name else None) or entity.entity_id


# An asset entity advertises how many Investigate tasks it can run at once as
# `capacity=N` in its description; the Lattice entity model has no structured field for
# it (Entity/TaskCatalog/TaskDefinition carry no capacity in SDK 5.4.0).
CAPACITY_PATTERN = re.compile(r"\bcapacity\s*[=:]\s*(\d+)\b", re.IGNORECASE)


def capacity_from_description(description: str | None) -> int | None:
    """Declared capacity from an asset description, or None when it declares none."""
    match = CAPACITY_PATTERN.search(description or "")
    if match is None:
        return None
    value = int(match.group(1))
    return value if value >= 1 else None


def _is_current(entity: Entity, now: datetime) -> bool:
    if entity.is_live is False:
        return False
    return entity.expiry_time is None or entity.expiry_time > now


def candidate_from_entity(entity: Entity) -> Candidate | None:
    pos = lat_lon_of(entity)
    if pos is None:
        return None
    twq = entity.tracked.track_quality_wrapper if entity.tracked else None
    score = None if twq is None else max(0.0, min(1.0, twq / 15.0))
    observed = (entity.provenance.source_update_time if entity.provenance else None) or entity.created_time
    return Candidate(
        entity_id=entity.entity_id,
        name=_name(entity),
        lat=pos[0],
        lon=pos[1],
        score=score,
        observed_at=observed,
        description=entity.description or "",
    )


def asset_from_entity(entity: Entity) -> Asset:
    pos = lat_lon_of(entity)
    defs = (entity.task_catalog.task_definitions or []) if entity.task_catalog else []
    health = entity.health
    return Asset(
        entity_id=entity.entity_id,
        name=_name(entity),
        lat=pos[0] if pos else None,
        lon=pos[1] if pos else None,
        task_types=frozenset(d.task_specification_url for d in defs if d.task_specification_url),
        connection_status=health.connection_status if health else None,
        health_status=health.health_status if health else None,
        capacity=capacity_from_description(entity.description),
    )


def zone_from_entity(entity: Entity) -> Zone | None:
    shape = entity.geo_shape
    if not shape or not shape.polygon or not shape.polygon.rings:
        return None
    ring = []
    for p in shape.polygon.rings[0].positions or []:
        pos = p.position
        if pos is not None and pos.latitude_degrees is not None and pos.longitude_degrees is not None:
            ring.append((pos.latitude_degrees, pos.longitude_degrees))
    return Zone(entity.entity_id, _name(entity), tuple(ring)) if len(ring) >= 3 else None


def objective_entity_id(task: Task) -> str | None:
    """Target entity of an Investigate task (`objective.entity_id`, either JSON spelling)."""
    spec = task.specification
    if spec is None:
        return None
    objective: Any = getattr(spec, "objective", None)
    if objective is None and spec.model_extra:
        objective = spec.model_extra.get("objective")
    if isinstance(objective, dict):
        return objective.get("entity_id") or objective.get("entityId")
    return None


def assignee_id(task: Task) -> str | None:
    rel = task.relations
    if rel and rel.assignee and rel.assignee.system:
        return rel.assignee.system.entity_id
    return None


def open_task_from_task(task: Task) -> OpenTask | None:
    status = task.status.status if task.status else None
    if task.specification is None or task.specification.type != INVESTIGATE_URL or is_terminal(status):
        return None
    return OpenTask(
        task_id=task.version.task_id,
        candidate_id=objective_entity_id(task),
        assignee_id=assignee_id(task),
        status=status or "STATUS_INVALID",
    )


def build_snapshot(
    entities: Iterable[Entity],
    tasks: Iterable[Task],
    now: datetime,
    aoi: AOI | None = None,
) -> Snapshot:
    candidates, assets, red_flags, stations = [], [], [], []
    raw: dict[str, Entity] = {}
    for e in entities:
        if not e.entity_id or not _is_current(e, now):
            continue
        raw[e.entity_id] = e
        dt = data_type_of(e)
        template = e.ontology.template if e.ontology else None
        if dt == CANDIDATE_DATA_TYPE:
            c = candidate_from_entity(e)
            if c and not is_retired(e.description) and (aoi is None or bbox_contains(aoi, c.lat, c.lon)):
                candidates.append(c)
        elif dt == RED_FLAG_DATA_TYPE:
            z = zone_from_entity(e)
            if z:
                red_flags.append(z)
        if template == "TEMPLATE_ASSET":
            assets.append(asset_from_entity(e))
        if dt == STATION_DATA_TYPE:
            pos = lat_lon_of(e)
            if pos:
                stations.append(Site(e.entity_id, _name(e), pos[0], pos[1]))
    open_tasks = [ot for t in tasks if (ot := open_task_from_task(t)) is not None]
    return Snapshot(
        now=now,
        candidates=tuple(sorted(candidates, key=lambda c: c.entity_id)),
        assets=tuple(sorted(assets, key=lambda a: a.entity_id)),
        red_flags=tuple(red_flags),
        stations=tuple(stations),
        open_tasks=tuple(open_tasks),
        entities=raw,
    )


# --------------------------------------------------------------------------- I/O


def fetch_entities(client: Lattice) -> list[Entity]:
    """All live entities, via one pre-existing-only entity stream (closes when done)."""
    out = []
    for event in client.entities.stream_entities(pre_existing_only=True):
        if getattr(event, "event", None) == "entity" and event.entity is not None:
            if event.event_type == "EVENT_TYPE_DELETED":
                continue
            out.append(event.entity)
    return out


def fetch_open_tasks(client: Lattice) -> list[Task]:
    """Every task in a non-terminal state (one paginated query per status)."""
    tasks: dict[str, Task] = {}
    for status in OPEN_STATUSES:
        token = None
        while True:
            kwargs: dict[str, Any] = {"status_filter": TaskQueryStatusFilter(status=status)}
            if token:
                kwargs["page_token"] = token
            page = client.tasks.query_tasks(**kwargs)
            for t in page.tasks or []:
                if t.version and t.version.task_id:
                    tasks[t.version.task_id] = t
            token = page.next_page_token
            if not token:
                break
    return list(tasks.values())
