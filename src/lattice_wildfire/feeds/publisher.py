"""Publish cycle and heartbeat loop. Network and Lattice I/O live here and in ``sources.collect_*``."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from anduril import Entity, Lattice
from anduril.core.api_error import ApiError
from anduril.core.jsonable_encoder import jsonable_encoder

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import now_utc

from .sources import COLLECTORS, Batch, Payload

log = logging.getLogger("lattice_wildfire.feeds")

# Context layers publish before hotspots. Fusion's first cycle then already has incidents,
# perimeters, warnings and stations to compare a fresh detection against; without this order
# an early cycle can call a real new start "unassociated" and publish it as a candidate.
CONTEXT_SOURCES = ("perimeters", "wfigs", "calfire", "redflag", "stations")
HOTSPOT_SOURCES = ("firms",)
PUBLICATION_ORDER = (*CONTEXT_SOURCES, *HOTSPOT_SOURCES)
# Readiness marker, relative to `$LW_STATE_DIR`: `lw-run` holds fusion and the planner until
# feeds has completed one cycle in which every selected source finished without error.
READY_MARKER = Path("feeds") / "ready.json"


def publication_order(sources: Sequence[str]) -> list[str]:
    """The selected sources in publish order: every context layer first, hotspots last."""
    wanted = list(dict.fromkeys(sources))
    ordered = [name for name in PUBLICATION_ORDER if name in wanted]
    return ordered + [name for name in wanted if name not in PUBLICATION_ORDER]


def ready_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """`$LW_STATE_DIR/feeds/ready.json`, or None when `LW_STATE_DIR` is not set."""
    env = os.environ if environ is None else environ
    base = (env.get("LW_STATE_DIR") or "").strip()
    return Path(base).expanduser() / READY_MARKER if base else None


def ready_document(reports: Sequence[SourceReport]) -> dict[str, Any]:
    """Marker body: publish counts and cache staleness per source of one clean cycle."""
    return {
        "at": now_utc().isoformat().replace("+00:00", "Z"),
        "sources": {
            r.source: {"published": r.published, "stale": r.stale}  # None: the source makes no fetch
            for r in reports
        },
    }


def write_ready(path: Path, reports: Sequence[SourceReport]) -> None:
    """Write the readiness marker atomically (temp file in the same directory, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ready_document(reports), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def wire_payload(payload: Payload) -> dict[str, Any]:
    """``publish_entity`` kwargs -> the camelCase JSON body the SDK sends (``None`` fields omitted)."""
    return jsonable_encoder(Entity(**payload))


@dataclass
class SourceReport:
    source: str
    received: int = 0
    selected: int = 0
    published: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    stale: bool | None = None
    from_cache: bool | None = None
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "received": self.received,
            "selected": self.selected,
            "published": self.published,
            "skipped": self.skipped,
            "stale": self.stale,
            "from_cache": self.from_cache,
            "errors": self.errors,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def publish_batch(client: Lattice | None, batch: Batch, out: IO[str] | None = None) -> SourceReport:
    """Publish every payload (``client=None`` = dry run: write wire JSON lines to ``out``)."""
    report = SourceReport(
        source=batch.source,
        received=batch.received,
        selected=len(batch.payloads),
        skipped=dict(sorted(batch.skipped.items())),
        stale=batch.stale if batch.fetches else None,
        from_cache=any(f.from_cache for f in batch.fetches) if batch.fetches else None,
    )
    for payload in batch.payloads:
        if client is None:
            if out is not None:
                out.write(
                    json.dumps({"source": batch.source, "entity": wire_payload(payload)}, sort_keys=True)
                    + "\n"
                )
            continue
        try:
            client.entities.publish_entity(**payload)
        except ApiError as exc:
            report.errors.append(f"{payload['entity_id']}: HTTP {exc.status_code} {exc.body}")
            continue
        report.published += 1
    return report


def run_cycle(
    client: Lattice | None,
    sources: Sequence[str],
    aoi: AOI,
    ttl_s: int,
    out: IO[str] | None = None,
    collectors: dict[str, Callable[[AOI, int], Batch]] = COLLECTORS,
) -> list[SourceReport]:
    """One fetch + publish pass over ``sources``. A failing source is reported, not fatal."""
    reports = []
    for name in publication_order(sources):
        started = time.monotonic()
        try:
            batch = collectors[name](aoi, ttl_s)
        except Exception as exc:  # one broken feed must not stop the others
            log.exception("source %s failed", name)
            reports.append(
                SourceReport(
                    source=name,
                    errors=[f"collect failed: {type(exc).__name__}: {exc}"],
                    elapsed_s=time.monotonic() - started,
                )
            )
            continue
        report = publish_batch(client, batch, out)
        report.elapsed_s = time.monotonic() - started
        reports.append(report)
    return reports


def cycle_summary(reports: list[SourceReport], dry_run: bool) -> dict[str, Any]:
    return {
        "at": now_utc().isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
        "sources": [r.as_dict() for r in reports],
    }


def run(
    client: Lattice | None,
    sources: Sequence[str],
    aoi: AOI,
    *,
    interval_s: int,
    once: bool,
    out: IO[str] = sys.stdout,
    report_out: IO[str] = sys.stderr,
    collectors: dict[str, Callable[[AOI, int], Batch]] = COLLECTORS,
) -> int:
    """Heartbeat loop: refetch and republish every ``interval_s`` so expiries keep rolling.

    Publishes the readiness marker after the first cycle in which every selected source
    finished without error (a stale cache still counts as finished). A dry run publishes
    nothing, so it never writes the marker and other apps never gate on it.

    Returns 0 when the last cycle had no errors, else 1.
    """
    ttl_s = max(30, min(240, interval_s))
    marker = ready_path()
    while True:
        started = time.monotonic()
        reports = run_cycle(client, sources, aoi, ttl_s, out if client is None else None, collectors)
        report_out.write(json.dumps(cycle_summary(reports, client is None)) + "\n")
        report_out.flush()
        failed = any(r.errors for r in reports)
        if marker is not None and client is not None and not failed and not marker.exists():
            try:
                write_ready(marker, reports)
                log.info("ready marker written: %s", marker)
            except OSError as exc:  # a bookkeeping failure must not stop the heartbeat
                log.warning("could not write %s: %s", marker, exc)
        if once:
            return 1 if failed else 0
        time.sleep(max(0.0, interval_s - (time.monotonic() - started)))
