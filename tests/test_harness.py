from __future__ import annotations

import hashlib
import queue
import threading
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from anduril import (
    Aliases,
    Entity,
    EntityIdsSelector,
    GoogleProtobufAny,
    MilView,
    Ontology,
    Principal,
    Relations,
    System,
    TaskCatalog,
    TaskDefinition,
    TaskStatus,
    TaskStreamRequestStatusFilter,
    TaskStreamRequestTaskTypeTaskTypePrefix,
    Tracked,
)
from anduril.errors import BadRequestError, NotFoundError, UnauthorizedError
from anduril.tasks.types.task_query_status_filter import TaskQueryStatusFilter

from lattice_wildfire.core import entities as ent
from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings
from lattice_wildfire.harness import start_harness
from lattice_wildfire.harness.store import Store

INVESTIGATE = "type.googleapis.com/anduril.tasks.v2.Investigate"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _track_kwargs(entity_id: str, lat: float = 34.2, lon: float = -118.2, expiry_s: int = 600) -> dict:
    return {
        "entity_id": entity_id,
        "is_live": True,
        "expiry_time": ent.expiry(expiry_s),
        "provenance": ent.provenance("test", "firms-viirs-hotspot"),
        "aliases": Aliases(name=f"Hotspot {entity_id}"),
        "location": ent.point_location(lat, lon),
        "ontology": Ontology(template="TEMPLATE_TRACK", platform_type="Wildfire Hotspot"),
        "mil_view": MilView(disposition="DISPOSITION_UNKNOWN", environment="ENVIRONMENT_LAND"),
        "symbology": ent.symbology("DISPOSITION_UNKNOWN", "ENVIRONMENT_LAND"),
        "data_classification": ent.unclassified(),
        "tracked": Tracked(track_quality_wrapper=2),
    }


def _publish_agent(client, agent_id: str) -> None:
    client.entities.publish_entity(
        entity_id=agent_id,
        is_live=True,
        expiry_time=ent.expiry(600),
        provenance=ent.provenance("test", "kodros-analyst"),
        aliases=Aliases(name="Test analyst"),
        location=ent.point_location(34.05, -118.25),
        ontology=Ontology(template="TEMPLATE_ASSET", platform_type="AI Analyst"),
        mil_view=MilView(disposition="DISPOSITION_FRIENDLY", environment="ENVIRONMENT_LAND"),
        task_catalog=TaskCatalog(task_definitions=[TaskDefinition(task_specification_url=INVESTIGATE)]),
    )


def _create_task(client, agent_id: str, target: str = "lw-test-target"):
    return client.tasks.create_task(
        display_name="investigate",
        specification=GoogleProtobufAny(type=INVESTIGATE, objective={"entity_id": target}),
        author=Principal(system=System(service_name="test-planner")),
        relations=Relations(assignee=Principal(system=System(entity_id=agent_id))),
        is_executed_elsewhere=False,
    )


def _status(client, task_id: str, status: str, version: int, agent_id: str):
    return client.tasks.update_task_status(
        task_id,
        status_version=version,
        new_status=TaskStatus(status=status),
        author=Principal(system=System(entity_id=agent_id)),
    )


def _create_unassigned_task(client, target: str = "lw-test-target"):
    """A task with no assignee: it stays STATUS_CREATED, so it is a stable stream subject."""
    return client.tasks.create_task(
        display_name="stream probe",
        specification=GoogleProtobufAny(type=INVESTIGATE, objective={"entity_id": target}),
        author=Principal(system=System(service_name="test-stream")),
        is_executed_elsewhere=False,
    )


def _task_events_for(stream, task_id: str, *, count: int, timeout_s: float = 20.0) -> list:
    """Read one task's events from a task stream; raises instead of blocking forever."""
    out: list = []
    deadline = time.time() + timeout_s
    while len(out) < count:
        if time.time() > deadline:
            raise AssertionError(f"only {len(out)} of {count} events for {task_id} arrived in {timeout_s}s")
        event = next(stream)
        if event.event == "task_event" and event.task_event.task.version.task_id == task_id:
            out.append(event.task_event)
    return out


