"""Offline validator for candidate isochrone edges built from walking routes.

Stage 2B of the v3 plan: two adjacent boundary sample points ``A`` and ``B`` may
be connected by a real walking route, but that route is only a *candidate*
contour edge.  Two points that are each reachable in about T seconds do not
prove that the walking route between them is a valid T-second contour edge; the
route can snap to a far road, come back toward the centre, detour, cross itself
or run through a hole / unknown band.  This module rejects those candidates
before they are allowed to replace a contour edge:

* endpoint snapping - the returned polyline does not start/end at A/B;
* inward folding - the route moves back toward the analysis centre;
* detour - the route is much longer than the straight A -> B distance;
* self intersection - the polyline crosses or retraces itself;
* hole / unknown crossing - the route runs through a supplied hole or unknown
  geometry;
* tier incompatibility - the measured centre arrival times at the endpoints are
  not close to the requested 300 / 600 / 900 s tier;
* missing time evidence - the edge is reported as ``unverified`` and is **never**
  silently accepted.

The module is pure geometry + timing: it never calls a route provider, never
fetches anything, and never invents missing evidence.  Obstacle geometries and
threshold values are injected by the caller.  Coordinates are either ``BD09LL``
(projected onto a local metre plane around ``center``, same formula as
``app/modules/isochrone/grid.py``) or an already projected local metre plane
(``crs="local"``).  All geometry inputs (``center``, ``a``, ``b``, ``polyline``,
``holes``, ``unknown``) must use the same CRS.

The default thresholds are *suggested* parameters for the offline prototype, not
values validated against a real A -> B sample; they are expected to be tuned once
the budgeted Baidu A -> B requests are available.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Literal

import numpy as np
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

EARTH_RADIUS_M = 6_371_008.8

PointLike = tuple[float, float]
CoordinateReferenceSystem = Literal["BD09LL", "local"]
Severity = Literal["reject", "unverified", "info"]
TimeCompatibility = Literal["compatible", "incompatible", "unverified"]
DecisionStatus = Literal["accept", "reject", "unverified"]

TIER_OPTIONS_S = (300, 600, 900)


class ReasonCode(str, Enum):
    """Machine-readable reason codes emitted by the candidate-edge validator."""

    ENDPOINT_SNAP_EXCEEDED = "ENDPOINT_SNAP_EXCEEDED"
    BACKTRACK_TOWARD_CENTER = "BACKTRACK_TOWARD_CENTER"
    DETOUR_RATIO_EXCEEDED = "DETOUR_RATIO_EXCEEDED"
    ROUTE_SELF_INTERSECTION = "ROUTE_SELF_INTERSECTION"
    HOLE_CROSSED = "HOLE_CROSSED"
    UNKNOWN_CROSSED = "UNKNOWN_CROSSED"
    TIME_TIER_INCOMPATIBLE = "TIME_TIER_INCOMPATIBLE"
    TIME_EVIDENCE_MISSING = "TIME_EVIDENCE_MISSING"
    DEGENERATE_EDGE = "DEGENERATE_EDGE"
    OBSTACLE_GEOMETRY_NOT_PROVIDED = "OBSTACLE_GEOMETRY_NOT_PROVIDED"


@dataclass(frozen=True, slots=True)
class EdgeThresholds:
    """Tunable thresholds with defaults for the offline prototype.

    ``backtrack_depth_m`` rejects a route that gets closer to the centre than
    both endpoints; the effective threshold is ``max(backtrack_depth_m,
    backtrack_ratio * min(centre_to_a_m, centre_to_b_m))`` so the rule also
    works for short edges near the centre.  ``inward_return_m`` rejects a route
    that moves outward and then significantly back toward the centre without
    going past both endpoints.  ``time_band_s`` is
    ``max(time_band_min_s, time_band_ratio * tier_s)``.
    """

    max_endpoint_snap_m: float = 30.0
    max_detour_ratio: float = 2.0
    min_straight_distance_m: float = 1.0
    backtrack_depth_m: float = 60.0
    backtrack_ratio: float = 0.1
    inward_return_m: float = 150.0
    time_band_ratio: float = 0.2
    time_band_min_s: float = 60.0
    hole_crossing_tolerance_m: float = 1.0
    unknown_crossing_tolerance_m: float = 1.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_endpoint_snap_m",
            "max_detour_ratio",
            "min_straight_distance_m",
            "backtrack_depth_m",
            "backtrack_ratio",
            "inward_return_m",
            "time_band_ratio",
            "time_band_min_s",
            "hole_crossing_tolerance_m",
            "unknown_crossing_tolerance_m",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"阈值 {field_name} 必须是有限的非负数。")
        if self.max_detour_ratio <= 0:
            raise ValueError("max_detour_ratio 必须为正数。")
        if self.min_straight_distance_m <= 0:
            raise ValueError("min_straight_distance_m 必须为正数。")
        if not 0 <= self.time_band_ratio < 1:
            raise ValueError("time_band_ratio 必须在 [0, 1) 之间。")
        if self.time_band_min_s <= 0:
            raise ValueError("time_band_min_s 必须为正数。")

    def time_band_s(self, tier_s: int) -> float:
        if tier_s not in TIER_OPTIONS_S:
            raise ValueError(f"tier_s 必须是 {TIER_OPTIONS_S} 之一。")
        return max(self.time_band_min_s, self.time_band_ratio * tier_s)


@dataclass(frozen=True, slots=True)
class Reason:
    """One machine-readable decision reason plus a human-readable message."""

    code: ReasonCode
    severity: Severity
    message: str
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EdgeMetrics:
    """Raw measurements behind a decision, kept for evidence tables."""

    tier_s: int
    tier_band_s: float
    vertex_count: int
    straight_distance_m: float
    route_length_m: float
    detour_ratio: float | None
    start_snap_m: float
    end_snap_m: float
    max_snap_m: float
    center_to_a_m: float
    center_to_b_m: float
    min_center_distance_m: float
    backtrack_depth_m: float
    inward_return_m: float
    self_intersects: bool
    obstacle_geometry_supplied: bool
    crosses_hole: bool | None
    crosses_unknown: bool | None
    hole_crossing_length_m: float | None
    unknown_crossing_length_m: float | None
    time_evidence_complete: bool
    time_compatibility: TimeCompatibility
    center_to_a_duration_s: float | None
    route_duration_s: float | None
    estimated_center_to_b_duration_s: float | None

    def as_dict(self) -> dict:
        return {
            key: (round(value, 3) if isinstance(value, float) else value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True, slots=True)
class EdgeDecision:
    """Validator output: status + reason codes + metrics + thresholds used."""

    status: DecisionStatus
    reasons: tuple[Reason, ...]
    metrics: EdgeMetrics
    thresholds: EdgeThresholds

    @property
    def accepted(self) -> bool:
        return self.status == "accept"

    @property
    def codes(self) -> tuple[ReasonCode, ...]:
        return tuple(reason.code for reason in self.reasons)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "reason_codes": [code.value for code in self.codes],
            "reasons": [reason.as_dict() for reason in self.reasons],
            "metrics": self.metrics.as_dict(),
            "thresholds": asdict(self.thresholds),
        }


def bd09ll_to_local(point: Sequence[float], center: Sequence[float]) -> PointLike:
    """Project a BD09LL ``(lng, lat)`` point onto the local metre plane.

    The plane is centred on ``center`` and uses the same equirectangular
    approximation as ``grid.py``; it is only intended for the few-kilometre
    scale of one 15-minute walking circle.
    """
    lng, lat = float(point[0]), float(point[1])
    center_lng, center_lat = float(center[0]), float(center[1])
    cosine = max(1e-6, math.cos(math.radians(center_lat)))
    x_m = math.radians(lng - center_lng) * EARTH_RADIUS_M * cosine
    y_m = math.radians(lat - center_lat) * EARTH_RADIUS_M
    return x_m, y_m


def _coerce_point(value: Sequence[float], name: str) -> PointLike:
    if isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError(f"{name} 必须是 (x, y) 或 (lng, lat) 二元组。")
    x, y = float(value[0]), float(value[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{name} 坐标必须是有限数。")
    return x, y


def _distance(first: PointLike, second: PointLike) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _project_geometry(geometry: BaseGeometry, center: Sequence[float]) -> BaseGeometry:
    """Project a shapely geometry from BD09LL to the local metre plane."""
    center_lng, center_lat = float(center[0]), float(center[1])
    cosine = max(1e-6, math.cos(math.radians(center_lat)))

    def _project(x, y, z=None):  # shapely 2.x passes numpy arrays here
        x_values = np.asarray(x, dtype=float)
        y_values = np.asarray(y, dtype=float)
        return (
            np.radians(x_values - center_lng) * EARTH_RADIUS_M * cosine,
            np.radians(y_values - center_lat) * EARTH_RADIUS_M,
        )

    return transform(_project, geometry)


def _dedupe_polyline(points: Sequence[PointLike]) -> list[PointLike]:
    deduped: list[PointLike] = []
    for point in points:
        if not deduped or _distance(deduped[-1], point) > 1e-9:
            deduped.append(point)
    return deduped


def _optional_duration(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"{name} 必须是有限的非负数。")
    return duration


def _snap_reason(start_snap_m: float, end_snap_m: float, threshold_m: float) -> Reason:
    offenders = []
    if start_snap_m > threshold_m:
        offenders.append(f"起点 A {start_snap_m:.1f} m")
    if end_snap_m > threshold_m:
        offenders.append(f"终点 B {end_snap_m:.1f} m")
    return Reason(
        code=ReasonCode.ENDPOINT_SNAP_EXCEEDED,
        severity="reject",
        message=f"路线端点与候选点吸附偏移超出 {threshold_m:.1f} m 阈值。",
        detail=", ".join(offenders),
    )


def validate_candidate_edge(
    *,
    center: Sequence[float],
    a: Sequence[float],
    b: Sequence[float],
    polyline: Sequence[Sequence[float]],
    tier_s: int,
    crs: CoordinateReferenceSystem = "BD09LL",
    center_to_a_duration_s: float | None = None,
    route_duration_s: float | None = None,
    holes: BaseGeometry | None = None,
    unknown: BaseGeometry | None = None,
    thresholds: EdgeThresholds | None = None,
) -> EdgeDecision:
    """Validate one candidate contour edge ``A -> B`` built from a walking route.

    Parameters
    ----------
    center:
        Analysis centre.  For ``crs="BD09LL"`` it is ``(lng, lat)``; for
        ``crs="local"`` it is a point in the local metre plane.
    a, b:
        Requested end points of the candidate edge (the adjacent boundary
        sample points), in the same CRS as ``center``.
    polyline:
        Route returned for ``A -> B``.  Some route providers snap the requested
        points to nearby roads, so the first/last vertex can differ from A/B;
        that is measured instead of assumed away.
    tier_s:
        Contour tier being validated, one of 300 / 600 / 900 seconds.
    crs:
        Coordinate reference of all point-like inputs.  ``"BD09LL"`` projects
        the points onto a local metre plane; ``"local"`` uses them directly.
    center_to_a_duration_s:
        Optional measured centre -> A walking time (seconds).  Without it the
        centre arrival at the endpoints cannot be placed on the tier.
    route_duration_s:
        Optional measured A -> B walking time (seconds).  Without it the edge
        time cannot be checked.  Missing evidence yields ``unverified``.
    holes, unknown:
        Optional shapely geometry for known holes / unknown bands, in the same
        CRS as the points.  When omitted the crossing check is reported as not
        supplied, never as "no crossing".
    thresholds:
        Optional ``EdgeThresholds`` override; defaults are offline suggestions.

    Returns
    -------
    EdgeDecision
        ``status`` is ``accept`` / ``reject`` / ``unverified`` with reason
        codes and all measured metrics.  ``unverified`` means the geometry did
        not obviously fail but the time evidence is incomplete, so the edge is
        not allowed to pass.
    """
    settings = thresholds if thresholds is not None else EdgeThresholds()
    if tier_s not in TIER_OPTIONS_S:
        raise ValueError(f"tier_s 必须是 {TIER_OPTIONS_S} 之一。")
    if crs not in ("BD09LL", "local"):
        raise ValueError('crs 必须是 "BD09LL" 或 "local"。')
    center_to_a = _optional_duration(center_to_a_duration_s, "center_to_a_duration_s")
    route_duration = _optional_duration(route_duration_s, "route_duration_s")

    center_point = _coerce_point(center, "center")
    a_point = _coerce_point(a, "a")
    b_point = _coerce_point(b, "b")
    raw_polyline = [_coerce_point(point, "polyline") for point in polyline]
    if len(raw_polyline) < 2:
        raise ValueError("路线折线至少需要两个点。")

    if crs == "BD09LL":
        local_center: PointLike = (0.0, 0.0)
        local_a = bd09ll_to_local(a_point, center_point)
        local_b = bd09ll_to_local(b_point, center_point)
        local_polyline = [bd09ll_to_local(point, center_point) for point in raw_polyline]
        local_holes = _project_geometry(holes, center_point) if holes is not None else None
        local_unknown = _project_geometry(unknown, center_point) if unknown is not None else None
    else:
        local_center = center_point
        local_a = a_point
        local_b = b_point
        local_polyline = list(raw_polyline)
        local_holes = holes
        local_unknown = unknown

    points = _dedupe_polyline(local_polyline)
    if len(points) < 2:
        raise ValueError("路线折线去重后不足两个点，无法校验。")

    line = LineString(points)
    center_geometry = Point(local_center)

    start_snap_m = _distance(points[0], local_a)
    end_snap_m = _distance(points[-1], local_b)
    max_snap_m = max(start_snap_m, end_snap_m)
    center_to_a_m = _distance(local_center, local_a)
    center_to_b_m = _distance(local_center, local_b)
    min_center_distance_m = float(line.distance(center_geometry))
    min_endpoint_center_m = min(center_to_a_m, center_to_b_m)
    backtrack_depth_m = max(0.0, min_endpoint_center_m - min_center_distance_m)

    running_max = 0.0
    inward_return_m = 0.0
    for point in points:
        center_distance = _distance(local_center, point)
        running_max = max(running_max, center_distance)
        inward_return_m = max(inward_return_m, running_max - center_distance)

    straight_distance_m = _distance(local_a, local_b)
    route_length_m = float(line.length)
    detour_ratio: float | None = None
    if straight_distance_m > settings.min_straight_distance_m:
        detour_ratio = route_length_m / straight_distance_m

    self_intersects = not line.is_simple

    obstacle_geometry_supplied = local_holes is not None or local_unknown is not None
    crosses_hole: bool | None = None
    crosses_unknown: bool | None = None
    hole_crossing_length_m: float | None = None
    unknown_crossing_length_m: float | None = None
    if local_holes is not None:
        hole_crossing_length_m = float(line.intersection(local_holes).length)
        crosses_hole = hole_crossing_length_m > settings.hole_crossing_tolerance_m
    if local_unknown is not None:
        unknown_crossing_length_m = float(line.intersection(local_unknown).length)
        crosses_unknown = unknown_crossing_length_m > settings.unknown_crossing_tolerance_m

    time_band_s = settings.time_band_s(tier_s)
    time_low_s = tier_s - time_band_s
    time_high_s = tier_s + time_band_s
    time_evidence_complete = center_to_a is not None and route_duration is not None
    estimated_center_to_b: float | None = None
    time_compatibility: TimeCompatibility
    if time_evidence_complete:
        assert center_to_a is not None and route_duration is not None
        estimated_center_to_b = center_to_a + route_duration
        a_in_band = time_low_s <= center_to_a <= time_high_s
        b_in_band = time_low_s <= estimated_center_to_b <= time_high_s
        time_compatibility = "compatible" if a_in_band and b_in_band else "incompatible"
    else:
        time_compatibility = "unverified"

    reasons: list[Reason] = []

    if detour_ratio is None:
        reasons.append(
            Reason(
                code=ReasonCode.DEGENERATE_EDGE,
                severity="reject",
                message=(
                    f"A 与 B 直线距离 {straight_distance_m:.1f} m 过小，"
                    "无法作为候选轮廓边。"
                ),
            )
        )
    if max_snap_m > settings.max_endpoint_snap_m:
        reasons.append(
            _snap_reason(start_snap_m, end_snap_m, settings.max_endpoint_snap_m)
        )

    backtrack_threshold_m = max(
        settings.backtrack_depth_m,
        settings.backtrack_ratio * min_endpoint_center_m,
    )
    if backtrack_depth_m > backtrack_threshold_m:
        reasons.append(
            Reason(
                code=ReasonCode.BACKTRACK_TOWARD_CENTER,
                severity="reject",
                message=(
                    "路线明显向分析中心回折：比两端点更靠近中心 "
                    f"{backtrack_depth_m:.1f} m（阈值 {backtrack_threshold_m:.1f} m）。"
                ),
                detail=f"min_center_distance_m={min_center_distance_m:.1f}",
            )
        )
    if inward_return_m > settings.inward_return_m:
        reasons.append(
            Reason(
                code=ReasonCode.BACKTRACK_TOWARD_CENTER,
                severity="reject",
                message=(
                    "路线向外走后又明显回退靠近中心 "
                    f"{inward_return_m:.1f} m（阈值 {settings.inward_return_m:.1f} m）。"
                ),
                detail=f"inward_return_m={inward_return_m:.1f}",
            )
        )

    if detour_ratio is not None and detour_ratio > settings.max_detour_ratio:
        reasons.append(
            Reason(
                code=ReasonCode.DETOUR_RATIO_EXCEEDED,
                severity="reject",
                message=(
                    f"路线长 {route_length_m:.1f} m 是 A→B 直线 "
                    f"{straight_distance_m:.1f} m 的 {detour_ratio:.2f} 倍，"
                    f"超过阈值 {settings.max_detour_ratio:.2f}。"
                ),
            )
        )

    if self_intersects:
        reasons.append(
            Reason(
                code=ReasonCode.ROUTE_SELF_INTERSECTION,
                severity="reject",
                message="路线折线自交或自身重叠，不能作为轮廓边。",
            )
        )

    if crosses_hole:
        reasons.append(
            Reason(
                code=ReasonCode.HOLE_CROSSED,
                severity="reject",
                message=(
                    "路线跨越已知孔洞 "
                    f"{hole_crossing_length_m or 0.0:.1f} m，孔洞不能被填平。"
                ),
            )
        )
    if crosses_unknown:
        reasons.append(
            Reason(
                code=ReasonCode.UNKNOWN_CROSSED,
                severity="reject",
                message=(
                    "路线跨越未知区域 "
                    f"{unknown_crossing_length_m or 0.0:.1f} m，"
                    "未知不能当作确定可达。"
                ),
            )
        )

    if not time_evidence_complete:
        missing = []
        if center_to_a is None:
            missing.append("center_to_a_duration_s")
        if route_duration is None:
            missing.append("route_duration_s")
        reasons.append(
            Reason(
                code=ReasonCode.TIME_EVIDENCE_MISSING,
                severity="unverified",
                message=(
                    f"缺少时间证据（{'、'.join(missing)}），"
                    "无法确认该边与 300/600/900 秒档相容，不得当作通过。"
                ),
                detail=(
                    f"tier={tier_s}s band=[{time_low_s:.0f},{time_high_s:.0f}]s"
                ),
            )
        )
    elif time_compatibility == "incompatible":
        assert estimated_center_to_b is not None
        offenders = []
        if not (time_low_s <= center_to_a <= time_high_s):
            offenders.append("A 端")
        if not (time_low_s <= estimated_center_to_b <= time_high_s):
            offenders.append("经 A→B 路线估算的 B 端")
        reasons.append(
            Reason(
                code=ReasonCode.TIME_TIER_INCOMPATIBLE,
                severity="reject",
                message=(
                    f"路线端点中心到达时间不在 {tier_s} 秒档 "
                    f"[{time_low_s:.0f}, {time_high_s:.0f}] s 内，"
                    "不能作为该档候选轮廓边。"
                ),
                detail=(
                    f"不满足: {'、'.join(offenders)}; "
                    f"t_a={center_to_a:.0f}s t_b_est={estimated_center_to_b:.0f}s"
                ),
            )
        )

    if not obstacle_geometry_supplied:
        reasons.append(
            Reason(
                code=ReasonCode.OBSTACLE_GEOMETRY_NOT_PROVIDED,
                severity="info",
                message="未提供孔洞/未知几何，跨越孔洞或未知区的检查未执行（不代表没有跨越）。",
            )
        )

    if any(reason.severity == "reject" for reason in reasons):
        status: DecisionStatus = "reject"
    elif any(reason.severity == "unverified" for reason in reasons):
        status = "unverified"
    else:
        status = "accept"

    metrics = EdgeMetrics(
        tier_s=tier_s,
        tier_band_s=time_band_s,
        vertex_count=len(points),
        straight_distance_m=straight_distance_m,
        route_length_m=route_length_m,
        detour_ratio=detour_ratio,
        start_snap_m=start_snap_m,
        end_snap_m=end_snap_m,
        max_snap_m=max_snap_m,
        center_to_a_m=center_to_a_m,
        center_to_b_m=center_to_b_m,
        min_center_distance_m=min_center_distance_m,
        backtrack_depth_m=backtrack_depth_m,
        inward_return_m=inward_return_m,
        self_intersects=self_intersects,
        obstacle_geometry_supplied=obstacle_geometry_supplied,
        crosses_hole=crosses_hole,
        crosses_unknown=crosses_unknown,
        hole_crossing_length_m=hole_crossing_length_m,
        unknown_crossing_length_m=unknown_crossing_length_m,
        time_evidence_complete=time_evidence_complete,
        time_compatibility=time_compatibility,
        center_to_a_duration_s=center_to_a,
        route_duration_s=route_duration,
        estimated_center_to_b_duration_s=estimated_center_to_b,
    )
    return EdgeDecision(
        status=status,
        reasons=tuple(reasons),
        metrics=metrics,
        thresholds=settings,
    )
