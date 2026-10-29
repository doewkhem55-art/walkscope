"""Offline tests for the three-tier (5/10/15 min) isochrone extraction."""

import json
import math

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon

from app.modules.isochrone.grid import GridField, field_to_geometry, make_radial_field
from app.modules.isochrone.tiers import (
    ALGORITHM_VERSION,
    CONTOUR_VERSION,
    DEFAULT_THRESHOLDS_S,
    OUTSIDE_LABEL,
    UNKNOWN_LABEL,
    TieredIsochrone,
    build_tiers,
    lnglat_to_local_xy,
)
from app.schemas.common import CenterPoint

CENTER = CenterPoint(lng=121.508602, lat=31.288268, crs="BD09LL")


def _polygons(geometry):
    if geometry is None or geometry.is_empty:
        return []
    return list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]


def _all_holes(geometry) -> int:
    return sum(len(polygon.interiors) for polygon in _polygons(geometry))


def _synthetic_field(radius_m: float = 1500, cell_m: float = 20) -> GridField:
    # 1.4 m/s walking speed: 300/600/900 s correspond to 420/840/1260 m.
    return make_radial_field(CENTER, radius_m=radius_m, cell_m=cell_m, seconds_per_m=1 / 1.4)


def test_three_tiers_are_nested_and_bands_partition_the_outer_range() -> None:
    tiers = build_tiers(_synthetic_field(radius_m=1800))
    assert isinstance(tiers, TieredIsochrone)
    assert tiers.thresholds_s == (300, 600, 900)
    assert [tier.label for tier in tiers.tiers] == ["0-300", "300-600", "600-900"]
    assert [tier.to_s for tier in tiers.tiers] == [300, 600, 900]
    assert tiers.algorithm_version == ALGORITHM_VERSION

    # Analytic circles for a smooth radial field (10% length tolerance).
    for threshold, expected_radius in ((300, 420.0), (600, 840.0), (900, 1260.0)):
        geometry = tiers.cumulative_at(threshold)
        expected = math.pi * expected_radius**2
        assert geometry.is_valid
        assert abs(geometry.area - expected) / expected < 0.10
        assert geometry.covers(Point(0, 0))

    # 5 min inside 10 min inside 15 min; never "grown" by a union fallback.
    assert tiers.cumulative_at(600).covers(tiers.cumulative_at(300))
    assert tiers.cumulative_at(900).covers(tiers.cumulative_at(600))
    assert tiers.checks["cumulative_nested"] is True
    assert tiers.checks["nesting_violation_area_m2"] == [0.0, 0.0]

    # Bands are pairwise disjoint and together equal the 15-minute range.
    for left in range(len(tiers.tiers)):
        for right in range(left + 1, len(tiers.tiers)):
            overlap = tiers.tiers[left].band.intersection(tiers.tiers[right].band)
            assert overlap.area < 1e-6
    assert tiers.checks["bands_pairwise_disjoint"] is True
    assert tiers.checks["bands_partition_outer"] is True
    assert tiers.checks["band_union_symmetric_difference_m2"] < 1e-6
    outer_area = tiers.cumulative_at(900).area
    assert abs(tiers.checks["band_area_sum_m2"] - outer_area) < 1e-3

    # A radial point belongs to exactly one tier in the expected band.
    assert tiers.classify_local_xy(0, 0) == "0-300"
    assert tiers.classify_local_xy(600, 0) == "300-600"
    assert tiers.classify_local_xy(1000, 0) == "600-900"
    assert tiers.classify_local_xy(1600, 0) == OUTSIDE_LABEL


def test_enclosed_unreachable_patch_stays_a_hole_in_every_tier() -> None:
    field = _synthetic_field(radius_m=1500)
    times = field.times.copy()
    rows, cols = times.shape
    row, col = rows // 2, cols // 2 + 30  # (x=600, y=0): inside the 600 s circle
    times[row - 4: row + 5, col - 4: col + 5] = 1200.0
    holed = GridField(
        times=times,
        origin_x_m=field.origin_x_m,
        origin_y_m=field.origin_y_m,
        cell_m=field.cell_m,
        center=CENTER,
    )
    tiers = build_tiers(holed)
    patch = Point(600, 0)
    for tier in tiers.tiers:
        assert not tier.cumulative.covers(patch)
        assert not tier.band.covers(patch)
    assert _all_holes(tiers.cumulative_at(600)) >= 1
    assert _all_holes(tiers.cumulative_at(900)) >= 1
    assert tiers.checks["unknown_not_filled"] is True
    # The surrounding reachable ring is still present, so the hole is not filled.
    assert tiers.cumulative_at(600).covers(Point(600, 300))
    assert tiers.classify_local_xy(600, 0) == OUTSIDE_LABEL


