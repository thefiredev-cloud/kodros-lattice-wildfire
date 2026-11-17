"""HTTP front end of the harness: the Lattice REST paths the Python SDK calls, on stdlib http.server."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .filters import FilterError, compile_statement
from .schema import schema_error, strip_nulls
from .store import ApiError, Store, bad_request, camel
from .timeutil import format_time, now, parse_time

log = logging.getLogger("lattice_wildfire.harness")

SANDBOX_HEADER = "anduril-sandbox-authorization"
DEFAULT_HEARTBEAT_MS = 30_000
MIN_HEARTBEAT_MS = 50
MIN_TASK_RATE_LIMIT_MS = 250
SWEEP_INTERVAL_S = 1.0


class _ClientGone(Exception):
    pass


def _project(entity: dict, components: list[str] | None) -> dict:
    if not components:
        return entity
    keep = {"entityId", "isLive", "expiryTime"} | {camel(c) for c in components}
    return {k: v for k, v in entity.items() if k in keep}


def _subset(want: Any, have: Any) -> bool:
    """True when every key/value in ``want`` appears in ``have`` (nested dicts compared the same way)."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_subset(v, have.get(k)) for k, v in want.items())
    return want == have


@dataclass(frozen=True)
class _TaskFilter:
    """``TaskStreamRequest`` filters: everything here has to match for a task event to be sent."""

    parent_task_id: str | None = None
    task_type_urls: tuple[str, ...] = ()
    task_type_prefix: str | None = None
    assignee: dict | None = None
    statuses: frozenset[str] = frozenset()
    exclude_statuses: bool = False
    update_start: datetime | None = None

    def matches(self, task: dict, *, preexisting: bool) -> bool:
        spec_type = (task.get("specification") or {}).get("@type") or ""
        if self.task_type_urls and spec_type not in self.task_type_urls:
            return False
        if self.task_type_prefix and not spec_type.startswith(self.task_type_prefix):
            return False
        if self.assignee is not None:
            assignee = (task.get("relations") or {}).get("assignee") or {}
            if not _subset(self.assignee, strip_nulls(assignee)):
                return False
        if self.statuses:
            hit = (task.get("status") or {}).get("status") in self.statuses
            if hit == self.exclude_statuses:
                return False
        if preexisting and self.update_start is not None:
            if parse_time(task["lastUpdateTime"]) < self.update_start:
                return False
        if self.parent_task_id is not None:
            parent = (task.get("relations") or {}).get("parentTaskId")
            if parent != self.parent_task_id:
                return False
        return True


