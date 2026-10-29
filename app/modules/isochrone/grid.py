"""Grid travel-time field to 900 s contour (offline prototype / stage-2 core).

The field is a regular grid of measured walking times (seconds). ``np.nan``
marks a point without a usable measurement (unknown). Marching squares extracts
the ``time_limit_s`` contour; ambiguous saddle cells are resolved with the cell
centre average. Contour rings are polygonized into a Polygon/MultiPolygon that
keeps holes, and cells touching an unknown point are never interpolated.

This module only does geometry: it does not call any route provider.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from app.schemas.common import CenterPoint

EARTH_RADIUS_M = 6_371_008.8
#: Chinese display labels for the standard 5/10/15-minute tiers.
TIER_LABELS_ZH = {"0-300": "5分钟", "300-600": "10分钟", "600-900": "15分钟"}
#: The checkup report is fixed at 15 minutes; tiers only make sense for 900 s.
TIERS_TIME_LIMIT_S = 900

# Edge names in clockwise order: top, right, bottom, left.
_EDGES = ("T", "R", "B", "L")


@dataclass(frozen=True, slots=True)
class GridField:
    """A regular local-plane grid; row index grows with +y, column with +x."""

    times: np.ndarray
    origin_x_m: float
    origin_y_m: float
    cell_m: float
    center: CenterPoint
    time_limit_s: int = 900

    def __post_init__(self) -> None:
        if self.times.ndim != 2:
            raise ValueError("times 必须是二维数组。")
        if not math.isfinite(self.cell_m) or self.cell_m <= 0:
            raise ValueError("cell_m 必须是正数。")

    @property
    def rows(self) -> int:
        return int(self.times.shape[0])

    @property
    def cols(self) -> int:
        return int(self.times.shape[1])

    def point_xy(self, row: int, col: int) -> tuple[float, float]:
        return (
            self.origin_x_m + col * self.cell_m,
            self.origin_y_m + row * self.cell_m,
        )

    def xy_to_lnglat(self, x_m: float, y_m: float) -> tuple[float, float]:
        cosine = max(1e-6, math.cos(math.radians(self.center.lat)))
        return (
            self.center.lng + math.degrees(x_m / (EARTH_RADIUS_M * cosine)),
            self.center.lat + math.degrees(y_m / EARTH_RADIUS_M),
        )

    def valid(self) -> np.ndarray:
        return np.isfinite(self.times)

    def reachable(self) -> np.ndarray:
        return np.isfinite(self.times) & (self.times <= self.time_limit_s)


def grid_points(field: GridField) -> list[tuple[int, int, float, float]]:
    """Return (row, col, x_m, y_m) for every finite measured point."""
    points = []
    for row in range(field.rows):
        for col in range(field.cols):
            if math.isfinite(float(field.times[row, col])):
                x, y = field.point_xy(row, col)
                points.append((row, col, x, y))
    return points


def _edge_point(field: GridField, row: int, col: int, edge: str,
                v00: float, v10: float, v11: float, v01: float) -> tuple[float, float]:
    limit = field.time_limit_s
    if edge == "T":
        t = (limit - v00) / (v10 - v00)
        x, y = col + t, row
    elif edge == "R":
        t = (limit - v10) / (v11 - v10)
        x, y = col + 1, row + t
    elif edge == "B":
        t = (limit - v01) / (v11 - v01)
        x, y = col + t, row + 1
    else:  # L
        t = (limit - v00) / (v01 - v00)
        x, y = col, row + t
    return field.origin_x_m + x * field.cell_m, field.origin_y_m + y * field.cell_m


# Marching-squares lookup: case bits b0=v00, b1=v10, b2=v11, b3=v01 (inside=1).
_CASES: dict[int, tuple[tuple[str, str], ...]] = {
    0: (), 15: (),
    1: (("L", "T"),),
    2: (("T", "R"),),
    3: (("L", "R"),),
    4: (("R", "B"),),
    5: (("L", "B"), ("T", "R")),  # ambiguous, resolved by centre
    6: (("T", "B"),),
    7: (("L", "B"),),
    8: (("B", "L"),),
    9: (("T", "B"),),
    10: (("T", "L"), ("R", "B")),  # ambiguous
    11: (("R", "B"),),
    12: (("L", "R"),),
    13: (("T", "R"),),
    14: (("L", "T"),),
}


def _cell_segments(field: GridField, row: int, col: int) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    v00 = float(field.times[row, col])
    v10 = float(field.times[row, col + 1])
    v11 = float(field.times[row + 1, col + 1])
    v01 = float(field.times[row + 1, col])
    if not all(math.isfinite(v) for v in (v00, v10, v11, v01)):
        return []  # never interpolate across an unknown corner
    limit = field.time_limit_s
    case = (
        (1 if v00 <= limit else 0)
        | (2 if v10 <= limit else 0)
        | (4 if v11 <= limit else 0)
        | (8 if v01 <= limit else 0)
    )
    if case in (0, 15):
        return []
    pairs = _CASES[case]
    if case in (5, 10):
        # Resolve the saddle with the cell centre (bilinear average).
        centre = (v00 + v10 + v11 + v01) / 4.0
        centre_inside = centre <= limit
        if case == 5:
            pairs = (("L", "B"), ("T", "R")) if centre_inside else (("L", "T"), ("R", "B"))
        else:
            pairs = (("T", "L"), ("R", "B")) if centre_inside else (("L", "B"), ("T", "R"))
    segments = []
    for first, second in pairs:
        a = _edge_point(field, row, col, first, v00, v10, v11, v01)
        b = _edge_point(field, row, col, second, v00, v10, v11, v01)
        if a != b:
            segments.append((a, b))
    return segments


def _padded_field(field: GridField) -> GridField:
    """Pad with above-limit values so the reachable region gets a closed outer ring.

    Unknown points are also treated as above-limit for *contouring* only: the
    resulting hole keeps them out of the reachable polygon, while the original
    mask is still reported as unknown. Without padding, polygonize cannot tell a
    closed high-time patch from the reachable side of an open contour.
    """
    limit = field.time_limit_s
    filled = np.where(np.isfinite(field.times), field.times, float(limit + 1))
    padded = np.full((field.rows + 2, field.cols + 2), float(limit + 1), dtype=float)
    padded[1:-1, 1:-1] = filled
    return GridField(
        times=padded,
        origin_x_m=field.origin_x_m - field.cell_m,
        origin_y_m=field.origin_y_m - field.cell_m,
        cell_m=field.cell_m,
        center=field.center,
        time_limit_s=field.time_limit_s,
    )


def marching_squares(field: GridField) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Return contour segments in local metres for the field's time limit."""
    padded = _padded_field(field)
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for row in range(padded.rows - 1):
        for col in range(padded.cols - 1):
            segments.extend(_cell_segments(padded, row, col))
    return segments


