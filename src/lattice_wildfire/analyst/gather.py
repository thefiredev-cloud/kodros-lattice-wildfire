"""I/O edges for Kodros Analyst: NWS, LA County layers, the Lattice picture, the local model.

Each fetch is recorded as a ``SourceRecord`` (URL, fetch time, cache state, error). A failed
fetch never stops the brief: its value stays ``None`` and the record's error becomes an
explicit unknown in the facts. Network access goes through a ``Sources`` object so tests
can replay real captured responses through the same URL building and parsing code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from lattice_wildfire.core.config import AOI
from lattice_wildfire.core.entities import now_utc
from lattice_wildfire.core.http import Fetched, arcgis_query, fetch_json
from lattice_wildfire.core.layers import (
    FHSZ_URL,
    FIRE_STATIONS_URL,
    HOSPITALS_URL,
    LACOFD_ORG_NAME,
    facilities_from_geojson,
    fhsz_class_from_geojson,
)
from lattice_wildfire.core.llm import ChatResult, LLMUnavailable
from lattice_wildfire.core.llm import chat_result as cluster_chat_result

from .brief import (
    Narrative,
    build_messages,
    narrative_from_text,
    narrative_unavailable,
    render_json,
    render_markdown,
)
from .facts import (
    AnalystInputs,
    NearbyEntity,
    SourceRecord,
    Target,
    gather_facts,
    summarize_fire_entity,
)

NWS_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

POINTS_TTL_S = 24 * 3600  # gridpoint mapping is static
FORECAST_TTL_S = 600
ALERTS_TTL_S = 120
FACILITY_TTL_S = 24 * 3600
FHSZ_TTL_S = 7 * 24 * 3600

# Facility search boxes around the target (degrees): try near first, widen once if < 3 found.
SEARCH_HALF_SIZES = ((0.2, 0.25), (0.5, 0.6))
LATTICE_PICTURE_TIMEOUT_S = 20.0
# The cluster model reasons before answering and its reasoning counts against max_tokens. With the
# slim prompt a hotspot brief used 1083 tokens (727 reasoning) on 2026-11-09; 700 left it empty.
LLM_MAX_TOKENS = 2000


class Sources(Protocol):
    """Network access used by ``fetch_inputs`` (live: core.http; tests: captured files)."""

    def json(self, url: str, params: dict[str, Any] | None, ttl_s: float) -> Fetched[Any]: ...

    def arcgis(self, layer_url: str, **query: Any) -> Fetched[dict]: ...


class LiveSources:
    """Real network through core.http (polite UA, on-disk last-good cache)."""

    def json(self, url: str, params: dict[str, Any] | None, ttl_s: float) -> Fetched[Any]:
        return fetch_json(url, params, ttl_s=ttl_s)

    def arcgis(self, layer_url: str, **query: Any) -> Fetched[dict]:
        return arcgis_query(layer_url, **query)


def _full_url(url: str, params: dict[str, Any] | None) -> str:
    return f"{url}?{urlencode(params)}" if params else url


def _record(
    name: str, url: str, fetched: Fetched[Any] | None, error: Exception | str | None = None
) -> SourceRecord:
    if fetched is None:
        message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return SourceRecord(name=name, url=url, fetched_at=None, error=message[:240])
    return SourceRecord(
        name=name,
        url=url,
        fetched_at=fetched.fetched_at.isoformat(timespec="seconds"),
        from_cache=fetched.from_cache,
        stale=fetched.stale,
    )


def search_box(lat: float, lon: float, half_lat: float, half_lon: float) -> AOI:
    return AOI(
        min_lat=max(-90.0, lat - half_lat),
        min_lon=max(-180.0, lon - half_lon),
        max_lat=min(90.0, lat + half_lat),
        max_lon=min(180.0, lon + half_lon),
    )


def _facilities(
    sources: Sources, name: str, layer_url: str, kind: str, lat: float, lon: float, where: str = "1=1"
) -> tuple[list[Any] | None, SourceRecord]:
    url = layer_url.rstrip("/") + "/query"
    found: list[Any] = []
    record = _record(name, url, None, "no search performed")
    for half_lat, half_lon in SEARCH_HALF_SIZES:
        box = search_box(lat, lon, half_lat, half_lon)
        try:
            fetched = sources.arcgis(layer_url, where=where, geometry_bbox=box, ttl_s=FACILITY_TTL_S)
        except Exception as exc:
            return None, _record(name, url, None, exc)
        found = facilities_from_geojson(fetched.data, kind)
        envelope = ",".join(f"{v:g}" for v in box.envelope())
        record = _record(f"{name} (bbox {envelope})", url, fetched)
        if len(found) >= 3:
            break
    return found, record


def fetch_inputs(
    target: Target, *, sources: Sources, checkpoint: Callable[[], None] = lambda: None
) -> AnalystInputs:
    """Fetch NWS point/forecast/alerts, FHSZ and nearby facilities for ``target``.

    ``checkpoint`` runs between fetches (the task worker raises there on cancel). The
    Lattice picture is fetched separately by ``lattice_fire_entities``.
    """
    lat, lon = target.lat, target.lon
    inputs = AnalystInputs(target=target)

    points_url = NWS_POINTS_URL.format(lat=lat, lon=lon)
    try:
        fetched = sources.json(points_url, None, POINTS_TTL_S)
        inputs.nws_point = fetched.data
        inputs.sources.append(_record("NWS points", points_url, fetched))
    except Exception as exc:
        inputs.sources.append(_record("NWS points", points_url, None, exc))
    checkpoint()

    forecast_url = ((inputs.nws_point or {}).get("properties") or {}).get("forecast")
    if forecast_url:
        try:
            fetched = sources.json(forecast_url, None, FORECAST_TTL_S)
            inputs.nws_forecast = fetched.data
            inputs.sources.append(_record("NWS forecast", forecast_url, fetched))
        except Exception as exc:
            inputs.sources.append(_record("NWS forecast", forecast_url, None, exc))
    else:
        inputs.sources.append(_record("NWS forecast", points_url, None, "no forecast URL from NWS points"))
    checkpoint()

    alert_params = {"point": f"{lat:.4f},{lon:.4f}"}
    alerts_url = _full_url(NWS_ALERTS_URL, alert_params)
    try:
        fetched = sources.json(NWS_ALERTS_URL, alert_params, ALERTS_TTL_S)
        inputs.nws_alerts = fetched.data
        inputs.sources.append(_record("NWS active alerts at point", alerts_url, fetched))
    except Exception as exc:
        inputs.sources.append(_record("NWS active alerts at point", alerts_url, None, exc))
    checkpoint()

    fhsz_url = FHSZ_URL.rstrip("/") + "/query"
    try:
        fetched = sources.arcgis(
            FHSZ_URL,
            out_fields="HAZ_CLASS,HAZ_CODE,SRA",
            point=(lat, lon),
            return_geometry=False,
            ttl_s=FHSZ_TTL_S,
        )
        inputs.fhsz = fhsz_class_from_geojson(fetched.data)
        inputs.fhsz_known = True
        inputs.sources.append(_record("LA County FHSZ (LRA+SRA)", fhsz_url, fetched))
    except Exception as exc:
        inputs.sources.append(_record("LA County FHSZ (LRA+SRA)", fhsz_url, None, exc))
    checkpoint()

    inputs.stations, record = _facilities(
        sources,
        "LACoFD fire stations",
        FIRE_STATIONS_URL,
        "fire_station",
        lat,
        lon,
        where=f"org_name = '{LACOFD_ORG_NAME}'",
    )
    inputs.sources.append(record)
    checkpoint()

    inputs.hospitals, record = _facilities(
        sources, "LA County hospitals", HOSPITALS_URL, "hospital", lat, lon
    )
    inputs.sources.append(record)
    checkpoint()
    return inputs


def lattice_fire_entities(
    client: Any, *, lattice_url: str | None, timeout_s: float = LATTICE_PICTURE_TIMEOUT_S
) -> tuple[list[NearbyEntity] | None, SourceRecord]:
    """Fire-related entities already in Lattice (pre-existing stream), deduplicated by id."""
    url = f"{(lattice_url or 'lattice:').rstrip('/')}/api/v1/entities/stream"
    started = time.monotonic()
    found: dict[str, NearbyEntity] = {}
    truncated = False
    try:
        for event in client.entities.stream_entities(pre_existing_only=True, heartbeat_interval_ms=1000):
            entity = getattr(event, "entity", None)
            if entity is not None and entity.entity_id:
                if getattr(event, "event_type", None) == "EVENT_TYPE_DELETED":
                    found.pop(entity.entity_id, None)
                else:
                    summary = summarize_fire_entity(entity)
                    if summary is not None:
                        found[summary.entity_id] = summary
            if time.monotonic() - started > timeout_s:
                truncated = True
                break
    except Exception as exc:
        return None, _record("Lattice entities (pre-existing)", url, None, exc)
    name = "Lattice entities (pre-existing" + (f", cut at {timeout_s:g} s)" if truncated else ")")
    record = SourceRecord(name=name, url=url, fetched_at=now_utc().isoformat(timespec="seconds"))
    return sorted(found.values(), key=lambda e: e.entity_id), record


def write_narrative(
    facts: dict[str, Any], *, chat_result: Callable[..., ChatResult] | None = None
) -> Narrative:
    """Six-line narrative from the local cluster model, or an explicit unavailable narrative.

    ``chat_result`` is ``core.llm.chat_result`` in production; a test passes a stand-in with the same
    signature. Whatever answer comes back goes through the deterministic verifier, and its server
    model id and latency are kept for the brief's footer and JSON.
    """
    try:
        result = (chat_result or cluster_chat_result)(
            build_messages(facts), max_tokens=LLM_MAX_TOKENS, temperature=0.1
        )
    except LLMUnavailable as exc:
        return narrative_unavailable(str(exc)[:240])
    return narrative_from_text(
        result.text, facts, model=result.model, latency_s=result.latency_s, usage=result.usage
    )


@dataclass(frozen=True)
class Brief:
    facts: dict[str, Any]
    narrative: Narrative
    markdown: str
    document: dict[str, Any]


def make_brief(
    target: Target,
    *,
    task_id: str | None,
    sources: Sources,
    client: Any = None,
    lattice_url: str | None = None,
    chat_result: Callable[..., ChatResult] | None = None,
    checkpoint: Callable[[], None] = lambda: None,
) -> Brief:
    """Fetch inputs (plus the Lattice picture when ``client`` is given), gather facts, narrate, render."""
    inputs = fetch_inputs(target, sources=sources, checkpoint=checkpoint)
    if client is not None:
        inputs.nearby, record = lattice_fire_entities(client, lattice_url=lattice_url)
        inputs.sources.append(record)
        checkpoint()
    facts = gather_facts(inputs, task_id=task_id, generated_at=now_utc().isoformat(timespec="seconds"))
    narrative = write_narrative(facts, chat_result=chat_result)
    checkpoint()
    return Brief(facts, narrative, render_markdown(facts, narrative), render_json(facts, narrative))