def _stream_task_events(client, *, heartbeats: int = 2, timeout_s: float = 20.0, **kwargs) -> list:
    """Collect stream events until ``heartbeats`` heartbeat events arrived, then close the stream."""
    events: list = []
    stream = client.tasks.stream_tasks(**kwargs)
    deadline = time.time() + timeout_s
    beats = 0
    try:
        for event in stream:
            events.append(event)
            if event.event == "heartbeat":
                beats += 1
                if beats >= heartbeats:
                    break
            if time.time() > deadline:
                raise AssertionError(f"only {beats} heartbeats arrived in {timeout_s}s")
    finally:
        stream.close()
    return events


# ---------------------------------------------------------------- entities
def test_rejects_entity_missing_data_type(lattice):
    kwargs = _track_kwargs(_uid("lw-bad"))
    kwargs["provenance"] = ent.Provenance(
        integration_name="lattice-wildfire-test", source_update_time=ent.now_utc()
    )
    with pytest.raises(BadRequestError) as info:
        lattice.entities.publish_entity(**kwargs)
    assert info.value.status_code == 400
    assert "provenance.dataType must be set" in info.value.body["message"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"is_live": None}, "isLive"),
        ({"aliases": Aliases()}, "aliases.name"),
        ({"location": None}, "location.position"),
        ({"mil_view": None}, "milView"),
    ],
)
def test_rejects_entity_floor_violations(lattice, change, message):
    kwargs = {**_track_kwargs(_uid("lw-bad")), **change}
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    with pytest.raises(BadRequestError) as info:
        lattice.entities.publish_entity(**kwargs)
    assert message in info.value.body["message"]


def test_rejects_expiry_out_of_window(lattice):
    kwargs = _track_kwargs(_uid("lw-bad"))
    kwargs["expiry_time"] = ent.now_utc() + timedelta(days=31)
    with pytest.raises(BadRequestError) as info:
        lattice.entities.publish_entity(**kwargs)
    assert "30 days" in info.value.body["message"]
    kwargs["expiry_time"] = ent.now_utc() - timedelta(seconds=5)
    with pytest.raises(BadRequestError) as info:
        lattice.entities.publish_entity(**kwargs)
    assert "future" in info.value.body["message"]


