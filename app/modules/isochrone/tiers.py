"""Stage 2A: 5/10/15-minute cumulative isochrones and non-overlapping bands.

One measured ``GridField`` is contoured at 300/600/900 s. The three cumulative
sets are strictly nested (as long as the field is a single monotone time
surface); the *display* bands are the set differences, so every location belongs
to exactly one tier:

===========  =====================  ==============================
展示档位      时间条件                几何
===========  =====================  ==============================
5 分钟       0–300 s（含 300 s）     cumulative(300)
10 分钟      >300–600 s             cumulative(600) − cumulative(300)
15 分钟      >600–900 s             cumulative(900) − cumulative(600)
未知          无法可靠判断            unknown mask（NaN 网格点，单独报告）
===========  =====================  ==============================

Unknown grid points (``NaN``) are never interpolated. In the contour pass they
behave as above-limit values (see ``grid._padded_field``), so they stay outside
every cumulative set. They are additionally reported through an explicit
unknown mask/ratio so a report can render them as "unknown" instead of
"outside 15 minutes".

The geometry stays in the field's local metre plane; ``*_geojson`` helpers
return BD09LL GeoJSON-like dicts for the map/report layer. This module only does
offline geometry: it does not call any route provider and does not read files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from app.modules.isochrone.grid import EARTH_RADIUS_M, GridField, field_to_geometry

#: Version of the three-tier extraction contract (cumulative + bands + unknown).
ALGORITHM_VERSION = "grid-tiers-v1"
#: Version of the underlying single-threshold contour implementation.
CONTOUR_VERSION = "grid-marching-v1"
#: Default thresholds (5, 10, 15 minutes). Tiers are ordered inner -> outer.
DEFAULT_THRESHOLDS_S: tuple[int, int, int] = (300, 600, 900)
#: Label returned by the classifier for a point with no usable measurement.
UNKNOWN_LABEL = "unknown"
#: Label returned by the classifier for a measured point outside the outer tier.
OUTSIDE_LABEL = "outside"

Geometry = Polygon | MultiPolygon

# Tolerance for "the same set" checks; local metre-plane areas are in m².
_AREA_TOL_M2 = 1e-6


@dataclass(frozen=True, slots=True)
class TierBand:
    """One display tier: a cumulative range plus its non-overlapping band."""

    label: str
    from_s: int
    to_s: int
    cumulative: Geometry
    band: Geometry

    @property
    def cumulative_area_m2(self) -> float:
        return float(self.cumulative.area)

    @property
    def band_area_m2(self) -> float:
        return float(self.band.area)

    def contains_xy(self, x_m: float, y_m: float) -> bool:
        return bool(self.band.covers(Point(x_m, y_m)))


@dataclass(frozen=True, slots=True, eq=False)
class TieredIsochrone:
    """Result of contouring one field at several time thresholds.

    Attributes are intentionally plain so stage 3 can either use the helpers or
    serialise the whole thing with :meth:`to_dict` and store it in a report.
    """

    field: GridField
    thresholds_s: tuple[int, ...]
    tiers: tuple[TierBand, ...]
    unknown_mask: np.ndarray
    unknown_geometry: Geometry
    algorithm_version: str = ALGORITHM_VERSION
    checks: dict[str, Any] = field(default_factory=dict, compare=False)

    # -- convenience -----------------------------------------------------
    @property
    def outer(self) -> Geometry:
        """Cumulative geometry of the largest threshold (15 min by default)."""
        return self.tiers[-1].cumulative

    @property
    def cumulative(self) -> dict[int, Geometry]:
        return {tier.to_s: tier.cumulative for tier in self.tiers}

    @property
    def bands(self) -> dict[str, Geometry]:
        return {tier.label: tier.band for tier in self.tiers}

    @property
    def unknown_points(self) -> int:
        return int(self.unknown_mask.sum())

    @property
    def measured_points(self) -> int:
        return int(self.unknown_mask.size - self.unknown_points)

    @property
    def unknown_ratio(self) -> float:
        return self.unknown_points / self.unknown_mask.size

    def cumulative_at(self, threshold_s: int) -> Geometry:
        for tier in self.tiers:
            if tier.to_s == threshold_s:
                return tier.cumulative
        raise KeyError(f"未定义的时间阈值: {threshold_s}")

    def band_for_label(self, label: str) -> Geometry:
        for tier in self.tiers:
            if tier.label == label:
                return tier.band
        raise KeyError(f"未知档位标签: {label}")

    # -- point classification -------------------------------------------
    def coverage_of_local_xy(self, x_m: float, y_m: float) -> bool:
        """True if the containing grid cell has four measured corners.

        An unknown point (or a point outside the measured grid) is *not* inside
        coverage: the field cannot classify it as inside or outside a tier.
        """
        cell = self._cell_of(x_m, y_m)
        if cell is None:
            return False
        row, col = cell
        return bool(np.isfinite(self.field.times[row: row + 2, col: col + 2]).all())

    def classify_local_xy(self, x_m: float, y_m: float) -> str:
        """Return tier label, ``unknown`` or ``outside`` for a local-metre point.

        ``unknown`` means the point falls in a cell with an unmeasured corner or
        outside the measured grid -- it must not be counted as "outside 15
        minutes". Inside coverage, points on a shared band boundary go to the
        *inner* tier (0–300 s is inclusive of 300 s).
        """
        if not self.coverage_of_local_xy(x_m, y_m):
            return UNKNOWN_LABEL
        point = Point(x_m, y_m)
        for tier in self.tiers:
            if tier.band.covers(point):
                return tier.label
        return OUTSIDE_LABEL

    def classify_lnglat(self, lng: float, lat: float) -> str:
        x, y = lnglat_to_local_xy(self.field, lng, lat)
        return self.classify_local_xy(x, y)

    def _cell_of(self, x_m: float, y_m: float) -> tuple[int, int] | None:
        field_ = self.field
        col = math.floor((x_m - field_.origin_x_m) / field_.cell_m)
        row = math.floor((y_m - field_.origin_y_m) / field_.cell_m)
        if 0 <= row < field_.rows - 1 and 0 <= col < field_.cols - 1:
            return row, col
        return None

    # -- serialisation ---------------------------------------------------
    def to_dict(
        self,
        *,
        include_geojson: bool = True,
        include_unknown_geometry: bool = True,
    ) -> dict[str, Any]:
        """JSON-serialisable contract for the report / stage-3 consumer."""
        field_ = self.field
        tier_dicts: list[dict[str, Any]] = []
        for tier in self.tiers:
            item: dict[str, Any] = {
                "tier_id": tier.label,
                "from_s": tier.from_s,
                "to_s": tier.to_s,
                "cumulative_area_m2": round(tier.cumulative_area_m2, 3),
                "band_area_m2": round(tier.band_area_m2, 3),
                "cumulative_holes": _ring_count(tier.cumulative),
                "band_holes": _ring_count(tier.band),
            }
            if include_geojson:
                item["cumulative_geojson"] = _geometry_to_geojson(field_, tier.cumulative)
                item["band_geojson"] = _geometry_to_geojson(field_, tier.band)
            tier_dicts.append(item)
        outer_area = float(self.outer.area)
        for item in tier_dicts:
            item["band_share_of_outer"] = (
                round(item["band_area_m2"] / outer_area, 6) if outer_area > 0 else 0.0
            )
        payload: dict[str, Any] = {
            "algorithm_version": self.algorithm_version,
            "contour_version": CONTOUR_VERSION,
            "thresholds_s": list(self.thresholds_s),
            "center": {
                "lng": float(field_.center.lng),
                "lat": float(field_.center.lat),
                "crs": field_.center.crs,
            },
            "grid": {
                "rows": field_.rows,
                "cols": field_.cols,
                "cell_m": float(field_.cell_m),
                "origin_x_m": float(field_.origin_x_m),
                "origin_y_m": float(field_.origin_y_m),
                "time_limit_s": int(field_.time_limit_s),
                "measured_points": self.measured_points,
                "unknown_points": self.unknown_points,
                "unknown_ratio": round(self.unknown_ratio, 6),
            },
            "tiers": tier_dicts,
            "unknown": {
                "label": UNKNOWN_LABEL,
                "unknown_points": self.unknown_points,
                "measured_points": self.measured_points,
                "unknown_ratio": round(self.unknown_ratio, 6),
                "mask_cells": [
                    f"{row}:{col}"
                    for row in range(field_.rows)
                    for col in range(field_.cols)
                    if not math.isfinite(float(field_.times[row, col]))
                ],
            },
            "checks": dict(self.checks),
        }
        if include_unknown_geometry:
            payload["unknown"]["mask_geojson"] = _geometry_to_geojson(
                field_, self.unknown_geometry
            )
        return payload


def lnglat_to_local_xy(field: GridField, lng: float, lat: float) -> tuple[float, float]:
    """Inverse of :meth:`GridField.xy_to_lnglat` (same local-plane constants)."""
    cosine = max(1e-6, math.cos(math.radians(field.center.lat)))
    return (
        math.radians(lng - field.center.lng) * EARTH_RADIUS_M * cosine,
        math.radians(lat - field.center.lat) * EARTH_RADIUS_M,
    )


def _polygonal(geometry: Geometry) -> Geometry:
    """Return only polygonal parts of a shapely result (never None)."""
    if geometry.is_empty:
        return Polygon()
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    parts = [
        part
        for part in getattr(geometry, "geoms", ())
        if isinstance(part, (Polygon, MultiPolygon)) and not part.is_empty
    ]
    return unary_union(parts) if parts else Polygon()


def _ring_count(geometry: Geometry) -> int:
    polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else (geometry,)
    return sum(len(polygon.interiors) for polygon in polygons)


def _tier_label(index: int, thresholds: tuple[int, ...]) -> str:
    start = 0 if index == 0 else thresholds[index - 1]
    return f"{start}-{thresholds[index]}"


def _unknown_geometry(field: GridField) -> Geometry:
    """Union of half-cell squares around unmeasured nodes (map overlay only)."""
    half = field.cell_m / 2.0
    boxes = []
    for row in range(field.rows):
        for col in range(field.cols):
            if math.isfinite(float(field.times[row, col])):
                continue
            x, y = field.point_xy(row, col)
            boxes.append(box(x - half, y - half, x + half, y + half))
    return unary_union(boxes) if boxes else Polygon()


def _unknown_nodes_covered(cumulative: Iterable[Geometry], field: GridField) -> int:
    """Count unmeasured nodes that wrongly fall inside a cumulative geometry."""
    covered = 0
    points = [
        Point(*field.point_xy(row, col))
        for row in range(field.rows)
        for col in range(field.cols)
        if not math.isfinite(float(field.times[row, col]))
    ]
    for point in points:
        if any(geometry.covers(point) for geometry in cumulative if not geometry.is_empty):
            covered += 1
    return covered


def _geometry_to_geojson(field: GridField, geometry: Geometry) -> dict[str, Any] | None:
    if geometry is None or geometry.is_empty:
        return None
    polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else (geometry,)
    coordinates = []
    for polygon in polygons:
        rings = [polygon.exterior, *polygon.interiors]
        coordinates.append([
            [list(field.xy_to_lnglat(x, y)) for x, y in ring.coords]
            for ring in rings
        ])
    return {
        "type": "MultiPolygon" if len(coordinates) > 1 else "Polygon",
        "coordinates": coordinates[0] if len(coordinates) == 1 else coordinates,
    }


def build_tiers(
    field: GridField,
    thresholds_s: Iterable[int] = DEFAULT_THRESHOLDS_S,
) -> TieredIsochrone:
    """Contour one field at every threshold and build the nested tier bands.

    The same measured time field is used for all thresholds: a point keeps one
    measured centre-arrival time and just falls into a different tier. The
    returned ``checks`` dict records the nesting, band-partition and
    unknown-coverage facts so a caller can gate on them without redoing the
    geometry.
    """
    thresholds = tuple(int(value) for value in thresholds_s)
    if len(thresholds) < 2:
        raise ValueError("至少需要两个时间阈值才能形成档位。")
    if any(value <= 0 for value in thresholds):
        raise ValueError("时间阈值必须为正整数秒。")
    if any(later <= earlier for earlier, later in zip(thresholds, thresholds[1:])):
        raise ValueError("时间阈值必须严格递增。")

    cumulative: list[Geometry] = []
    tiers: list[TierBand] = []
    previous: Geometry | None = None
    for index, threshold in enumerate(thresholds):
        geometry = field_to_geometry(replace(field, time_limit_s=threshold))
        current = _polygonal(geometry) if geometry is not None else Polygon()
        band = current if previous is None else _polygonal(current.difference(previous))
        tiers.append(
            TierBand(
                label=_tier_label(index, thresholds),
                from_s=0 if index == 0 else thresholds[index - 1],
                to_s=threshold,
                cumulative=current,
                band=band,
            )
        )
        cumulative.append(current)
        previous = current

    # --- factual checks (reported, never silently "fixed") ------------
    violation_areas = [
        round(float(_polygonal(cumulative[index - 1].difference(cumulative[index])).area), 6)
        for index in range(1, len(cumulative))
    ]
    overlap_areas = [
        round(float(tiers[left].band.intersection(tiers[right].band).area), 6)
        for left in range(len(tiers))
        for right in range(left + 1, len(tiers))
    ]
    band_union = unary_union([tier.band for tier in tiers if not tier.band.is_empty]) \
        if any(not tier.band.is_empty for tier in tiers) else Polygon()
    outer = cumulative[-1]
    partition_diff = round(
        float(_polygonal(band_union.symmetric_difference(outer)).area), 6
    ) if not outer.is_empty else 0.0
    unknown_mask = ~np.isfinite(field.times)
    unknown_inside = _unknown_nodes_covered(cumulative, field)
    checks: dict[str, Any] = {
        "cumulative_nested": all(area <= _AREA_TOL_M2 for area in violation_areas),
        "nesting_violation_area_m2": violation_areas,
        "bands_pairwise_disjoint": all(area <= _AREA_TOL_M2 for area in overlap_areas),
        "band_pairwise_overlap_area_m2": overlap_areas,
        "bands_partition_outer": partition_diff <= _AREA_TOL_M2,
        "band_union_symmetric_difference_m2": partition_diff,
        "band_area_sum_m2": round(sum(tier.band_area_m2 for tier in tiers), 3),
        "outer_cumulative_area_m2": round(float(outer.area), 3),
        "cumulative_holes": [_ring_count(geometry) for geometry in cumulative],
        "band_holes": [_ring_count(tier.band) for tier in tiers],
        "unknown_points": int(unknown_mask.sum()),
        "unknown_nodes_covered_by_cumulative": int(unknown_inside),
        "unknown_not_filled": unknown_inside == 0,
    }
    return TieredIsochrone(
        field=field,
        thresholds_s=thresholds,
        tiers=tuple(tiers),
        unknown_mask=unknown_mask,
        unknown_geometry=_unknown_geometry(field),
        checks=checks,
    )