def segments_to_geometry(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> Polygon | MultiPolygon | None:
    """Polygonize closed contour rings, keeping holes via containment depth.

    Marching squares can leave open lines along unknown patches; those are
    ignored by polygonize, and the caller reports unknown coverage separately.
    """
    if not segments:
        return None
    lines = [LineString([a, b]) for a, b in segments]
    polygons = [poly for poly in polygonize(unary_union(lines)) if poly.area > 0]
    if not polygons:
        return None
    # Containment depth: outer rings have even depth, holes odd depth. Polygonize
    # may already punch the hole, so containment is tested against the filled
    # exterior ring rather than the punctured polygon.
    filled = [Polygon(poly.exterior) for poly in polygons]
    depths = []
    for index, polygon in enumerate(polygons):
        point = polygon.representative_point()
        depth = sum(
            1
            for other_index, other in enumerate(filled)
            if other_index != index and other.area > filled[index].area and other.contains(point)
        )
        depths.append(depth)
    outers = [poly for poly, depth in zip(polygons, depths, strict=True) if depth % 2 == 0]
    holes = [poly for poly, depth in zip(polygons, depths, strict=True) if depth % 2 == 1]
    if not outers:
        return None
    # Assign each hole to the smallest outer whose *filled* exterior contains it.
    # ``outer`` itself is already punctured by polygonize, so containment must be
    # tested against the filled ring rather than the polygon.
    filled_outers = [(outer, Polygon(outer.exterior)) for outer in outers]
    built = []
    for outer, filled in filled_outers:
        # polygonize often already recognises an enclosed ring as an interior;
        # keep those and only attach holes it left as separate polygons.
        assigned = list(outer.interiors)
        for hole in holes:
            if any(ring.equals(hole.exterior) for ring in assigned):
                continue
            point = hole.representative_point()
            if not filled.contains(point):
                continue
            if any(
                candidate.area < filled.area and candidate.contains(point)
                for candidate, candidate_filled in filled_outers
                if candidate is not outer and candidate_filled.contains(point)
            ):
                continue
            assigned.append(hole.exterior)
        polygon = Polygon(outer.exterior, assigned)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty and polygon.area > 0:
            built.append(polygon)
    if not built:
        return None
    merged = unary_union(built)
    return merged if isinstance(merged, (Polygon, MultiPolygon)) else None


def field_to_geometry(field: GridField) -> Polygon | MultiPolygon | None:
    return segments_to_geometry(marching_squares(field))


def field_to_lnglat_geometry(field: GridField) -> dict | None:
    """GeoJSON-like dict in BD09LL for a field contour."""
    geometry = field_to_geometry(field)
    if geometry is None:
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
        "algorithm_version": "grid-marching-v1",
        "unknown_points": int((~field.valid()).sum()),
        "unknown_ratio": float((~field.valid()).sum() / field.times.size),
    }