def _task_filter(body: dict) -> _TaskFilter:
    """Build the stream filter, rejecting the combinations the Tasks API rejects."""
    parent = body.get("parentTaskId")
    if parent and any(body.get(name) for name in ("updateStartTime", "assignee", "statusFilter", "taskType")):
        raise bad_request(
            "parentTaskId is mutually exclusive with updateStartTime, assignee, statusFilter and taskType"
        )
    status_filter = body.get("statusFilter") or {}
    filter_type = status_filter.get("filterType") or "FILTER_TYPE_INVALID"
    if filter_type not in ("FILTER_TYPE_INVALID", "FILTER_TYPE_INCLUSIVE", "FILTER_TYPE_EXCLUSIVE"):
        raise bad_request("statusFilter.filterType must be FILTER_TYPE_INCLUSIVE or FILTER_TYPE_EXCLUSIVE")
    start = body.get("updateStartTime")
    try:
        update_start = parse_time(start) if start else None
    except ValueError as exc:
        raise bad_request(f"updateStartTime: {exc}") from exc
    task_type = body.get("taskType") or {}
    return _TaskFilter(
        parent_task_id=parent,
        task_type_urls=tuple(task_type.get("taskTypeUrls") or ()),
        task_type_prefix=task_type.get("taskTypePrefix"),
        assignee=strip_nulls(body.get("assignee")) or None,
        statuses=frozenset(status_filter.get("statuses") or ()),
        exclude_statuses=filter_type == "FILTER_TYPE_EXCLUSIVE",
        update_start=update_start,
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server
    # Set once a response's headers are on the wire: a later failure must not start a second one.
    _response_started = False

    # ------------------------------------------------------------------ plumbing
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.info("%s - %s", self.address_string(), format % args)

    def end_headers(self) -> None:
        super().end_headers()
        self._response_started = True

    def _close_after_response(self) -> None:
        """Announce the close before the headers go out, then close the connection once they have.

        HTTP/1.1 defaults to keep-alive: a response that ends with the server dropping the socket
        makes the client return it to its connection pool, and the next request on that socket
        races the close (httpx ``RemoteProtocolError``). ``Connection: close`` is what stops the
        socket being pooled: h11 then closes it as soon as the response body ends.
        """
        self.send_header("Connection", "close")
        self.close_connection = True

    def _read_body(self) -> bytes:
        self._body_consumed = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline()
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self, component: str) -> dict:
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise bad_request(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise bad_request("request body must be a JSON object")
        body = strip_nulls(body)
        problem = schema_error(component, body)
        if problem:
            raise bad_request(problem)
        return body

    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(self, err: ApiError) -> None:
        self._send_json(err.status, {"code": err.code, "message": err.message})

    def _check_auth(self, path: str) -> None:
        if path != "/api/v1/oauth/token":
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer ") or not auth[7:].strip():
                raise ApiError(
                    401, "UNAUTHENTICATED", "missing or malformed 'Authorization: Bearer <token>' header"
                )
        if self.server.sandbox:
            sbx = self.headers.get(SANDBOX_HEADER) or ""
            if not sbx.startswith("Bearer ") or not sbx[7:].strip():
                raise ApiError(
                    401, "UNAUTHENTICATED", f"missing or malformed '{SANDBOX_HEADER}: Bearer <token>' header"
                )

    def _dispatch(self) -> None:
        # One handler instance serves every request on a keep-alive connection: reset per request.
        self._body_consumed = False
        self._response_started = False
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        try:
            for method, pattern, handler in ROUTES:
                if method != self.command:
                    continue
                m = pattern.fullmatch(path)
                if m:
                    self._check_auth(path)
                    handler(self, *(unquote(g) for g in m.groups()), query=query)
                    return
            raise ApiError(
                404, "NOT_FOUND", f"{self.command} {path} is not implemented by the lattice-wildfire harness"
            )
        except ApiError as err:
            if self.command in ("POST", "PUT") and not self._body_consumed:
                self._read_body()
            self._send_error(err)
        except (BrokenPipeError, ConnectionResetError, _ClientGone):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - surface harness bugs as 500s, never hang the client
            log.exception("harness error on %s %s", self.command, path)
            if self._response_started:
                # The framing is already committed (an SSE stream, or a body mid-write): a JSON
                # error cannot be spliced in, so end the connection and let the client see it.
                self.close_connection = True
                return
            self._send_internal(exc)

    def _send_internal(self, exc: Exception) -> None:
        """500 for an unexpected harness bug; the connection ends because the request state is unknown."""
        self.close_connection = True
        self._send_json(
            500,
            {"code": "INTERNAL", "message": f"harness error: {type(exc).__name__}: {exc}"},
            {"Connection": "close"},
        )

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    # ------------------------------------------------------------------ SSE
    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self._close_after_response()
        self.end_headers()

    def _sse(self, payload: dict) -> None:
        text = f"event: {payload['event']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
        try:
            self.wfile.write(f"{len(text):x}\r\n".encode() + text + b"\r\n")
            self.wfile.flush()
        except OSError as exc:
            raise _ClientGone() from exc

    def _end_sse(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def _heartbeat(self) -> None:
        self._sse({"event": "heartbeat", "timestamp": format_time(now())})

    @staticmethod
    def _heartbeat_s(value: Any) -> float:
        ms = value if isinstance(value, int) and value > 0 else DEFAULT_HEARTBEAT_MS
        return max(ms, MIN_HEARTBEAT_MS) / 1000.0

    # ------------------------------------------------------------------ entities
    def put_entity(self, *, query: dict) -> None:
        body = self._json_body("Entity")
        self._send_json(200, self.server.store.publish_entity(body))

    def get_entity(self, entity_id: str, *, query: dict) -> None:
        self._send_json(200, self.server.store.get_entity(entity_id))

    def put_entity_override(self, entity_id: str, field_path: str, *, query: dict) -> None:
        body = self._json_body("EntityOverride")
        self._send_json(200, self.server.store.override_entity(entity_id, field_path, body))

    def delete_entity_override(self, entity_id: str, field_path: str, *, query: dict) -> None:
        self._send_json(200, self.server.store.remove_entity_override(entity_id, field_path))

    def long_poll(self, *, query: dict) -> None:
        body = self._json_body("EntityEventRequest")
        self._send_json(200, self.server.store.long_poll(body.get("sessionToken", ""), body.get("batchSize")))

    def stream_entities(self, *, query: dict) -> None:
        body = self._json_body("EntityStreamRequest")
        store = self.server.store
        try:
            matches = compile_statement(body["filter"]) if body.get("filter") else (lambda _e: True)
        except FilterError as exc:
            raise bad_request(f"filter: {exc}") from exc
        components = body.get("componentsToInclude")
        heartbeat_s = self._heartbeat_s(body.get("heartbeatIntervalMS"))
        pre, cursor = store.entity_snapshot()
        self._start_sse()
        matched: set[str] = set()
        for event in pre:
            entity = event["entity"]
            if matches(entity):
                matched.add(entity["entityId"])
                self._sse({"event": "entity", **event, "entity": _project(entity, components)})
        if body.get("preExistingOnly"):
            self._end_sse()
            return
        next_beat = now().timestamp() + heartbeat_s
        while not store.closed and not self.server.stopping.is_set():
            wait = max(0.0, next_beat - now().timestamp())
            try:
                events, cursor = store.wait_entity_events(cursor, min(wait, 1.0))
            except ApiError:
                self._end_sse()
                return
            for event in events:
                entity = event["entity"]
                eid = entity["entityId"]
                was = eid in matched
                if event["eventType"] == "EVENT_TYPE_DELETED":
                    if not was:
                        continue
                    matched.discard(eid)
                    out_type = "EVENT_TYPE_DELETED"
                elif matches(entity):
                    matched.add(eid)
                    out_type = event["eventType"] if was else "EVENT_TYPE_CREATED"
                elif was:
                    matched.discard(eid)
                    out_type = "EVENT_TYPE_DELETED"
                else:
                    continue
                self._sse(
                    {
                        "event": "entity",
                        "eventType": out_type,
                        "time": event["time"],
                        "entity": _project(entity, components),
                    }
                )
            if now().timestamp() >= next_beat:
                self._heartbeat()
                next_beat = now().timestamp() + heartbeat_s
        self._end_sse()

    # ------------------------------------------------------------------ tasks
    def create_task(self, *, query: dict) -> None:
        body = self._json_body("TaskCreation")
        self._send_json(201, self.server.store.create_task(body))

    def get_task(self, task_id: str, *, query: dict) -> None:
        self._send_json(200, self.server.store.get_task(task_id))

    def update_status(self, task_id: str, *, query: dict) -> None:
        body = self._json_body("TaskStatusUpdate")
        self._send_json(200, self.server.store.update_task_status(task_id, body))

    def cancel_task(self, task_id: str, *, query: dict) -> None:
        body = self._json_body("TaskCancellation")
        self._send_json(200, self.server.store.cancel_task(task_id, body))

    def query_tasks(self, *, query: dict) -> None:
        body = self._json_body("TaskQuery")
        self._send_json(200, self.server.store.query_tasks(body))

    def stream_tasks(self, *, query: dict) -> None:
        body = self._json_body("TaskStreamRequest")
        flt = _task_filter(body)
        rate_raw = body.get("rateLimit")
        rate_s = 0.0
        if isinstance(rate_raw, int) and rate_raw:
            if rate_raw < MIN_TASK_RATE_LIMIT_MS:
                raise bad_request(f"rateLimit must be at least {MIN_TASK_RATE_LIMIT_MS} ms when set")
            rate_s = rate_raw / 1000.0
        heartbeat_s = self._heartbeat_s(body.get("heartbeatIntervalMs"))
        store = self.server.store
        self._start_sse()
        last_sent: dict[str, float] = {}

        def emit(event: dict, *, preexisting: bool) -> None:
            task = event["task"]
            if not flt.matches(task, preexisting=preexisting):
                return
            task_id = (task.get("version") or {}).get("taskId") or ""
            if rate_s:
                at_s = now().timestamp()
                if at_s - last_sent.get(task_id, 0.0) < rate_s:
                    return
                last_sent[task_id] = at_s
            self._sse({"event": "task_event", "taskEvent": {"eventType": event["eventType"], "task": task}})

        if body.get("excludePreexistingTasks"):
            cursor = store.task_cursor()
        else:
            pre, cursor = store.task_snapshot()
            for event in pre:
                emit(event, preexisting=True)
        next_beat = now().timestamp() + heartbeat_s
        while not store.closed and not self.server.stopping.is_set():
            wait = max(0.0, next_beat - now().timestamp())
            try:
                events, cursor = store.wait_task_events(cursor, min(wait, 1.0))
            except ApiError:
                self._end_sse()
                return
            for event in events:
                emit(event, preexisting=False)
            if now().timestamp() >= next_beat:
                self._heartbeat()
                next_beat = now().timestamp() + heartbeat_s
        self._end_sse()

    @staticmethod
    def _selector_ids(body: dict) -> list[str]:
        ids = [i for i in ((body.get("agentSelector") or {}).get("entityIds") or []) if i]
        if not ids:
            raise bad_request("agentSelector.entityIds must contain at least one entity id")
        return ids

    def agent_listen(self, *, query: dict) -> None:
        body = self._json_body("AgentListener")
        ids = self._selector_ids(body)
        store = self.server.store
        got = store.next_agent_request(ids, store.long_poll_timeout_s)
        if got is None:
            self._send_json(200, {})
            return
        eid, item, request = got
        try:
            self._send_json(200, request)
        except OSError:
            store.requeue(eid, item)
            raise

    def agent_stream(self, *, query: dict) -> None:
        body = self._json_body("AgentStreamRequest")
        ids = self._selector_ids(body)
        heartbeat_s = self._heartbeat_s(body.get("heartbeatIntervalMs"))
        store = self.server.store
        self._start_sse()
        next_beat = now().timestamp() + heartbeat_s
        while not store.closed and not self.server.stopping.is_set():
            wait = max(0.0, next_beat - now().timestamp())
            got = store.next_agent_request(ids, min(wait, 1.0))
            if got is not None:
                eid, item, request = got
                try:
                    self._sse({"event": "agent_request", **request})
                except _ClientGone:
                    store.requeue(eid, item)
                    raise
            if now().timestamp() >= next_beat:
                self._heartbeat()
                next_beat = now().timestamp() + heartbeat_s
        self._end_sse()

    # ------------------------------------------------------------------ objects
    def list_objects(self, *, query: dict) -> None:
        def one(name: str) -> str | None:
            vals = query.get(name)
            return vals[0] if vals else None

        since_raw = one("sinceTimestamp")
        try:
            since = parse_time(since_raw) if since_raw else None
        except ValueError as exc:
            raise bad_request(f"sinceTimestamp: {exc}") from exc
        size_raw = one("maxPageSize")
        if size_raw is not None and not size_raw.isdigit():
            raise bad_request("maxPageSize must be an integer")
        out = self.server.store.list_objects(
            one("prefix"), since, one("pageToken"), int(size_raw) if size_raw else None
        )
        self._send_json(200, out)

    def upload_object(self, object_path: str, *, query: dict) -> None:
        data = self._read_body()
        ttl_raw = self.headers.get("Time-To-Live")
        if ttl_raw is not None and not ttl_raw.strip().lstrip("-").isdigit():
            raise bad_request("Time-To-Live header must be an integer number of nanoseconds")
        mode = self.headers.get("Distribution-Mode")
        if mode is not None and mode != "force":
            raise bad_request("Distribution-Mode header must be 'force'")
        ttl = int(ttl_raw) if ttl_raw is not None else None
        self._send_json(200, self.server.store.upload_object(object_path, data, ttl))

    def get_object(self, object_path: str, *, query: dict) -> None:
        obj = self.server.store.get_object(object_path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(obj.data)))
        self.send_header("Checksum", obj.checksum)
        self.end_headers()
        self.wfile.write(obj.data)

    def head_object(self, object_path: str, *, query: dict) -> None:
        obj = self.server.store.get_object(object_path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(obj.data)))
        self.send_header("Path", object_path)
        self.send_header("Checksum", obj.checksum)
        self.send_header("Last-Modified", format_datetime(obj.last_updated, usegmt=True))
        if obj.expiry is not None:
            self.send_header("Expires", format_datetime(obj.expiry, usegmt=True))
        self.end_headers()

    def delete_object(self, object_path: str, *, query: dict) -> None:
        self.server.store.delete_object(object_path)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------ oauth
    def oauth_token(self, *, query: dict) -> None:
        raw = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "application/json":
            try:
                form = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise bad_request("token request body is not valid JSON") from exc
        else:
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        if form.get("grant_type") != "client_credentials":
            raise bad_request("grant_type must be client_credentials", code="unsupported_grant_type")
        if not form.get("client_id") or not form.get("client_secret"):
            raise ApiError(401, "invalid_client", "client_id and client_secret are required")
        self._send_json(
            200, {"access_token": f"harness-{uuid.uuid4().hex}", "token_type": "Bearer", "expires_in": 3600}
        )