def test_schema_validation_rejects_wrong_types(lattice_env):
    resp = httpx.put(
        f"{lattice_env.base_url}/api/v1/entities",
        json={"entityId": "x", "isLive": "yes"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 400
    assert "Entity schema violation at isLive" in resp.json()["message"]


def test_publish_full_track_and_read_back(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    got = lattice.entities.get_entity(eid)
    assert got.entity_id == eid
    assert got.is_live is True
    assert got.aliases.name == f"Hotspot {eid}"
    assert got.location.position.latitude_degrees == 34.2
    assert got.symbology.mil_std2525c.sidc == "SUGP-----------"
    assert got.provenance.integration_name == "lattice-wildfire-test"
    assert ent.data_type_of(got) == "firms-viirs-hotspot"
    assert got.created_time is not None
    with pytest.raises(NotFoundError):
        lattice.entities.get_entity(_uid("lw-missing"))


def test_older_source_update_time_is_ignored(lattice):
    eid = _uid("lw-firms")
    newer = _track_kwargs(eid)
    lattice.entities.publish_entity(**newer)
    older = _track_kwargs(eid, lat=34.9)
    older["provenance"] = ent.provenance("test", "firms-viirs-hotspot", ent.now_utc() - timedelta(hours=1))
    lattice.entities.publish_entity(**older)
    assert lattice.entities.get_entity(eid).location.position.latitude_degrees == 34.2


def test_stream_entities_pre_existing_only_returns_entity(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    events = list(lattice.entities.stream_entities(pre_existing_only=True))
    mine = [e for e in events if e.event == "entity" and e.entity.entity_id == eid]
    assert len(mine) == 1
    assert mine[0].event_type == "EVENT_TYPE_PREEXISTING"
    assert mine[0].entity.aliases.name == f"Hotspot {eid}"


def test_stream_entities_filter_and_components(lattice):
    wanted = _uid("lw-wfigs")
    other = _uid("lw-firms")
    kwargs = _track_kwargs(wanted)
    kwargs["provenance"] = ent.provenance("test", "wfigs-incident")
    lattice.entities.publish_entity(**kwargs)
    lattice.entities.publish_entity(**_track_kwargs(other))
    from anduril import Predicate, Statement, StringType, Value

    stmt = Statement(
        predicate=Predicate(
            field_path="provenance.data_type",
            value=Value(string_type=StringType(value="wfigs-incident")),
            comparator="COMPARATOR_EQUALITY",
        )
    )
    events = list(
        lattice.entities.stream_entities(
            pre_existing_only=True, filter=stmt, components_to_include=["aliases"]
        )
    )
    ids = {e.entity.entity_id for e in events if e.event == "entity"}
    assert wanted in ids and other not in ids
    hit = next(e for e in events if e.event == "entity" and e.entity.entity_id == wanted)
    assert hit.entity.aliases.name and hit.entity.location is None


def test_stream_entities_live_create_and_heartbeat(lattice):
    received: queue.Queue = queue.Queue()
    stop = threading.Event()

    def consume():
        for event in lattice.entities.stream_entities(heartbeat_interval_ms=100):
            received.put(event)
            if stop.is_set():
                return

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:  # the first heartbeat proves the stream is open
        if received.get(timeout=5).event == "heartbeat":
            break
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    created = None
    while time.time() < deadline and created is None:
        event = received.get(timeout=5)
        if event.event == "entity" and event.entity.entity_id == eid:
            created = event
    stop.set()
    t.join(timeout=5)
    assert created is not None and created.event_type == "EVENT_TYPE_CREATED"


def test_stream_response_tells_the_client_to_close(lattice, lattice_env):
    """A stream ends its connection on the record, so the client never pools the socket.

    Regression: the SSE response set ``close_connection`` and dropped the socket without sending
    ``Connection: close``. httpx returned that socket to its pool when the chunked body ended, and
    the next request on it raced the server's close (``httpx.RemoteProtocolError: Server
    disconnected without sending a response``).
    """
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))

    events = list(lattice.entities.stream_entities(pre_existing_only=True))  # real SDK client
    assert eid in [e.entity.entity_id for e in events if e.event == "entity"]  # shared harness picture
    for _ in range(50):  # one client, straight after the stream: nothing to reuse, nothing to race
        assert lattice.entities.get_entity(eid).entity_id == eid

    headers = {"Authorization": "Bearer test-token"}
    with httpx.Client(base_url=lattice_env.base_url, headers=headers) as client:
        pool = client._transport._pool  # httpx keeps its httpcore pool here: proof of what is reused
        with client.stream("POST", "/api/v1/entities/stream", json={"preExistingOnly": True}) as resp:
            assert resp.headers["connection"] == "close"
            assert b"event: entity" in b"".join(resp.iter_raw())
        assert [c for c in pool._connections if not c.is_closed()] == []  # the stream's socket is gone
        assert client.get(f"/api/v1/entities/{eid}").status_code == 200
        assert client.get(f"/api/v1/entities/{eid}").status_code == 200
        assert len([c for c in pool._connections if not c.is_closed()]) == 1  # keep-alive still alive


def test_internal_error_closes_the_connection(monkeypatch):
    """A harness bug answers 500 and ends the connection on the record: the request state is unknown."""
    with start_harness(0, long_poll_timeout_s=1.0) as h:

        def boom(self, entity_id):
            raise ValueError("injected harness bug")

        monkeypatch.setattr(Store, "get_entity", boom)
        headers = {"Authorization": "Bearer test-token"}
        with httpx.Client(base_url=h.base_url, headers=headers) as client:
            pool = client._transport._pool
            for _ in range(2):  # each 500 gets a fresh socket, and no dead one is pooled
                resp = client.get("/api/v1/entities/x")
                assert resp.status_code == 500
                assert resp.headers["connection"] == "close"
                assert resp.json()["code"] == "INTERNAL"
                assert [c for c in pool._connections if not c.is_closed()] == []


def test_long_poll_session(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    first = lattice.entities.long_poll_entity_events(session_token="", batch_size=2000)
    assert first.session_token
    assert any(
        e.entity.entity_id == eid and e.event_type == "EVENT_TYPE_PREEXISTING" for e in first.entity_events
    )
    eid2 = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid2))
    # The session harness is shared: other tests' entities may expire meanwhile. Judge only ours.
    mine = {eid, eid2}
    second = lattice.entities.long_poll_entity_events(session_token=first.session_token)
    assert [
        (e.entity.entity_id, e.event_type) for e in second.entity_events if e.entity.entity_id in mine
    ] == [(eid2, "EVENT_TYPE_CREATED")]
    empty = lattice.entities.long_poll_entity_events(session_token=first.session_token)
    assert [e for e in empty.entity_events if e.entity.entity_id in mine] == []


def test_entity_expires(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid, expiry_s=1))
    lattice.entities.get_entity(eid)
    time.sleep(1.3)
    with pytest.raises(NotFoundError):
        lattice.entities.get_entity(eid)