def make_radial_field(
    center: CenterPoint,
    radius_m: float,
    cell_m: float,
    seconds_per_m: float,
    *,
    unknown_mask=None,
) -> GridField:
    """Synthetic radial field used by offline tests (not a product path)."""
    if radius_m <= 0 or cell_m <= 0 or seconds_per_m <= 0:
        raise ValueError("半径、格距和步行速度必须为正。")
    half_cells = int(math.ceil(radius_m / cell_m))
    size = half_cells * 2 + 1
    times = np.full((size, size), np.nan, dtype=float)
    x0 = -half_cells * cell_m
    y0 = -half_cells * cell_m
    for row in range(size):
        for col in range(size):
            x = x0 + col * cell_m
            y = y0 + row * cell_m
            distance = math.hypot(x, y)
            if unknown_mask is not None and unknown_mask(x, y):
                continue
            if distance > radius_m:
                continue
            times[row, col] = distance * seconds_per_m
    return GridField(
        times=times,
        origin_x_m=x0,
        origin_y_m=y0,
        cell_m=cell_m,
        center=center,
    )


def _grid_targets(center: CenterPoint, radius_m: float, cell_m: float) -> tuple[list[tuple[str, CenterPoint]], int, int, float, float]:
    """Build (target_id, point) for every grid node inside the circle."""
    half = int(math.ceil(radius_m / cell_m))
    size = half * 2 + 1
    origin = -half * cell_m
    cosine = max(1e-6, math.cos(math.radians(center.lat)))
    targets: list[tuple[str, CenterPoint]] = []
    for row in range(size):
        for col in range(size):
            x = origin + col * cell_m
            y = origin + row * cell_m
            if math.hypot(x, y) > radius_m:
                continue
            lng = center.lng + math.degrees(x / (EARTH_RADIUS_M * cosine))
            lat = center.lat + math.degrees(y / EARTH_RADIUS_M)
            targets.append((f"grid:r{row:04d}:c{col:04d}", CenterPoint(lng=lng, lat=lat, crs="BD09LL")))
    return targets, size, size, origin, origin


def _contract_geometry(geojson: dict | None) -> dict | None:
    """Promote one polygon with holes to MultiPolygon for the report contract.

    ``field_to_lnglat_geometry`` / ``tiers.to_dict`` may return a GeoJSON
    Polygon whose ring list contains holes. ``GeoJSONPolygon`` only accepts one
    ring, while ``GeoJSONMultiPolygon`` already supports holes, so a holed
    polygon is wrapped as a one-polygon MultiPolygon.
    """
    if geojson is None:
        return None
    if geojson["type"] == "Polygon" and len(geojson["coordinates"]) > 1:
        return {"type": "MultiPolygon", "coordinates": [geojson["coordinates"]]}
    return geojson


