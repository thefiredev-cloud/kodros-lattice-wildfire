"""Seven-day FIRMS detection history, used to recognize fixed heat sources (refineries, landfills).

The history is the union of the NOAA-20, Suomi NPP and NOAA-21 7-day active-fire files. One
satellite failing (fetch or parse) leaves the history partial: the other satellites are still
used and the failure is reported. Only when every satellite fails is the history unknown.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from lattice_wildfire.core.http import FetchError, fetch_text
from lattice_wildfire.feeds.parsers import parse_firms_csv

FIRMS_CSV_ROOT = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"
NOAA20_7D_URL = f"{FIRMS_CSV_ROOT}/noaa-20-viirs-c2/csv/J1_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv"
SUOMI_NPP_7D_URL = f"{FIRMS_CSV_ROOT}/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv"
NOAA21_7D_URL = f"{FIRMS_CSV_ROOT}/noaa-21-viirs-c2/csv/J2_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv"
HISTORY_TTL_S = 3 * 3600
# Label for a history parsed from one CSV, where no satellite union exists.
SINGLE_CSV_SOURCE = "FIRMS 7-day CSV"


@dataclass(frozen=True)
class HistorySource:
    satellite: str
    url: str


# Verified live 2026-11-09 (HTTP 200, FIRMS 7-day CSVs).
FIRMS_7D_SOURCES = (
    HistorySource("NOAA-20", NOAA20_7D_URL),
    HistorySource("Suomi NPP", SUOMI_NPP_7D_URL),
    HistorySource("NOAA-21", NOAA21_7D_URL),
)


@dataclass(frozen=True)
class PastDetection:
    entity_id: str  # same id Fireline Feeds gives the detection, so a cluster can exclude itself
    lat: float
    lon: float
    acquired: datetime
    acq_date: str
    frp_mw: float


@dataclass(frozen=True)
class SourceFailure:
    satellite: str
    error: str


@dataclass(frozen=True)
class HeatHistory:
    detections: tuple[PastDetection, ...]
    fetched_at: datetime
    stale: bool
    source: str
    failures: tuple[SourceFailure, ...] = ()
    sources: tuple[str, ...] = ()  # satellites that contributed detections

    @property
    def partial(self) -> bool:
        return bool(self.failures) and bool(self.sources)


def _detections(text: str) -> tuple[PastDetection, ...]:
    return tuple(
        PastDetection(h.entity_id, h.lat, h.lon, h.acquired, h.acq_date, h.frp_mw)
        for h in parse_firms_csv(text)
    )


def history_from_csv(
    text: str, fetched_at: datetime, *, stale: bool = False, source: str = SINGLE_CSV_SOURCE
) -> HeatHistory:
    """FIRMS VIIRS CSV text -> history. Raises ValueError on a malformed file."""
    return HeatHistory(_detections(text), fetched_at, stale, source)


def merged_history(
    parts: Mapping[str, Sequence[PastDetection]],
    fetched_at: datetime,
    *,
    stale: bool = False,
    failures: Sequence[SourceFailure] = (),
) -> HeatHistory:
    """Union per-satellite detection sets into one 7-day history.

    Distinct ``acq_date`` values count across satellites: a date one satellite saw is a date, and
    three satellites seeing the same pass is still one date. Detections are de-duplicated by
    entity id, which carries the satellite, so the same pixel from two satellites stays two rows.
    ``failures`` names the satellites that could not be fetched or parsed: the history is then
    partial (``partial`` is true) and every evidence line says which satellite is missing.
    """
    detections: list[PastDetection] = []
    seen: set[str] = set()
    for rows in parts.values():
        for detection in rows:
            if detection.entity_id not in seen:
                seen.add(detection.entity_id)
                detections.append(detection)
    sources = tuple(parts)
    return HeatHistory(tuple(detections), fetched_at, stale, ", ".join(sources), tuple(failures), sources)


_memo_lock = threading.Lock()
_memo: dict[tuple[str, datetime], tuple[PastDetection, ...]] = {}


def _parsed(source: HistorySource, fetched_at: datetime, text: str) -> tuple[PastDetection, ...]:
    """Parse each satellite file once per fetch; a parse failure is a source failure."""
    key = (source.satellite, fetched_at)
    with _memo_lock:
        hit = _memo.get(key)
    if hit is not None:
        return hit
    detections = _detections(text)
    with _memo_lock:
        _memo[key] = detections
        for stale_key in [k for k in _memo if k[0] == source.satellite and k != key]:
            del _memo[stale_key]
    return detections


def firms_7d_history() -> HeatHistory:
    """Fetch (3 h cache, stale cache allowed) and merge every satellite's 7-day file.

    A satellite that cannot be fetched or parsed is recorded in ``failures`` and the rest of the
    history is still used. Raises FetchError when every satellite fails (history unknown).
    """
    parts: dict[str, tuple[PastDetection, ...]] = {}
    failures: list[SourceFailure] = []
    fetched_at: datetime | None = None
    stale = False
    for source in FIRMS_7D_SOURCES:
        try:
            fetched = fetch_text(source.url, ttl_s=HISTORY_TTL_S)
            rows = _parsed(source, fetched.fetched_at, fetched.data)
        except Exception as error:  # noqa: BLE001 - one satellite failing is partial, not fatal
            failures.append(SourceFailure(source.satellite, f"{type(error).__name__}: {error}"))
            continue
        parts[source.satellite] = rows
        fetched_at = fetched.fetched_at if fetched_at is None else min(fetched_at, fetched.fetched_at)
        stale = stale or fetched.stale
    if fetched_at is None:
        raise FetchError(
            "7-day FIRMS history unavailable for every satellite: "
            + "; ".join(f"{f.satellite}: {f.error}" for f in failures)
        )
    return merged_history(parts, fetched_at, stale=stale, failures=failures)
