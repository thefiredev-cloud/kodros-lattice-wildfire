"""Publish cycle: one rejected entity or one failing feed must not hide the rest; ``run`` exit code."""

from __future__ import annotations

import io
import json
from collections import Counter
from types import SimpleNamespace

from anduril.core.api_error import ApiError

from lattice_wildfire.core.config import AOI
from lattice_wildfire.feeds.publisher import publish_batch, run, run_cycle
from lattice_wildfire.feeds.sources import COLLECTORS, Batch

AOI_ANY = AOI(min_lat=33.70, min_lon=-118.95, max_lat=34.82, max_lon=-117.65)


class FakeEntities:
    """Stands in for ``client.entities``: records accepted ids, raises the mapped ApiError for rejected ids."""

    def __init__(self, reject: dict[str, ApiError] | None = None):
        self.reject = reject or {}
        self.published: list[str] = []

    def publish_entity(self, **payload):
        entity_id = payload["entity_id"]
        if entity_id in self.reject:
            raise self.reject[entity_id]
        self.published.append(entity_id)


def fake_client(entities: FakeEntities):
    return SimpleNamespace(entities=entities)


def test_publish_batch_rejected_entity_is_reported_and_later_entities_still_publish():
    entities = FakeEntities(reject={"lw-a": ApiError(status_code=409, body="duplicate")})
    batch = Batch("firms", 2, [{"entity_id": "lw-a"}, {"entity_id": "lw-b"}], Counter())

    report = publish_batch(fake_client(entities), batch)

    assert entities.published == ["lw-b"]
    assert report.published == 1 and report.selected == 2
    assert report.errors == ["lw-a: HTTP 409 duplicate"]


def test_run_cycle_failing_collector_is_reported_and_later_sources_still_run():
    # Names come from PUBLICATION_ORDER: the failing context layer runs before the hotspot source.
    def broken(aoi, ttl_s):
        raise RuntimeError("feed down")

    def healthy(aoi, ttl_s):
        return Batch("firms", 1, [{"entity_id": "lw-firms-1"}])

    entities = FakeEntities()
    reports = run_cycle(
        fake_client(entities),
        ["perimeters", "firms"],
        AOI_ANY,
        60,
        collectors={"perimeters": broken, "firms": healthy},
    )

    assert [r.source for r in reports] == ["perimeters", "firms"]
    assert reports[0].errors == ["collect failed: RuntimeError: feed down"]
    assert reports[0].received == 0 and reports[0].published == 0
    assert reports[1].errors == [] and reports[1].published == 1
    assert entities.published == ["lw-firms-1"]


def test_run_once_exits_nonzero_only_when_a_source_reported_errors(monkeypatch):
    def ok_feed(aoi, ttl_s):
        return Batch("ok-feed", 0, [])

    def bad_feed(aoi, ttl_s):
        raise ConnectionError("feed unreachable")

    monkeypatch.setitem(COLLECTORS, "ok-feed", ok_feed)
    monkeypatch.setitem(COLLECTORS, "bad-feed", bad_feed)

    assert (
        run(
            None,
            ["ok-feed"],
            AOI_ANY,
            interval_s=600,
            once=True,
            out=io.StringIO(),
            report_out=io.StringIO(),
        )
        == 0
    )

    report = io.StringIO()
    code = run(
        None,
        ["ok-feed", "bad-feed"],
        AOI_ANY,
        interval_s=600,
        once=True,
        out=io.StringIO(),
        report_out=report,
    )

    assert code == 1
    summary = json.loads(report.getvalue())
    assert [s["source"] for s in summary["sources"]] == ["ok-feed", "bad-feed"]
    assert summary["sources"][1]["errors"] == ["collect failed: ConnectionError: feed unreachable"]