def _build_tiers_contract(field: GridField, tiered, *, sample_count: int):
    """Turn a ``TieredIsochrone`` into the report contract models.

    Returns ``(tiers, time_field)`` or ``(None, None)`` when a cumulative range
    or band is empty; the caller then keeps the single 900 s layer.
    """
    from app.schemas.isochrone import (
        IsochroneTiers,
        IsochroneTimeField,
        IsochroneUnknownMask,
        TierBand,
        TierCumulative,
    )

    payload = tiered.to_dict()
    unknown = payload["unknown"]
    ratio = float(unknown["unknown_ratio"])
    cumulative = []
    bands = []
    for item in payload["tiers"]:
        geometry_cumulative = _contract_geometry(item.get("cumulative_geojson"))
        geometry_band = _contract_geometry(item.get("band_geojson"))
        if geometry_cumulative is None or geometry_band is None:
            return None, None
        label_zh = TIER_LABELS_ZH.get(item["tier_id"], item["tier_id"])
        cumulative.append(TierCumulative(
            threshold_s=int(item["to_s"]),
            label_zh=label_zh,
            geometry=geometry_cumulative,
            unknown_ratio=ratio,
        ))
        bands.append(TierBand(
            tier_id=item["tier_id"],
            from_s=int(item["from_s"]),
            to_s=int(item["to_s"]),
            label_zh=label_zh,
            geometry=geometry_band,
        ))
    tiers = IsochroneTiers(
        thresholds_s=list(payload["thresholds_s"]),
        cumulative=cumulative,
        bands=bands,
        time_source="route-matrix",
        sample_count=int(sample_count),
        unknown_ratio=ratio,
        unknown_mask=IsochroneUnknownMask(
            mask_cells=list(unknown["mask_cells"]),
            mask_geometry=_contract_geometry(unknown.get("mask_geojson")),
        ),
        algorithm_version=payload["algorithm_version"],
    )
    times_s = [
        [
            None if not math.isfinite(float(field.times[row, col]))
            else float(field.times[row, col])
            for col in range(field.cols)
        ]
        for row in range(field.rows)
    ]
    time_field = IsochroneTimeField(
        crs="local-m",
        origin_x_m=field.origin_x_m,
        origin_y_m=field.origin_y_m,
        cell_m=field.cell_m,
        rows=field.rows,
        cols=field.cols,
        time_limit_s=field.time_limit_s,
        times_s=times_s,
        source="route-matrix",
        collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return tiers, time_field


def generate_grid_isochrone(
    route,
    request,
    *,
    radius_m: float = 2500.0,
    cell_m: float = 200.0,
    max_grid_points: int = 800,
    include_tiers: bool = False,
):
    """Measure a regular grid and return a Polygon/MultiPolygon isochrone.

    ``route`` follows the same callable contract as ``IsochroneService``. The
    geometry is a 900 s contour of the measured time field; unknown grid points
    are represented by holes/absent coverage, never interpolated as reachable.

    ``include_tiers`` (off by default) additionally attaches the optional
    5/10/15-minute ``tiers`` and the replayable ``time_field`` contracts. The
    900 s ``geometry``/``quality`` output is identical with or without tiers,
    so coverage, scoring and blindspot consumers keep their current behaviour.
    """
    from app.schemas.common import Meta, WarningItem
    from app.schemas.isochrone import (
        GeoJSONMultiPolygon,
        GeoJSONPolygon,
        IsochroneConfidence,
        IsochroneData,
        IsochroneErrorCode,
        IsochroneFailure,
        IsochroneQuality,
        IsochroneResult,
        IsochroneSuccess,
    )
    from app.schemas.routing import (
        RouteStatus,
        RouteTarget,
        RoutingFailure,
        RoutingRequest,
        RoutingSuccess,
    )

    if radius_m <= 0 or cell_m <= 0:
        raise ValueError("radius_m 和 cell_m 必须为正数。")
    targets, rows, cols, origin_x, origin_y = _grid_targets(request.center, radius_m, cell_m)
    if len(targets) > max_grid_points:
        raise ValueError(f"网格点数 {len(targets)} 超过上限 {max_grid_points}。")
    routing_request = RoutingRequest(
        schema_version="1.0",
        origin=request.center,
        targets=[RouteTarget(target_id=target_id, location=point) for target_id, point in targets],
        travel_mode="walking",
    )
    result = route(routing_request)
    if isinstance(result.root, RoutingFailure):
        return IsochroneResult(root=IsochroneFailure(
            ok=False,
            error={"code": IsochroneErrorCode.ROUTING_FAILED,
                   "message": "步行批量测时失败，无法生成网格等时圈。",
                   "retryable": result.root.error.retryable},
            warnings=result.root.warnings,
            meta=Meta(schema_version="1.0", provider="baidu"),
        ))
    assert isinstance(result.root, RoutingSuccess)
    positions = {target_id: (int(target_id.split(":")[1][1:]), int(target_id.split(":")[2][1:]))
                 for target_id, _ in targets}
    times = np.full((rows, cols), np.nan, dtype=float)
    success = 0
    for route_result in result.root.data.routes:
        if route_result.status is not RouteStatus.SUCCESS or route_result.duration_s is None:
            continue
        position = positions.get(route_result.target_id)
        if position is None:
            continue
        times[position] = float(route_result.duration_s)
        success += 1

    field = GridField(
        times=times,
        origin_x_m=origin_x,
        origin_y_m=origin_y,
        cell_m=cell_m,
        center=request.center,
        time_limit_s=request.time_limit_s,
    )
    geojson = _contract_geometry(field_to_lnglat_geometry(field))
    if geojson is None or success < 3:
        return IsochroneResult(root=IsochroneFailure(
            ok=False,
            error={"code": IsochroneErrorCode.INSUFFICIENT_ROUTING_DATA,
                   "message": f"有效网格测时 {success}/{len(targets)}，无法生成可靠等时圈。",
                   "retryable": True},
            warnings=[WarningItem(code="PARTIAL_ROUTING_RESULTS",
                                  message="部分网格点没有可用步行测时；未把未知区域当作可达。")],
            meta=Meta(schema_version="1.0", provider="baidu"),
        ))
    if geojson["type"] == "MultiPolygon":
        geometry = GeoJSONMultiPolygon(type="MultiPolygon", coordinates=geojson["coordinates"])
    else:
        geometry = GeoJSONPolygon(type="Polygon", coordinates=geojson["coordinates"])
    success_rate = success / len(targets)
    confidence = (
        IsochroneConfidence.HIGH if success_rate >= 0.9
        else IsochroneConfidence.MEDIUM if success_rate >= 0.75
        else IsochroneConfidence.LOW
    )
    warnings = []
    if success < len(targets):
        warnings.append(WarningItem(
            code="PARTIAL_ROUTING_RESULTS",
            message=f"网格测时成功 {success}/{len(targets)}；未知网格点不参与插值。"))
    if confidence is IsochroneConfidence.LOW:
        warnings.append(WarningItem(code="LOW_CONFIDENCE", message="网格等时圈测时成功率较低。"))
    warnings.append(WarningItem(
        code="APPROXIMATE_WALKING_ISOCHRONE",
        message=("等时圈由规则网格步行测时与 900 秒等值线推导；未知区域不填平，"
                 "仍不是真实道路或建筑边界。")))
    quality = IsochroneQuality(
        requested_directions=0,
        valid_boundary_directions=0,
        routing_sample_count=len(targets),
        routing_success_count=success,
        sample_success_rate=success_rate,
        confidence=confidence,
        unknown_ratio=1.0 - success_rate,
        grid_cell_m=cell_m,
        algorithm_version="grid-marching-v1",
    )

    tiers = time_field = None
    if include_tiers:
        if request.time_limit_s != TIERS_TIME_LIMIT_S:
            warnings.append(WarningItem(
                code="TIERS_SKIPPED_NON_900S",
                message="三档展示只适用于 900 秒报告；本次返回单层等时圈。"))
        else:
            from app.modules.isochrone.tiers import build_tiers

            tiers, time_field = _build_tiers_contract(
                field, build_tiers(field), sample_count=success,
            )
            if tiers is None or time_field is None:
                tiers = time_field = None
                warnings.append(WarningItem(
                    code="TIERS_UNAVAILABLE",
                    message="网格时间场未能生成完整三档；本次返回单层等时圈。"))

    return IsochroneResult(root=IsochroneSuccess(
        ok=True,
        data=IsochroneData(
            center=request.center,
            time_limit_s=request.time_limit_s,
            geometry=geometry,
            algorithm_version="grid-marching-v1",
            boundary_points=[],
            quality=quality,
            tiers=tiers,
            time_field=time_field,
        ),
        warnings=warnings,
        meta=Meta(schema_version="1.0", provider="baidu"),
    ))
