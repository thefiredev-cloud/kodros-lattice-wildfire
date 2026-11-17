"""In-memory Lattice state: entities + event log, tasks + agent delivery queues, objects.

Everything is guarded by one condition variable so long polls and streams can wait for changes.
All payloads are REST JSON (camelCase), exactly as the SDK sends and parses them.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .timeutil import format_time, now, parse_time

MAX_ENTITY_EXPIRY = timedelta(days=30)
DEFAULT_OBJECT_TTL = timedelta(days=90)
MAX_OBJECT_BYTES = 1 << 30
MAX_EVENTS = 100_000
MAX_TASK_EVENTS = 100_000
TASK_PAGE_SIZE = 100
OBJECT_PAGE_SIZE = 100
TERMINAL = frozenset({"STATUS_DONE_OK", "STATUS_DONE_NOT_OK"})
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]{5,36}$")
_OBJECT_PATH_RE = re.compile(r"^[a-zA-Z0-9/_=\-.]+$")
_FIELD_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MISSING = object()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message: str, code: str = "VALIDATION_ERROR") -> ApiError:
    return ApiError(400, code, message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def camel(name: str) -> str:
    """``mil_view`` -> ``milView``: the REST JSON spelling of a protobuf field name."""
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _field_segments(field_path: str) -> list[str]:
    """Validate an override field path (lower_snake_case, dotted) and return its REST-JSON segments."""
    segments = field_path.split(".")
    if not field_path or any(not _FIELD_SEGMENT_RE.match(part) for part in segments):
        raise bad_request(
            "fieldPath must be a lower_snake_case dotted path rooted at the entity, e.g. 'aliases.name'"
        )
    return [camel(part) for part in segments]


def _path_get(root: Any, segments: list[str]) -> Any:
    cur = root
    for seg in segments:
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


def _path_set(root: dict, segments: list[str], value: Any) -> None:
    cur = root
    for seg in segments[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segments[-1]] = value


def _path_del(root: dict, segments: list[str]) -> None:
    cur: Any = root
    for seg in segments[:-1]:
        cur = cur.get(seg) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return
    cur.pop(segments[-1], None)


def _nested(segments: list[str], value: Any) -> dict:
    out: dict = {}
    cur = out
    for seg in segments[:-1]:
        cur[seg] = {}
        cur = cur[seg]
    cur[segments[-1]] = value
    return out


def entity_floor_error(entity: dict, at: datetime) -> str | None:
    """Lattice's documented server-side minimum for a published entity (entity-fields.md)."""
    if not entity.get("entityId"):
        return "entityId must be set"
    if entity.get("isLive") is not True:
        return "isLive must be set to true"
    if not entity.get("noExpiry"):
        raw = entity.get("expiryTime")
        if not raw:
            return "expiryTime must be set"
        try:
            exp = parse_time(raw)
        except ValueError:
            return "expiryTime must be an RFC 3339 timestamp with an offset"
        if exp <= at:
            return "expiryTime must be in the future"
        if exp - at >= MAX_ENTITY_EXPIRY:
            return "expiryTime must be less than 30 days in the future"
    if not _get(entity, "provenance", "integrationName"):
        return "provenance.integrationName must be set"
    if not _get(entity, "provenance", "dataType"):
        return "provenance.dataType must be set"
    sut = _get(entity, "provenance", "sourceUpdateTime")
    if sut:
        try:
            parse_time(sut)
        except ValueError:
            return "provenance.sourceUpdateTime must be an RFC 3339 timestamp with an offset"
    if not _get(entity, "aliases", "name"):
        return "aliases.name must be set"
    template = _get(entity, "ontology", "template")
    if template in ("TEMPLATE_TRACK", "TEMPLATE_ASSET"):
        pos = _get(entity, "location", "position") or {}
        if "latitudeDegrees" not in pos or "longitudeDegrees" not in pos:
            return f"location.position latitudeDegrees and longitudeDegrees must be set for {template}"
        if not _get(entity, "milView", "disposition") or not _get(entity, "milView", "environment"):
            return f"milView.disposition and milView.environment must be set for {template}"
    elif template == "TEMPLATE_GEO":
        if not entity.get("geoDetails"):
            return "geoDetails must be set for TEMPLATE_GEO"
        shape = entity.get("geoShape")
        if not shape:
            return "geoShape must be set for TEMPLATE_GEO"
        if ("ellipse" in shape or "ellipsoid" in shape) and not _get(entity, "location", "position"):
            return "location must be set for a TEMPLATE_GEO ellipse"
    return None


