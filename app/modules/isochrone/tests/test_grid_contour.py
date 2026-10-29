"""Offline tests for the grid time field + marching-squares contour."""

import math

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon

from app.modules.isochrone.grid import (
    GridField,
    field_to_geometry,
    field_to_lnglat_geometry,
    make_radial_field,
)
from app.schemas.common import CenterPoint

CENTER = CenterPoint(lng=121.508602, lat=31.288268, crs="BD09LL")


def _geometry_polygons(geometry):
    if geometry is None:
        return []
    return list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]


def test_smooth_radial_field_matches_analytic_circle() -> None:
    # 1.4 m/s walking speed -> 1260 m in 900 s.
    field = make_radial_field(CENTER, radius_m=1800, cell_m=10, seconds_per_m=1 / 1.4)
    geometry = field_to_geometry(field)
    assert geometry is not None
    assert geometry.is_valid
    expected = math.pi * 1260.0 ** 2
    assert abs(geometry.area - expected) / expected < 0.10
    assert geometry.contains(Point(0, 0))
    assert not geometry.contains(Point(1700, 0))


def test_non_monotonic_detour_is_kept_not_replaced_by_first_crossing() -> None:
    # 21x21 grid, 100 m cells, local origin at -1000 m.
    times = np.full((21, 21), 600.0)
    # A near point is blocked (1100 s) while a farther detour point is reachable.
    times[10, 14] = 1100.0  # (x=400, y=0)
    times[13, 16] = 850.0   # (x=600, y=300)
    field = GridField(times=times, origin_x_m=-1000, origin_y_m=-1000, cell_m=100, center=CENTER)
    geometry = field_to_geometry(field)
    assert geometry is not None and geometry.is_valid
    assert not geometry.contains(Point(400, 0))
    assert geometry.contains(Point(600, 300))


def test_enclosed_high_time_patch_becomes_a_hole() -> None:
    field = make_radial_field(CENTER, radius_m=1500, cell_m=10, seconds_per_m=1 / 1.4)
    times = field.times.copy()
    rows, cols = times.shape
    cx, cy = rows // 2, cols // 2
    times[cx - 8: cx + 9, cy - 8: cy + 9] = 1200.0
    holed = GridField(times=times, origin_x_m=field.origin_x_m, origin_y_m=field.origin_y_m,
                      cell_m=field.cell_m, center=CENTER)
    geometry = field_to_geometry(holed)
    assert geometry is not None and geometry.is_valid
    assert geometry.contains(Point(1000, 0))
    assert not geometry.contains(Point(0, 0))
    assert any(poly.interiors for poly in _geometry_polygons(geometry))


def test_unknown_patch_is_not_filled_as_reachable() -> None:
    field = make_radial_field(
        CENTER, radius_m=1500, cell_m=10, seconds_per_m=1 / 1.4,
        unknown_mask=lambda x, y: -300 < x < 300 and -300 < y < 300,
    )
    geometry = field_to_geometry(field)
    assert geometry is not None and geometry.is_valid
    assert not geometry.contains(Point(0, 0))
    geojson = field_to_lnglat_geometry(field)
    assert geojson is not None
    assert geojson["unknown_points"] > 0
    assert geojson["unknown_ratio"] > 0
    assert geojson["algorithm_version"] == "grid-marching-v1"


def test_saddle_case_does_not_crash_and_stays_valid() -> None:
    rows = cols = 11
    times = np.indices((rows, cols)).sum(axis=0).astype(float) * 100.0
    # Checkerboard perturbation creates many ambiguous cells.
    times[::2, ::2] += 400.0
    times[1::2, 1::2] += 400.0
    field = GridField(times=times, origin_x_m=0, origin_y_m=0, cell_m=100, center=CENTER)
    geometry = field_to_geometry(field)
    assert geometry is None or geometry.is_valid
