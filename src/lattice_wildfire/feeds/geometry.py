"""Pure polygon helpers for feed geometry: canonical orientation and AOI overlap.

Lattice accepts only canonical RFC 7946 polygons: closed rings, exterior counter-clockwise, holes
clockwise. ArcGIS (WFIGS) and api.weather.gov both deliver clockwise exteriors, so every part is
re-oriented before it is published.
"""

from __future__ import annotations

import itertools

from lattice_wildfire.core.config import AOI

from .records import PolygonPart, Ring


def close_ring(ring: Ring) -> Ring:
    if ring and ring[0] != ring[-1]:
        return [*ring, ring[0]]
    return list(ring)


def signed_area(ring: Ring) -> float:
    """Shoelace area in degree^2 with x=lon, y=lat. Positive = counter-clockwise."""
    closed = close_ring(ring)
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in itertools.pairwise(closed):
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def orient_part(part: PolygonPart) -> PolygonPart | None:
    """Close rings, exterior CCW, holes CW. Degenerate rings (<4 positions or zero area) are dropped;
    a part whose exterior is degenerate returns ``None``."""
    out: PolygonPart = []
    for index, raw in enumerate(part):
        ring = close_ring(raw)
        if len(ring) < 4:
            if index == 0:
                return None
            continue
        area = signed_area(ring)
        if area == 0.0:
            if index == 0:
                return None
            continue
        want_ccw = index == 0
        if (area > 0) != want_ccw:
            ring = ring[::-1]
        out.append(ring)
    return out


def part_area(part: PolygonPart) -> float:
    """Exterior area minus holes, degree^2 (only used to rank parts)."""
    if not part:
        return 0.0
    return abs(signed_area(part[0])) - sum(abs(signed_area(h)) for h in part[1:])


def ring_bbox(ring: Ring) -> tuple[float, float, float, float]:
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return min(lats), min(lons), max(lats), max(lons)


def part_intersects_aoi(part: PolygonPart, aoi: AOI) -> bool:
    """Bounding-box overlap between the part's exterior and the AOI (edges count as overlap)."""
    if not part or not part[0]:
        return False
    min_lat, min_lon, max_lat, max_lon = ring_bbox(part[0])
    return not (
        max_lat < aoi.min_lat or min_lat > aoi.max_lat or max_lon < aoi.min_lon or min_lon > aoi.max_lon
    )


def ranked_parts(parts: list[PolygonPart]) -> list[PolygonPart]:
    """Canonical parts, largest first; ties keep source order. Degenerate parts are removed."""
    oriented = [p for p in (orient_part(part) for part in parts) if p]
    return sorted(oriented, key=part_area, reverse=True)
