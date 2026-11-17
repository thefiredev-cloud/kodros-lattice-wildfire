"""Feed geometry: AOI overlap and largest-part ranking decide what is published and which part keeps the id."""

from __future__ import annotations

from lattice_wildfire.core.config import AOI
from lattice_wildfire.feeds.geometry import part_intersects_aoi, ranked_parts, ring_bbox

AOI_TEST = AOI(min_lat=34.0, min_lon=-119.0, max_lat=35.0, max_lon=-118.0)


def square(lat0: float, lon0: float, size: float) -> list[tuple[float, float]]:
    """Closed (lat, lon) ring around an axis-aligned square of side ``size``."""
    return [(lat0, lon0), (lat0, lon0 + size), (lat0 + size, lon0 + size), (lat0 + size, lon0), (lat0, lon0)]


def test_part_sharing_only_an_aoi_edge_counts_as_overlap():
    # Exterior east edge at lon -119.0 is exactly the AOI west edge.
    assert part_intersects_aoi([square(34.5, -119.5, 0.5)], AOI_TEST)


def test_part_entirely_west_of_aoi_does_not_intersect():
    # East edge at lon -119.25 lies strictly west of the AOI west edge (-119.0).
    assert not part_intersects_aoi([square(34.5, -119.5, 0.25)], AOI_TEST)


def test_part_without_exterior_ring_does_not_intersect():
    assert not part_intersects_aoi([], AOI_TEST)
    assert not part_intersects_aoi([[]], AOI_TEST)


def test_ranked_parts_orders_by_exterior_minus_holes_and_drops_degenerates():
    medium = [square(0.0, 0.0, 3.0)]  # net area 9
    # 4x4 exterior (16) minus a 3x3 hole (9): net 7, so it ranks below the 3x3 square despite the larger exterior.
    holed = [square(0.0, 0.0, 4.0), square(0.5, 0.5, 3.0)]
    small = [square(0.0, 0.0, 1.0)]  # net area 1
    two_points = [[(0.0, 0.0), (1.0, 1.0)]]  # exterior too short to be a ring
    collinear = [[(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)]]  # zero-area exterior

    ranked = ranked_parts([holed, two_points, small, collinear, medium])

    extents = [round(ring_bbox(part[0])[2] - ring_bbox(part[0])[0], 6) for part in ranked]
    assert extents == [3.0, 4.0, 1.0]


def test_ranked_parts_ties_keep_source_order():
    west = [square(0.0, 0.0, 1.0)]
    east = [square(0.0, 10.0, 1.0)]

    def min_lons(parts):
        return [ring_bbox(part[0])[1] for part in parts]

    assert min_lons(ranked_parts([west, east])) == [0.0, 10.0]
    assert min_lons(ranked_parts([east, west])) == [10.0, 0.0]
