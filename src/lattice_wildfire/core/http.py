"""Polite HTTP fetching with an on-disk last-good cache, plus ArcGIS REST paging.

Every fetch is real. A fresh-enough cache entry (``ttl_s``) is served without a request; on network or
parse failure the last good response is returned marked ``stale=True``. With no cache entry the failure
propagates as :class:`FetchError`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

import httpx

from .config import AOI

USER_AGENT = "lattice-wildfire/0.1 (tanner@meshvault.ai)"
TIMEOUT_S = 30.0
MAX_ARCGIS_PAGES = 200

T = TypeVar("T")


class FetchError(RuntimeError):
    """Network or parse failure with no cached copy to fall back on."""


@dataclass(frozen=True)
class Fetched(Generic[T]):
    data: T
    fetched_at: datetime
    from_cache: bool
    stale: bool


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
        return _client


def cache_root(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    raw = cache_dir or os.environ.get("LW_CACHE_DIR") or "~/.cache/lattice-wildfire"
    return Path(raw).expanduser()


def _cache_path(root: Path, url: str, params: Mapping[str, Any] | None, kind: str) -> Path:
    key_src = json.dumps([kind, url, sorted((str(k), str(v)) for k, v in (params or {}).items())])
    return root / "http" / f"{hashlib.sha256(key_src.encode()).hexdigest()}.json"


def _read_cache(path: Path) -> tuple[str, datetime] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return record["body"], datetime.fromisoformat(record["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(
    path: Path, url: str, params: Mapping[str, Any] | None, body: str, fetched_at: datetime
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "url": url,
        "params": {str(k): str(v) for k, v in (params or {}).items()},
        "fetched_at": fetched_at.isoformat(),
        "body": body,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fetch(
    url: str,
    params: Mapping[str, Any] | None,
    ttl_s: float,
    cache_dir: str | os.PathLike[str] | None,
    kind: str,
    parse: Callable[[str], T],
) -> Fetched[T]:
    path = _cache_path(cache_root(cache_dir), url, params, kind)
    cached = _read_cache(path)
    now = datetime.now(UTC)
    if cached is not None and ttl_s > 0 and (now - cached[1]).total_seconds() < ttl_s:
        try:
            return Fetched(parse(cached[0]), cached[1], from_cache=True, stale=False)
        except Exception:  # noqa: BLE001 - a corrupt cache entry is simply refetched
            cached = None
    try:
        resp = _http().get(url, params=dict(params) if params else None)
        resp.raise_for_status()
        body = resp.text
        data = parse(body)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to last good
        if cached is not None:
            try:
                return Fetched(parse(cached[0]), cached[1], from_cache=True, stale=True)
            except Exception:  # noqa: BLE001
                pass
        raise FetchError(f"fetch failed for {url}: {type(exc).__name__}: {exc}") from exc
    fetched_at = datetime.now(UTC)
    _write_cache(path, url, params, body, fetched_at)
    return Fetched(data, fetched_at, from_cache=False, stale=False)


def fetch_json(
    url: str,
    params: Mapping[str, Any] | None = None,
    ttl_s: float = 300,
    cache_dir: str | os.PathLike[str] | None = None,
    *,
    validate: Callable[[Any], None] | None = None,
) -> Fetched[Any]:
    """GET ``url`` and parse JSON. ``validate`` may raise to reject a 200 body (it is then not cached)."""

    def parse(body: str) -> Any:
        data = json.loads(body)
        if validate is not None:
            validate(data)
        return data

    return _fetch(url, params, ttl_s, cache_dir, "json", parse)


def fetch_text(
    url: str,
    params: Mapping[str, Any] | None = None,
    ttl_s: float = 300,
    cache_dir: str | os.PathLike[str] | None = None,
) -> Fetched[str]:
    return _fetch(url, params, ttl_s, cache_dir, "text", lambda body: body)


class ArcGISError(ValueError):
    """ArcGIS answered HTTP 200 with an ``error`` object."""


def _arcgis_validate(data: Any) -> None:
    if not isinstance(data, dict):
        raise ArcGISError("ArcGIS response is not a JSON object")
    if "error" in data:
        err = data["error"] or {}
        raise ArcGISError(
            f"ArcGIS error {err.get('code')}: {err.get('message')} {err.get('details') or ''}".strip()
        )
    if data.get("type") != "FeatureCollection":
        raise ArcGISError("ArcGIS response is not a GeoJSON FeatureCollection")


def arcgis_query(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    geometry_bbox: AOI | tuple[float, float, float, float] | None = None,
    *,
    point: tuple[float, float] | None = None,
    return_geometry: bool = True,
    page_size: int = 1000,
    order_by: str | None = None,
    max_allowable_offset: float | None = None,
    ttl_s: float = 300,
    cache_dir: str | os.PathLike[str] | None = None,
) -> Fetched[dict]:
    """Query an ArcGIS feature/map-service layer as GeoJSON, following ``resultOffset`` paging.

    ``geometry_bbox`` is an :class:`AOI` or an ArcGIS envelope ``(min_lon, min_lat, max_lon, max_lat)``.
    ``point`` is ``(lat, lon)`` for a point-intersects query. Coordinates are WGS84 in and out.
    ``max_allowable_offset`` is ArcGIS ``maxAllowableOffset``: the generalization (simplification)
    distance in ``outSR`` units, so degrees for the 4326 output every layer here returns.
    Returns one merged FeatureCollection; ``from_cache`` is true only if every page came from cache and
    ``stale`` if any page did.
    """
    if max_allowable_offset is not None and max_allowable_offset <= 0:
        raise ValueError("max_allowable_offset must be a positive number of output-SR units")
    base: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": 4326,
        "f": "geojson",
    }
    if geometry_bbox is not None and point is not None:
        raise ValueError("pass geometry_bbox or point, not both")
    if geometry_bbox is not None:
        env = geometry_bbox.envelope() if isinstance(geometry_bbox, AOI) else tuple(geometry_bbox)
        base.update(
            geometry=",".join(str(v) for v in env),
            geometryType="esriGeometryEnvelope",
            inSR=4326,
            spatialRel="esriSpatialRelIntersects",
        )
    if point is not None:
        base.update(
            geometry=f"{point[1]},{point[0]}",
            geometryType="esriGeometryPoint",
            inSR=4326,
            spatialRel="esriSpatialRelIntersects",
        )
    if order_by:
        base["orderByFields"] = order_by
    if max_allowable_offset is not None:
        base["maxAllowableOffset"] = max_allowable_offset

    features: list[dict] = []
    fetched_ats: list[datetime] = []
    all_cached = True
    any_stale = False
    offset = 0
    url = layer_url.rstrip("/") + "/query"
    for _ in range(MAX_ARCGIS_PAGES):
        page = fetch_json(
            url,
            {**base, "resultOffset": offset, "resultRecordCount": page_size},
            ttl_s=ttl_s,
            cache_dir=cache_dir,
            validate=_arcgis_validate,
        )
        fetched_ats.append(page.fetched_at)
        all_cached &= page.from_cache
        any_stale |= page.stale
        page_features = page.data.get("features") or []
        features.extend(page_features)
        exceeded = page.data.get("exceededTransferLimit") or (page.data.get("properties") or {}).get(
            "exceededTransferLimit"
        )
        if not exceeded or not page_features:
            break
        offset += len(page_features)
    else:
        raise FetchError(f"ArcGIS paging exceeded {MAX_ARCGIS_PAGES} pages for {layer_url}")
    return Fetched(
        {"type": "FeatureCollection", "features": features},
        min(fetched_ats),
        from_cache=all_cached,
        stale=any_stale,
    )
