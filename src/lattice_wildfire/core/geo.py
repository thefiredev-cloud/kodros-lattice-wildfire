"""Small, dependency-free geodesy helpers. Coordinates are WGS84 degrees, rings are ``[(lat, lon), ...]``."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AOI

EARTH_RADIUS_M = 6_371_008.8  # IUGG mean Earth radius
_M_PER_DEG_LAT = EARTH_RADIUS_M * math.pi / 180.0
_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

LatLon = tuple[float, float]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters on a spherical Earth."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from true north in [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def _open_ring(ring: Sequence[LatLon]) -> list[LatLon]:
    pts = [(float(lat), float(lon)) for lat, lon in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def centroid(ring: Sequence[LatLon]) -> LatLon:
    """Area-weighted centroid of a polygon ring (planar in lat/lon; fine for fire-scale polygons).

    Degenerate rings (zero area, lines, single points) fall back to the vertex mean.
    """
    pts = _open_ring(ring)
    if not pts:
        raise ValueError("centroid of an empty ring")
    area2 = cx = cy = 0.0
    n = len(pts)
    lat0, lon0 = pts[0]  # translate for numerical stability
    for i in range(n):
        y1, x1 = pts[i][0] - lat0, pts[i][1] - lon0
        y2, x2 = pts[(i + 1) % n][0] - lat0, pts[(i + 1) % n][1] - lon0
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-18:
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    return (lat0 + cy / (3.0 * area2), lon0 + cx / (3.0 * area2))


def point_in_polygon(lat: float, lon: float, ring: Sequence[LatLon]) -> bool:
    """Even-odd ray casting test. Points exactly on an edge may land either way."""
    pts = _open_ring(ring)
    inside = False
    n = len(pts)
    for i in range(n):
        lat_i, lon_i = pts[i]
        lat_j, lon_j = pts[i - 1]
        if (lat_i > lat) != (lat_j > lat):
            lon_cross = lon_i + (lat - lat_i) * (lon_j - lon_i) / (lat_j - lat_i)
            if lon < lon_cross:
                inside = not inside
    return inside


def bbox_contains(aoi: AOI, lat: float, lon: float) -> bool:
    """Inclusive bounding-box test."""
    return aoi.min_lat <= lat <= aoi.max_lat and aoi.min_lon <= lon <= aoi.max_lon


def geohash(lat: float, lon: float, precision: int = 7) -> str:
    """Standard base32 geohash."""
    if not 1 <= precision <= 12:
        raise ValueError("geohash precision must be between 1 and 12")
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out: list[str] = []
    bits = 0
    nbits = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                bits = (bits << 1) | 1
                lon_lo = mid
            else:
                bits <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                bits = (bits << 1) | 1
                lat_lo = mid
            else:
                bits <<= 1
                lat_hi = mid
        even = not even
        nbits += 1
        if nbits == 5:
            out.append(_GEOHASH_BASE32[bits])
            bits = 0
            nbits = 0
    return "".join(out)


def cluster_points(points: Sequence[LatLon], radius_m: float) -> list[list[int]]:
    """Single-linkage clusters: points closer than or equal to ``radius_m`` (transitively) share a cluster.

    Returns lists of input indices. Each cluster is sorted ascending and clusters are ordered by their
    smallest index, so the output depends only on the input, never on hashing or iteration order.
    """
    if radius_m <= 0:
        raise ValueError("radius_m must be positive")
    n = len(points)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if n == 0:
        return []
    max_abs_lat = max(abs(float(p[0])) for p in points)
    cos_lat = math.cos(math.radians(min(max_abs_lat, 90.0)))
    lat_step = radius_m / _M_PER_DEG_LAT * 1.01
    if cos_lat < 0.01 or lat_step > 10:
        # Polar or continental radius: grid bucketing stops paying off; compare every pair.
        for i in range(n):
            for j in range(i + 1, n):
                if haversine_m(points[i][0], points[i][1], points[j][0], points[j][1]) <= radius_m:
                    union(i, j)
    else:
        lon_step = lat_step / cos_lat
        grid: dict[tuple[int, int], list[int]] = {}
        cells: list[tuple[int, int]] = []
        for i, (lat, lon) in enumerate(points):
            cell = (math.floor(lat / lat_step), math.floor(lon / lon_step))
            cells.append(cell)
            grid.setdefault(cell, []).append(i)
        for i, (ci, cj) in enumerate(cells):
            lat_i, lon_i = points[i]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for j in grid.get((ci + di, cj + dj), ()):
                        if j > i and haversine_m(lat_i, lon_i, points[j][0], points[j][1]) <= radius_m:
                            union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return sorted(clusters.values(), key=lambda members: members[0])