# ---------------------------------------------------------------- tasks
def test_task_lifecycle_via_listen(lattice):
    agent = _uid("lw-analyst")
    _publish_agent(lattice, agent)
    task = _create_task(lattice, agent)
    task_id = task.version.task_id
    assert task.status.status == "STATUS_CREATED"

    request = lattice.tasks.listen_as_agent(agent_selector=EntityIdsSelector(entity_ids=[agent]))
    assert request.execute_request.task.version.task_id == task_id
    assert request.execute_request.task.specification.type == INVESTIGATE
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_SENT"

    executing = _status(lattice, task_id, "STATUS_EXECUTING", 1, agent)
    assert executing.status.status == "STATUS_EXECUTING"
    with pytest.raises(BadRequestError) as info:
        _status(lattice, task_id, "STATUS_EXECUTING", 1, agent)
    assert "stale" in info.value.body["message"]
    _status(lattice, task_id, "STATUS_DONE_OK", 2, agent)

    done = lattice.tasks.get_task(task_id)
    assert done.status.status == "STATUS_DONE_OK"
    assert done.last_updated_by.system.entity_id == agent
    with pytest.raises(BadRequestError):
        _status(lattice, task_id, "STATUS_EXECUTING", 3, agent)

    results = lattice.tasks.query_tasks(status_filter=TaskQueryStatusFilter(status="STATUS_DONE_OK"))
    assert task_id in {t.version.task_id for t in results.tasks}


def test_stream_as_agent_receives_execute_request(lattice):
    agent = _uid("lw-analyst")
    _publish_agent(lattice, agent)
    task_id = _create_task(lattice, agent).version.task_id
    got = None
    for event in lattice.tasks.stream_as_agent(
        agent_selector=EntityIdsSelector(entity_ids=[agent]), heartbeat_interval_ms=200
    ):
        if event.event == "agent_request" and event.execute_request:
            got = event.execute_request.task
            break
    assert got is not None and got.version.task_id == task_id
    _status(lattice, task_id, "STATUS_EXECUTING", 1, agent)
    _status(lattice, task_id, "STATUS_DONE_OK", 2, agent)
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_DONE_OK"


def test_cancel_routes_to_agent_or_cancels_undelivered(lattice):
    agent = _uid("lw-analyst")
    _publish_agent(lattice, agent)
    undelivered = _create_task(lattice, agent).version.task_id
    cancelled = lattice.tasks.cancel_task(undelivered, author=Principal(system=System(service_name="op")))
    assert cancelled.status.status == "STATUS_DONE_NOT_OK"
    assert cancelled.status.task_error.code == "ERROR_CODE_CANCELLED"

    task_id = _create_task(lattice, agent).version.task_id
    selector = EntityIdsSelector(entity_ids=[agent])
    assert (
        lattice.tasks.listen_as_agent(agent_selector=selector).execute_request.task.version.task_id == task_id
    )
    _status(lattice, task_id, "STATUS_EXECUTING", 1, agent)
    lattice.tasks.cancel_task(task_id, author=Principal(system=System(service_name="op")))
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_CANCEL_REQUESTED"
    cancel = lattice.tasks.listen_as_agent(agent_selector=selector).cancel_request
    assert cancel.task_id == task_id
    _status(lattice, task_id, "STATUS_DONE_NOT_OK", 2, agent)
    assert lattice.tasks.get_task(task_id).status.status == "STATUS_DONE_NOT_OK"


# ---------------------------------------------------------------- objects
def test_objects_round_trip(lattice):
    path = f"lattice-wildfire/briefs/{_uid('task')}.md"
    payload = b"# Brief\n\nReal bytes \xe2\x9c\x93\n" * 50
    meta = lattice.objects.upload_object(path, request=payload)
    assert meta.content_identifier.path == path
    assert meta.size_bytes == len(payload)
    assert b"".join(lattice.objects.get_object(path)) == payload
    headers = lattice.objects.get_object_metadata(path)
    assert headers.get("checksum") == hashlib.sha256(payload).hexdigest()
    listed = [
        m.content_identifier.path for m in lattice.objects.list_objects(prefix="lattice-wildfire/briefs/")
    ]
    assert path in listed
    lattice.objects.delete_object(path)
    with pytest.raises(NotFoundError):
        b"".join(lattice.objects.get_object(path))