def test_patch_above_inner_threshold_lands_in_middle_band_only() -> None:
    field = _synthetic_field(radius_m=1500)
    times = field.times.copy()
    rows, cols = times.shape
    row, col = rows // 2, cols // 2 + 30  # (x=600, y=0)
    times[row - 3: row + 4, col - 3: col + 4] = 450.0  # 300 < 450 <= 600
    bumped = GridField(
        times=times,
        origin_x_m=field.origin_x_m,
        origin_y_m=field.origin_y_m,
        cell_m=field.cell_m,
        center=CENTER,
    )
    tiers = build_tiers(bumped)
    assert not tiers.cumulative_at(300).covers(Point(600, 0))
    assert tiers.cumulative_at(600).covers(Point(600, 0))
    assert tiers.band_for_label("300-600").covers(Point(600, 0))
    assert not tiers.band_for_label("0-300").covers(Point(600, 0))
    assert tiers.classify_local_xy(600, 0) == "300-600"
    # The coarse outer tiers are unchanged: 600–900 s band must not swallow it.
    assert not tiers.band_for_label("600-900").covers(Point(600, 0))


def test_unknown_patch_is_never_interpolated_into_a_tier() -> None:
    field = make_radial_field(
        CENTER,
        radius_m=1500,
        cell_m=20,
        seconds_per_m=1 / 1.4,
        unknown_mask=lambda x, y: 500 < x < 700 and -100 < y < 100,
    )
    tiers = build_tiers(field)
    assert tiers.unknown_points > 0
    assert 0 < tiers.unknown_ratio < 1
    assert tiers.checks["unknown_not_filled"] is True
    assert tiers.checks["unknown_nodes_covered_by_cumulative"] == 0
    assert not tiers.unknown_geometry.is_empty

    assert tiers.classify_local_xy(600, 0) == UNKNOWN_LABEL
    assert not any(tier.band.covers(Point(600, 0)) for tier in tiers.tiers)
    assert not any(tier.cumulative.covers(Point(600, 0)) for tier in tiers.tiers)
    # The measured centre still gets its inner tier; unknown is kept separate.
    assert tiers.classify_local_xy(0, 0) == "0-300"
    # A measured point outside the outer contour is "outside", not "unknown".
    assert tiers.classify_local_xy(1400, 0) == OUTSIDE_LABEL
    # Outside the measured grid there is no field coverage at all.
    assert tiers.classify_local_xy(0, 5000) == UNKNOWN_LABEL


def test_detour_is_kept_and_near_blocked_point_is_not_wrapped_in() -> None:
    # 21x21, 100 m cells, local origin -1000 m: a near point is blocked while a
    # farther detour point is reachable. A naive "connect adjacent boundary
    # points" outline would fold back around the blocked point; the level set
    # must not.
    times = np.full((21, 21), 600.0)
    times[10, 14] = 1100.0  # (x=400, y=0)
    times[13, 16] = 850.0   # (x=600, y=300)
    field = GridField(
        times=times, origin_x_m=-1000, origin_y_m=-1000, cell_m=100, center=CENTER
    )
    tiers = build_tiers(field)

    blocked_900 = tiers.cumulative_at(900)
    assert not blocked_900.covers(Point(400, 0))
    assert blocked_900.covers(Point(600, 300))
    assert tiers.cumulative_at(600).covers(Point(0, 0))
    assert not tiers.cumulative_at(600).covers(Point(600, 300))

    # Every measured node is classified exactly by its measured time: no
    # over-limit node is enclosed, no within-limit node is left out.
    expected_labels = []
    for row in range(field.rows):
        for col in range(field.cols):
            if row in (0, field.rows - 1) or col in (0, field.cols - 1):
                continue  # border nodes have no containing cell: reported unknown
            value = float(times[row, col])
            x, y = field.point_xy(row, col)
            expected = (
                "0-300" if value <= 300
                else "300-600" if value <= 600
                else "600-900" if value <= 900
                else OUTSIDE_LABEL
            )
            expected_labels.append((x, y, value, expected, tiers.classify_local_xy(x, y)))
    mismatches = [
        item for item in expected_labels if item[3] != item[4]
    ]
    assert mismatches == []
    # The blocked point is over 900 s and the detour point is 600–900 s only.
    assert tiers.classify_local_xy(400, 0) == OUTSIDE_LABEL
    assert tiers.classify_local_xy(600, 300) == "600-900"
    # Border nodes have no containing cell, so they are "unknown" by contract.
    assert tiers.classify_local_xy(1000, -1000) == UNKNOWN_LABEL