Route = tuple[str, "re.Pattern[str]", Callable[..., None]]
_SEG = r"([^/]+)"
ROUTES: list[Route] = [
    ("PUT", re.compile(r"/api/v1/entities"), _Handler.put_entity),
    ("POST", re.compile(r"/api/v1/entities/events"), _Handler.long_poll),
    ("POST", re.compile(r"/api/v1/entities/stream"), _Handler.stream_entities),
    ("PUT", re.compile(rf"/api/v1/entities/{_SEG}/override/{_SEG}"), _Handler.put_entity_override),
    ("DELETE", re.compile(rf"/api/v1/entities/{_SEG}/override/{_SEG}"), _Handler.delete_entity_override),
    ("GET", re.compile(rf"/api/v1/entities/{_SEG}"), _Handler.get_entity),
    ("POST", re.compile(r"/api/v1/tasks"), _Handler.create_task),
    ("POST", re.compile(r"/api/v1/tasks/query"), _Handler.query_tasks),
    ("POST", re.compile(r"/api/v1/tasks/stream"), _Handler.stream_tasks),
    ("GET", re.compile(rf"/api/v1/tasks/{_SEG}"), _Handler.get_task),
    ("PUT", re.compile(rf"/api/v1/tasks/{_SEG}/status"), _Handler.update_status),
    ("PUT", re.compile(rf"/api/v1/tasks/{_SEG}/cancel"), _Handler.cancel_task),
    ("POST", re.compile(r"/api/v1/agent/listen"), _Handler.agent_listen),
    ("POST", re.compile(r"/api/v1/agent/stream"), _Handler.agent_stream),
    ("GET", re.compile(r"/api/v1/objects"), _Handler.list_objects),
    ("POST", re.compile(r"/api/v1/objects/(.+)"), _Handler.upload_object),
    ("GET", re.compile(r"/api/v1/objects/(.+)"), _Handler.get_object),
    ("HEAD", re.compile(r"/api/v1/objects/(.+)"), _Handler.head_object),
    ("DELETE", re.compile(r"/api/v1/objects/(.+)"), _Handler.delete_object),
    ("POST", re.compile(r"/api/v1/oauth/token"), _Handler.oauth_token),
]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: Store, sandbox: bool) -> None:
        super().__init__(address, _Handler)
        self.store = store
        self.sandbox = sandbox
        self.stopping = threading.Event()