# ---------------------------------------------------------------- entity overrides
def test_entity_override_round_trip(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    overridden = lattice.entities.override_entity(
        eid,
        "aliases.name",
        entity=Entity(entity_id=eid, aliases=Aliases(name="Operator relabel")),
        provenance=ent.provenance("test", "operator-override"),
    )
    assert overridden.aliases.name == "Operator relabel"
    assert lattice.entities.get_entity(eid).aliases.name == "Operator relabel"
    rows = overridden.overrides.override
    assert [r.field_path for r in rows] == ["aliases.name"]
    assert rows[0].status == "OVERRIDE_STATUS_APPLIED"
    assert rows[0].type == "OVERRIDE_TYPE_LIVE"
    assert rows[0].masked_field_value.aliases.name == "Operator relabel"
    assert rows[0].provenance.data_type == "operator-override"

    snapshot = list(lattice.entities.stream_entities(pre_existing_only=True))
    seen = [e.entity for e in snapshot if e.event == "entity" and e.entity.entity_id == eid]
    assert seen and seen[0].aliases.name == "Operator relabel"  # consumers see the override

    restored = lattice.entities.remove_entity_override(eid, "aliases.name")
    assert restored.aliases.name == f"Hotspot {eid}"
    assert not (restored.overrides and restored.overrides.override)
    assert lattice.entities.get_entity(eid).aliases.name == f"Hotspot {eid}"


def test_entity_override_nested_path_and_errors(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    hostile = lattice.entities.override_entity(
        eid, "mil_view.disposition", entity=Entity(mil_view=MilView(disposition="DISPOSITION_HOSTILE"))
    )
    assert hostile.mil_view.disposition == "DISPOSITION_HOSTILE"
    assert (
        hostile.mil_view.environment == "ENVIRONMENT_LAND"
    )  # fields the override did not name are untouched
    assert lattice.entities.remove_entity_override(eid, "mil_view.disposition").mil_view.disposition == (
        "DISPOSITION_UNKNOWN"
    )

    with pytest.raises(NotFoundError):
        lattice.entities.override_entity(
            _uid("lw-missing"), "aliases.name", entity=Entity(aliases=Aliases(name="x"))
        )
    with pytest.raises(BadRequestError) as info:
        lattice.entities.override_entity(eid, "aliases.name", entity=Entity(entity_id=eid))
    assert "fieldPath aliases.name" in info.value.body["message"]
    with pytest.raises(BadRequestError) as info:
        lattice.entities.override_entity(eid, "Aliases.name", entity=Entity(aliases=Aliases(name="x")))
    assert "lower_snake_case" in info.value.body["message"]
    with pytest.raises(NotFoundError):
        lattice.entities.remove_entity_override(eid, "aliases.name")


def test_entity_override_does_not_survive_a_republish(lattice):
    eid = _uid("lw-firms")
    lattice.entities.publish_entity(**_track_kwargs(eid))
    lattice.entities.override_entity(
        eid, "aliases.name", entity=Entity(aliases=Aliases(name="Operator relabel"))
    )
    republished = _track_kwargs(eid)
    republished["provenance"] = ent.provenance(
        "test", "firms-viirs-hotspot", ent.now_utc() + timedelta(seconds=1)
    )
    after = lattice.entities.publish_entity(**republished)
    assert after.aliases.name == f"Hotspot {eid}"
    assert not (after.overrides and after.overrides.override)
    with pytest.raises(NotFoundError):
        lattice.entities.remove_entity_override(eid, "aliases.name")


# ---------------------------------------------------------------- task stream
def test_task_stream_preexisting_task(lattice):
    task_id = _create_unassigned_task(lattice).version.task_id
    try:
        events = _stream_task_events(lattice, exclude_preexisting_tasks=False, heartbeat_interval_ms=100)
        mine = [
            e.task_event
            for e in events
            if e.event == "task_event" and e.task_event.task.version.task_id == task_id
        ]
        assert len(mine) == 1
        assert mine[0].event_type == "EVENT_TYPE_PREEXISTING"
        assert mine[0].task.status.status == "STATUS_CREATED"
        assert mine[0].task.specification.type == INVESTIGATE
    finally:
        _status(lattice, task_id, "STATUS_DONE_OK", 1, "lw-stream-agent")


def test_task_stream_live_create_and_status_update(lattice):
    stream = lattice.tasks.stream_tasks(exclude_preexisting_tasks=True, heartbeat_interval_ms=100)
    task_id = None
    try:
        first = next(stream)  # any event, usually a heartbeat: the stream is open
        assert first.event in ("heartbeat", "task_event")
        task_id = _create_unassigned_task(lattice).version.task_id
        _status(lattice, task_id, "STATUS_EXECUTING", 1, "lw-stream-agent")
        seen = _task_events_for(stream, task_id, count=2)
        assert [(e.event_type, e.task.status.status) for e in seen] == [
            ("EVENT_TYPE_CREATED", "STATUS_CREATED"),
            ("EVENT_TYPE_UPDATE", "STATUS_EXECUTING"),
        ]
        assert seen[1].task.last_updated_by.system.entity_id == "lw-stream-agent"
    finally:
        stream.close()
        if task_id:
            _status(lattice, task_id, "STATUS_DONE_OK", 2, "lw-stream-agent")


def test_task_stream_filters(lattice):
    task_id = _create_unassigned_task(lattice).version.task_id
    try:
        kept = _stream_task_events(
            lattice,
            heartbeat_interval_ms=100,
            status_filter=TaskStreamRequestStatusFilter(statuses=["STATUS_CREATED"]),
            task_type=TaskStreamRequestTaskTypeTaskTypePrefix(
                task_type_prefix="type.googleapis.com/anduril.tasks.v2."
            ),
        )
        assert any(e.event == "task_event" and e.task_event.task.version.task_id == task_id for e in kept)

        wrong_type = _stream_task_events(
            lattice,
            heartbeat_interval_ms=100,
            task_type=TaskStreamRequestTaskTypeTaskTypePrefix(
                task_type_prefix="type.googleapis.com/anduril.tasks.v2.Nonexistent"
            ),
        )
        assert not [e for e in wrong_type if e.event == "task_event"]

        excluded = _stream_task_events(
            lattice,
            heartbeat_interval_ms=100,
            status_filter=TaskStreamRequestStatusFilter(
                statuses=["STATUS_CREATED"], filter_type="FILTER_TYPE_EXCLUSIVE"
            ),
        )
        assert not [
            e for e in excluded if e.event == "task_event" and e.task_event.task.version.task_id == task_id
        ]

        later = _stream_task_events(
            lattice,
            heartbeat_interval_ms=100,
            update_start_time=(ent.now_utc() + timedelta(days=1)).isoformat(),
        )
        assert not [e for e in later if e.event == "task_event"]
    finally:
        _status(lattice, task_id, "STATUS_DONE_OK", 1, "lw-stream-agent")


def test_task_stream_rejects_bad_requests(lattice):
    with pytest.raises(BadRequestError) as info:
        list(
            lattice.tasks.stream_tasks(
                parent_task_id="lw-parent-1234",
                status_filter=TaskStreamRequestStatusFilter(statuses=["STATUS_CREATED"]),
            )
        )
    assert "mutually exclusive" in info.value.body["message"]
    with pytest.raises(BadRequestError) as info:
        list(lattice.tasks.stream_tasks(rate_limit=100))
    assert "250" in info.value.body["message"]


# ---------------------------------------------------------------- auth
def test_requires_bearer(lattice_env):
    resp = httpx.get(f"{lattice_env.base_url}/api/v1/entities/x")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


def test_sandbox_header_and_oauth(monkeypatch):
    with start_harness(0, sandbox=True, long_poll_timeout_s=1.0) as h:
        no_sbx = make_client(LatticeSettings(h.base_url, "test-token", None, None, None))
        with pytest.raises(UnauthorizedError):
            no_sbx.entities.get_entity("x")
        with_sbx = make_client(LatticeSettings(h.base_url, "test-token", "sbx-token", None, None))
        eid = _uid("lw-firms")
        with_sbx.entities.publish_entity(**_track_kwargs(eid))
        oauth = make_client(LatticeSettings(h.base_url, None, "sbx-token", "client-id", "client-secret"))
        assert oauth.entities.get_entity(eid).entity_id == eid