@dataclass
class StoredObject:
    data: bytes
    checksum: str
    last_updated: datetime
    expiry: datetime | None

    def metadata(self, path: str) -> dict:
        out = {
            "content_identifier": {"path": path, "checksum": self.checksum},
            "size_bytes": len(self.data),
            "last_updated_at": format_time(self.last_updated),
        }
        if self.expiry is not None:
            out["expiry_time"] = format_time(self.expiry)
        return out


@dataclass
class _TaskMeta:
    assignee_id: str | None
    delivered: bool = False
    last_update_version: int = 0


@dataclass
class _EntityOverride:
    """The value an override replaced, so removing the override can restore it."""

    original: Any


class Store:
    def __init__(self, long_poll_timeout_s: float = 300.0) -> None:
        self.cond = threading.Condition()
        self.long_poll_timeout_s = long_poll_timeout_s
        self.closed = False
        self.entities: dict[str, dict] = {}
        self._events: list[dict] = []
        self._event_base = 0
        self._sessions: dict[str, dict] = {}
        self._overrides: dict[str, dict[str, _EntityOverride]] = {}
        self.tasks: dict[str, dict] = {}
        self._task_meta: dict[str, _TaskMeta] = {}
        self._task_events: list[dict] = []
        self._task_event_base = 0
        self._agent_queues: dict[str, deque] = {}
        self.objects: dict[str, StoredObject] = {}

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def sweep(self) -> None:
        """Expire entities and objects whose expiry passed."""
        with self.cond:
            self._expire_entities(now())
            t = now()
            for path in [p for p, o in self.objects.items() if o.expiry is not None and o.expiry <= t]:
                del self.objects[path]

    # ------------------------------------------------------------------ entities
    def _append_event(self, event_type: str, entity: dict, at: datetime) -> None:
        self._events.append(
            {"eventType": event_type, "time": format_time(at), "entity": copy.deepcopy(entity)}
        )
        if len(self._events) > MAX_EVENTS:
            drop = len(self._events) - MAX_EVENTS
            del self._events[:drop]
            self._event_base += drop
        self.cond.notify_all()

    def _expire_entities(self, at: datetime) -> None:
        expired = [
            eid
            for eid, ent in self.entities.items()
            if not ent.get("noExpiry") and parse_time(ent["expiryTime"]) <= at
        ]
        for eid in expired:
            ent = self.entities.pop(eid)
            self._overrides.pop(eid, None)
            self._append_event("EVENT_TYPE_DELETED", ent, at)

    def publish_entity(self, entity: dict) -> dict:
        at = now()
        problem = entity_floor_error(entity, at)
        if problem:
            raise bad_request(problem)
        with self.cond:
            self._expire_entities(at)
            eid = entity["entityId"]
            existing = self.entities.get(eid)
            if existing is not None:
                old = _get(existing, "provenance", "sourceUpdateTime")
                new = _get(entity, "provenance", "sourceUpdateTime")
                if old and new and parse_time(new) <= parse_time(old):
                    # Lattice only applies an update with a newer provenance.sourceUpdateTime.
                    return copy.deepcopy(existing)
            stored = copy.deepcopy(entity)
            stored.setdefault("createdTime", existing.get("createdTime") if existing else format_time(at))
            self.entities[eid] = stored
            # A source update is a new full document: the harness does not carry overrides onto it.
            self._overrides.pop(eid, None)
            self._append_event("EVENT_TYPE_UPDATE" if existing else "EVENT_TYPE_CREATED", stored, at)
            return copy.deepcopy(stored)

    # ------------------------------------------------------------------ entity overrides
    def override_entity(self, entity_id: str, field_path: str, body: dict) -> dict:
        """Apply one field override (last writer wins per field path) and return the entity."""
        segments = _field_segments(field_path)
        source = body.get("entity")
        if not isinstance(source, dict) or not source:
            raise bad_request("entity must be set on an EntityOverride request")
        value = _path_get(source, segments)
        if value is _MISSING or value is None:
            raise bad_request(
                f"entity has no value at fieldPath {field_path}; send only the field being overridden"
            )
        at = now()
        with self.cond:
            self._expire_entities(at)
            entity = self.entities.get(entity_id)
            if entity is None:
                raise not_found(f"entity {entity_id} not found")
            self._overrides.setdefault(entity_id, {}).setdefault(
                field_path, _EntityOverride(original=_path_get(entity, segments))
            )
            _path_set(entity, segments, copy.deepcopy(value))
            record: dict[str, Any] = {
                "requestId": uuid.uuid4().hex,
                "fieldPath": field_path,
                "maskedFieldValue": _nested(segments, copy.deepcopy(value)),
                "status": "OVERRIDE_STATUS_APPLIED",
                "type": "OVERRIDE_TYPE_LIVE",
                "requestTimestamp": format_time(at),
            }
            provenance = body.get("provenance")
            if isinstance(provenance, dict) and provenance:
                record["provenance"] = provenance
            overrides = entity.get("overrides")
            if not isinstance(overrides, dict):
                overrides = {}
                entity["overrides"] = overrides
            rows = [o for o in (overrides.get("override") or []) if _get(o, "fieldPath") != field_path]
            overrides["override"] = [*rows, record]
            self._append_event("EVENT_TYPE_UPDATE", entity, at)
            return copy.deepcopy(entity)

    def remove_entity_override(self, entity_id: str, field_path: str) -> dict:
        """Clear one field override, restoring the value the source last published, and return the entity."""
        segments = _field_segments(field_path)
        at = now()
        with self.cond:
            self._expire_entities(at)
            entity = self.entities.get(entity_id)
            if entity is None:
                raise not_found(f"entity {entity_id} not found")
            mine = self._overrides.get(entity_id) or {}
            saved = mine.pop(field_path, None)
            if saved is None:
                raise not_found(f"entity {entity_id} has no override at {field_path}")
            if saved.original is _MISSING:
                _path_del(entity, segments)
            else:
                _path_set(entity, segments, copy.deepcopy(saved.original))
            overrides = entity.get("overrides")
            if isinstance(overrides, dict):
                rows = [o for o in (overrides.get("override") or []) if _get(o, "fieldPath") != field_path]
                if rows:
                    overrides["override"] = rows
                else:
                    entity.pop("overrides", None)
            self._append_event("EVENT_TYPE_UPDATE", entity, at)
            return copy.deepcopy(entity)

    def get_entity(self, entity_id: str) -> dict:
        with self.cond:
            self._expire_entities(now())
            ent = self.entities.get(entity_id)
            if ent is None:
                raise not_found(f"entity {entity_id} not found")
            return copy.deepcopy(ent)

    def entity_snapshot(self) -> tuple[list[dict], int]:
        """PREEXISTING events for every live entity plus the log cursor that follows them."""
        with self.cond:
            at = now()
            self._expire_entities(at)
            pre = [
                {"eventType": "EVENT_TYPE_PREEXISTING", "time": format_time(at), "entity": copy.deepcopy(e)}
                for _, e in sorted(self.entities.items())
            ]
            return pre, self._event_base + len(self._events)

    def wait_entity_events(self, cursor: int, timeout: float) -> tuple[list[dict], int]:
        """Events after ``cursor`` (waiting up to ``timeout``) and the new cursor. Raises if the log was trimmed."""
        with self.cond:
            end = self._event_base + len(self._events)
            if cursor >= end and timeout > 0 and not self.closed:
                self.cond.wait(timeout)
                end = self._event_base + len(self._events)
            if cursor < self._event_base:
                raise ApiError(404, "NOT_FOUND", "stream fell too far behind the entity event log")
            return copy.deepcopy(self._events[cursor - self._event_base :]), end

    def long_poll(self, session_token: str, batch_size: int | None) -> dict:
        size = batch_size or 100
        if not 1 <= size <= 2000:
            raise bad_request("batchSize must be between 1 and 2000")
        with self.cond:
            if not session_token:
                pre, cursor = self.entity_snapshot()
                session_token = uuid.uuid4().hex
                self._sessions[session_token] = {"pending": deque(pre), "cursor": cursor}
            session = self._sessions.get(session_token)
            if session is None:
                raise not_found("unknown sessionToken; start a new session with an empty sessionToken")
            deadline = now() + timedelta(seconds=self.long_poll_timeout_s)
            while True:
                if session["cursor"] < self._event_base:
                    del self._sessions[session_token]
                    raise not_found("session fell behind the entity event log; start a new session")
                batch: list[dict] = []
                while session["pending"] and len(batch) < size:
                    batch.append(session["pending"].popleft())
                start = session["cursor"] - self._event_base
                take = self._events[start : start + (size - len(batch))]
                batch.extend(copy.deepcopy(take))
                session["cursor"] += len(take)
                remaining = (deadline - now()).total_seconds()
                if batch or remaining <= 0 or self.closed:
                    return {"sessionToken": session_token, "entityEvents": batch}
                self.cond.wait(remaining)

    # ------------------------------------------------------------------ tasks
    def _set_status(self, task: dict, status: dict, at: datetime) -> None:
        task["status"] = status
        task["version"]["statusVersion"] = task["version"].get("statusVersion", 0) + 1
        task["lastUpdateTime"] = format_time(at)

    # ------------------------------------------------------------------ tasks
    def _append_task_event(self, event_type: str, task: dict) -> None:
        self._task_events.append({"eventType": event_type, "task": copy.deepcopy(task)})
        if len(self._task_events) > MAX_TASK_EVENTS:
            drop = len(self._task_events) - MAX_TASK_EVENTS
            del self._task_events[:drop]
            self._task_event_base += drop
        self.cond.notify_all()

    def task_snapshot(self) -> tuple[list[dict], int]:
        """PREEXISTING events for every non-terminal task plus the log cursor that follows them."""
        with self.cond:
            pre = [
                {"eventType": "EVENT_TYPE_PREEXISTING", "task": copy.deepcopy(task)}
                for task in sorted(
                    self.tasks.values(), key=lambda t: (t["createTime"], t["version"]["taskId"])
                )
                if task["status"].get("status") not in TERMINAL
            ]
            return pre, self._task_event_base + len(self._task_events)

    def task_cursor(self) -> int:
        """The log cursor after every task event so far (for a stream that skips preexisting tasks)."""
        with self.cond:
            return self._task_event_base + len(self._task_events)

    def wait_task_events(self, cursor: int, timeout: float) -> tuple[list[dict], int]:
        """Task events after ``cursor`` (waiting up to ``timeout``) and the new cursor."""
        with self.cond:
            end = self._task_event_base + len(self._task_events)
            if cursor >= end and timeout > 0 and not self.closed:
                self.cond.wait(timeout)
                end = self._task_event_base + len(self._task_events)
            if cursor < self._task_event_base:
                raise ApiError(404, "NOT_FOUND", "stream fell too far behind the task event log")
            return copy.deepcopy(self._task_events[cursor - self._task_event_base :]), end

    def create_task(self, body: dict) -> dict:
        task_id = body.get("taskId") or uuid.uuid4().hex
        if not _TASK_ID_RE.match(task_id):
            raise bad_request("taskId must match [A-Za-z0-9_-.]{5,36}")
        if not _get(body, "specification", "@type"):
            raise bad_request("specification.@type must be set")
        at = now()
        stamp = format_time(at)
        task: dict[str, Any] = {
            "version": {"taskId": task_id, "definitionVersion": 1, "statusVersion": 1},
            "specification": body["specification"],
            "status": {"status": "STATUS_CREATED", "lastUpdateTime": stamp},
            "createTime": stamp,
            "lastUpdateTime": stamp,
            "isExecutedElsewhere": bool(body.get("isExecutedElsewhere", False)),
        }
        for src, dst in (
            ("displayName", "displayName"),
            ("description", "description"),
            ("relations", "relations"),
            ("initialEntities", "initialEntities"),
            ("retryStrategy", "retryStrategy"),
            ("executionConstraints", "executionConstraints"),
        ):
            if src in body:
                task[dst] = body[src]
        if "author" in body:
            task["createdBy"] = body["author"]
            task["lastUpdatedBy"] = body["author"]
        assignee_id = _get(body, "relations", "assignee", "system", "entityId")
        with self.cond:
            if task_id in self.tasks:
                raise bad_request(f"task {task_id} already exists", code="ALREADY_EXISTS")
            self.tasks[task_id] = task
            self._task_meta[task_id] = _TaskMeta(assignee_id=assignee_id)
            self._append_task_event("EVENT_TYPE_CREATED", task)
            if assignee_id and not task["isExecutedElsewhere"]:
                self._agent_queues.setdefault(assignee_id, deque()).append(("execute", task_id))
                self.cond.notify_all()
            return copy.deepcopy(task)

    def get_task(self, task_id: str) -> dict:
        with self.cond:
            task = self.tasks.get(task_id)
            if task is None:
                raise not_found(f"task {task_id} not found")
            return copy.deepcopy(task)

    def update_task_status(self, task_id: str, body: dict) -> dict:
        version = body.get("statusVersion")
        new_status = body.get("newStatus") or {}
        if not isinstance(version, int) or version <= 0:
            raise bad_request("statusVersion must be a positive integer")
        if not new_status.get("status"):
            raise bad_request("newStatus.status must be set")
        with self.cond:
            task = self.tasks.get(task_id)
            if task is None:
                raise not_found(f"task {task_id} not found")
            meta = self._task_meta[task_id]
            current = task["status"].get("status")
            if current in TERMINAL:
                raise bad_request(
                    f"task {task_id} is in terminal state {current}", code="FAILED_PRECONDITION"
                )
            if version <= meta.last_update_version:
                raise bad_request(
                    f"statusVersion {version} is stale: last accepted status update was version "
                    f"{meta.last_update_version}",
                    code="FAILED_PRECONDITION",
                )
            at = now()
            status = copy.deepcopy(new_status)
            status["lastUpdateTime"] = format_time(at)
            prior_start = task["status"].get("startTime")
            if prior_start and "startTime" not in status:
                status["startTime"] = prior_start
            if status["status"] == "STATUS_EXECUTING" and "startTime" not in status:
                status["startTime"] = format_time(at)
            if status["status"] in TERMINAL and "endTime" not in status:
                status["endTime"] = format_time(at)
            meta.last_update_version = version
            prev_version = task["version"]["statusVersion"]
            self._set_status(task, status, at)
            task["version"]["statusVersion"] = max(prev_version + 1, version)
            if "author" in body:
                task["lastUpdatedBy"] = body["author"]
            self._append_task_event("EVENT_TYPE_UPDATE", task)
            return copy.deepcopy(task)

    def cancel_task(self, task_id: str, body: dict) -> dict:
        with self.cond:
            task = self.tasks.get(task_id)
            if task is None:
                raise not_found(f"task {task_id} not found")
            meta = self._task_meta[task_id]
            current = task["status"].get("status")
            if current in TERMINAL:
                raise bad_request(
                    f"task {task_id} is in terminal state {current}", code="FAILED_PRECONDITION"
                )
            at = now()
            if not meta.delivered:
                queue = self._agent_queues.get(meta.assignee_id or "")
                if queue is not None:
                    try:
                        queue.remove(("execute", task_id))
                    except ValueError:
                        pass
                self._set_status(
                    task,
                    {
                        "status": "STATUS_DONE_NOT_OK",
                        "taskError": {"code": "ERROR_CODE_CANCELLED", "message": "cancelled before delivery"},
                        "lastUpdateTime": format_time(at),
                        "endTime": format_time(at),
                    },
                    at,
                )
            else:
                status = {
                    **task["status"],
                    "status": "STATUS_CANCEL_REQUESTED",
                    "lastUpdateTime": format_time(at),
                }
                self._set_status(task, status, at)
                cancel: dict[str, Any] = {"taskId": task_id}
                assignee = _get(task, "relations", "assignee")
                if assignee:
                    cancel["assignee"] = assignee
                if "author" in body:
                    cancel["author"] = body["author"]
                self._agent_queues.setdefault(meta.assignee_id or "", deque()).append(("cancel", cancel))
            if "author" in body:
                task["lastUpdatedBy"] = body["author"]
            self._append_task_event("EVENT_TYPE_UPDATE", task)
            return copy.deepcopy(task)

    def query_tasks(self, body: dict) -> dict:
        token = body.get("pageToken") or "0"
        if not token.isdigit():
            raise bad_request("invalid pageToken")
        offset = int(token)
        parent = body.get("parentTaskId")
        status = _get(body, "statusFilter", "status")
        rng = body.get("updateTimeRange") or {}
        try:
            start = parse_time(rng["startTime"]) if rng.get("startTime") else None
            end = parse_time(rng["endTime"]) if rng.get("endTime") else None
        except ValueError as exc:
            raise bad_request(f"updateTimeRange: {exc}") from exc
        if parent and (status or rng):
            raise bad_request("parentTaskId is mutually exclusive with the other query parameters")
        with self.cond:
            matches = []
            for task in sorted(self.tasks.values(), key=lambda t: (t["createTime"], t["version"]["taskId"])):
                if parent and _get(task, "relations", "parentTaskId") != parent:
                    continue
                if status and task["status"].get("status") != status:
                    continue
                updated = parse_time(task["lastUpdateTime"])
                if start and updated < start:
                    continue
                if end and updated > end:
                    continue
                matches.append(task)
            page = copy.deepcopy(matches[offset : offset + TASK_PAGE_SIZE])
        out: dict[str, Any] = {"tasks": page}
        if offset + TASK_PAGE_SIZE < len(matches):
            out["nextPageToken"] = str(offset + TASK_PAGE_SIZE)
        return out

    # ------------------------------------------------------------------ agent delivery
    def next_agent_request(self, entity_ids: list[str], timeout: float) -> tuple[str, tuple, dict] | None:
        """Pop the next request for any of ``entity_ids`` (waiting up to ``timeout``).

        Returns ``(entity_id, queue_item, AgentRequest JSON)`` so a failed delivery can be requeued.
        Delivering an execute request moves the task from STATUS_CREATED to STATUS_SENT.
        """
        deadline = now() + timedelta(seconds=timeout)
        with self.cond:
            while True:
                for eid in entity_ids:
                    queue = self._agent_queues.get(eid)
                    while queue:
                        item = queue.popleft()
                        rendered = self._render(item)
                        if rendered is not None:
                            return eid, item, rendered
                remaining = (deadline - now()).total_seconds()
                if remaining <= 0 or self.closed:
                    return None
                self.cond.wait(remaining)

    def _render(self, item: tuple) -> dict | None:
        kind, payload = item
        if kind == "cancel":
            return {"cancelRequest": copy.deepcopy(payload)}
        task = self.tasks.get(payload)
        if task is None or task["status"].get("status") in TERMINAL:
            return None
        meta = self._task_meta[payload]
        if not meta.delivered:
            meta.delivered = True
            at = now()
            self._set_status(
                task, {**task["status"], "status": "STATUS_SENT", "lastUpdateTime": format_time(at)}, at
            )
            self._append_task_event("EVENT_TYPE_UPDATE", task)
        return {"executeRequest": {"task": copy.deepcopy(task)}}

    def requeue(self, entity_id: str, item: tuple) -> None:
        with self.cond:
            self._agent_queues.setdefault(entity_id, deque()).appendleft(item)
            self.cond.notify_all()

    # ------------------------------------------------------------------ objects
    @staticmethod
    def check_object_path(path: str) -> None:
        if not path or not _OBJECT_PATH_RE.match(path):
            raise bad_request(
                "objectPath must be non-empty and use only letters, digits, '/', '_', '=', '-', '.'"
            )
        if any(seg in ("", ".", "..") for seg in path.split("/")):
            raise bad_request("objectPath must not contain empty, '.' or '..' segments")

    def upload_object(self, path: str, data: bytes, ttl_ns: int | None) -> dict:
        self.check_object_path(path)
        if len(data) > MAX_OBJECT_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "objects must be 1 GiB or smaller")
        at = now()
        if ttl_ns is not None:
            if ttl_ns <= 0:
                raise bad_request("Time-To-Live must be a positive number of nanoseconds")
            expiry = at + timedelta(microseconds=ttl_ns / 1000)
        else:
            expiry = at + DEFAULT_OBJECT_TTL
        obj = StoredObject(
            data=data, checksum=hashlib.sha256(data).hexdigest(), last_updated=at, expiry=expiry
        )
        with self.cond:
            self.objects[path] = obj
            return obj.metadata(path)

    def get_object(self, path: str) -> StoredObject:
        self.check_object_path(path)
        with self.cond:
            obj = self.objects.get(path)
            if obj is None or (obj.expiry is not None and obj.expiry <= now()):
                self.objects.pop(path, None)
                raise not_found(f"object {path} not found")
            return obj

    def delete_object(self, path: str) -> None:
        self.check_object_path(path)
        with self.cond:
            if self.objects.pop(path, None) is None:
                raise not_found(f"object {path} not found")

    def list_objects(
        self, prefix: str | None, since: datetime | None, page_token: str | None, max_page_size: int | None
    ) -> dict:
        size = max_page_size or OBJECT_PAGE_SIZE
        if size <= 0:
            raise bad_request("maxPageSize must be positive")
        token = page_token or "0"
        if not token.isdigit():
            raise bad_request("invalid pageToken")
        offset = int(token)
        t = now()
        with self.cond:
            rows = [
                obj.metadata(path)
                for path, obj in sorted(self.objects.items())
                if (not prefix or path.startswith(prefix))
                and (since is None or obj.last_updated >= since)
                and (obj.expiry is None or obj.expiry > t)
            ]
        out: dict[str, Any] = {"path_metadatas": rows[offset : offset + size]}
        if offset + size < len(rows):
            out["next_page_token"] = str(offset + size)
        return out