@dataclass
class Harness:
    """A running harness. ``base_url`` is what ``LATTICE_BASE_URL`` should be set to."""

    base_url: str
    store: Store
    _server: _Server = field(repr=False)
    _threads: list[threading.Thread] = field(default_factory=list, repr=False)

    def stop(self) -> None:
        self._server.stopping.set()
        self.store.close()
        self._server.shutdown()
        self._server.server_close()
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def start_harness(
    port: int = 0,
    *,
    host: str = "127.0.0.1",
    sandbox: bool = False,
    long_poll_timeout_s: float = 300.0,
) -> Harness:
    """Start the harness on a background thread. ``port=0`` picks a free port."""
    store = Store(long_poll_timeout_s=long_poll_timeout_s)
    server = _Server((host, port), store, sandbox)
    bound_host, bound_port = server.server_address[:2]

    def sweeper() -> None:
        while not server.stopping.wait(SWEEP_INTERVAL_S):
            store.sweep()

    threads = [
        threading.Thread(target=server.serve_forever, name="lw-harness-http", daemon=True),
        threading.Thread(target=sweeper, name="lw-harness-sweeper", daemon=True),
    ]
    for t in threads:
        t.start()
    url_host = f"[{bound_host}]" if ":" in str(bound_host) else bound_host
    return Harness(base_url=f"http://{url_host}:{bound_port}", store=store, _server=server, _threads=threads)