def test_contract_is_json_serialisable_and_stage3_ready() -> None:
    tiers = build_tiers(_synthetic_field(radius_m=1200))
    payload = tiers.to_dict()
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "grid-tiers-v1" in text
    assert payload["algorithm_version"] == ALGORITHM_VERSION
    assert payload["contour_version"] == CONTOUR_VERSION
    assert payload["thresholds_s"] == list(DEFAULT_THRESHOLDS_S)
    assert payload["center"]["crs"] == "BD09LL"
    assert payload["grid"]["cell_m"] == 20.0
    assert payload["grid"]["measured_points"] > 0
    assert payload["grid"]["unknown_points"] == len(payload["unknown"]["mask_cells"])
    assert (
        payload["grid"]["measured_points"] + payload["grid"]["unknown_points"]
        == payload["grid"]["rows"] * payload["grid"]["cols"]
    )

    labels = [item["tier_id"] for item in payload["tiers"]]
    assert labels == ["0-300", "300-600", "600-900"]
    for item in payload["tiers"]:
        assert item["band_area_m2"] > 0
        assert item["band_share_of_outer"] > 0
        assert item["cumulative_geojson"] is not None
        assert item["band_geojson"] is not None
        assert item["band_geojson"]["type"] in ("Polygon", "MultiPolygon")

    assert payload["checks"]["cumulative_nested"] is True
    assert payload["checks"]["bands_pairwise_disjoint"] is True
    assert payload["checks"]["bands_partition_outer"] is True
    assert payload["checks"]["unknown_not_filled"] is True
    assert "mask_cells" in payload["unknown"]


def test_lnglat_roundtrip_classification_matches_local_plane() -> None:
    tiers = build_tiers(_synthetic_field(radius_m=1200))
    lng, lat = tiers.field.xy_to_lnglat(600, 0)
    x, y = lnglat_to_local_xy(tiers.field, lng, lat)
    assert abs(x - 600) < 1e-6 and abs(y) < 1e-6
    assert tiers.classify_lnglat(lng, lat) == tiers.classify_local_xy(x, y)
    assert tiers.classify_lnglat(lng, lat) == "300-600"


def test_unknown_ratio_and_counts_are_consistent() -> None:
    # Fully measured small field: exactly one unknown node.
    times = np.full((11, 11), 600.0)
    times[0, 0] = np.nan
    field = GridField(
        times=times, origin_x_m=-500, origin_y_m=-500, cell_m=100, center=CENTER
    )
    tiers = build_tiers(field)
    assert tiers.unknown_points == 1
    assert tiers.measured_points == times.size - 1
    assert math.isclose(tiers.unknown_ratio, 1 / times.size)
    payload = tiers.to_dict()
    assert payload["unknown"]["mask_cells"] == ["0:0"]
    assert payload["checks"]["unknown_not_filled"] is True


def test_invalid_thresholds_are_rejected() -> None:
    field = _synthetic_field(radius_m=600, cell_m=40)
    with pytest.raises(ValueError):
        build_tiers(field, thresholds_s=(300,))
    with pytest.raises(ValueError):
        build_tiers(field, thresholds_s=(300, 300, 900))
    with pytest.raises(ValueError):
        build_tiers(field, thresholds_s=(0, 300, 600))
    with pytest.raises(ValueError):
        build_tiers(field, thresholds_s=(900, 600, 300))


def test_existing_single_threshold_contour_is_unchanged() -> None:
    # Guard the reuse contract: tiers.py must not change grid.py semantics.
    field = _synthetic_field(radius_m=1200)
    legacy = field_to_geometry(field)
    tiers = build_tiers(field)
    assert legacy is not None
    assert math.isclose(legacy.area, tiers.cumulative_at(900).area, rel_tol=1e-9)
