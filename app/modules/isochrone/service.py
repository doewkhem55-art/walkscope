"""Approximate a walking isochrone from real point-to-many Routing results."""

from collections.abc import Callable

from app.errors import ProviderStopError
from app.modules.isochrone.interpolation import interpolate_radius
from app.modules.isochrone.refinement import (
    BoundaryBracket,
    MeasuredSample,
    find_bracket,
    midpoint_sample,
)
from app.modules.isochrone.sampling import (
    DEFAULT_RADII_M,
    SamplePoint,
    bearings,
    destination_point,
    initial_samples,
    sample_point,
)
from app.schemas.common import Meta, WarningItem
from app.schemas.isochrone import (
    BoundaryPoint,
    GeoJSONPolygon,
    IsochroneConfidence,
    IsochroneData,
    IsochroneErrorCode,
    IsochroneFailure,
    IsochroneQuality,
    IsochroneRequest,
    IsochroneResult,
    IsochroneSuccess,
)
from app.schemas.routing import RouteStatus, RoutingRequest, RoutingResult


Route = Callable[[RoutingRequest], RoutingResult]


class IsochroneService:
    def __init__(
        self,
        route: Route,
        *,
        direction_count: int = 24,
        initial_radii_m: tuple[float, ...] = DEFAULT_RADII_M,
        refinement_rounds: int = 2,
        extension_step_m: float = 600.0,
        max_radius_m: float = 3000.0,
        max_routing_samples: int | None = None,
        max_angular_gap_steps: int | None = None,
        include_approximation_warning: bool = False,
    ) -> None:
        if direction_count < 3 or refinement_rounds < 0:
            raise ValueError("方向数至少为 3，refinement_rounds 不能为负数。")
        if (
            not initial_radii_m
            or any(radius <= 0 for radius in initial_radii_m)
            or len(set(initial_radii_m)) != len(initial_radii_m)
        ):
            raise ValueError("初始采样半径必须全部大于 0。")
        if extension_step_m <= 0 or max_radius_m <= max(initial_radii_m):
            raise ValueError("外扩采样参数无效。")
        if (
            max_routing_samples is not None
            and max_routing_samples < direction_count * len(initial_radii_m)
        ):
            raise ValueError("路由采样预算不能小于完整初始采样数。")
        if max_angular_gap_steps is not None and max_angular_gap_steps < 1:
            raise ValueError("最大边界缺口至少为一个方向采样间隔。")
        self.route = route
        self.direction_count = direction_count
        self.initial_radii_m = initial_radii_m
        self.refinement_rounds = refinement_rounds
        self.extension_step_m = extension_step_m
        self.max_radius_m = max_radius_m
        self.max_routing_samples = max_routing_samples
        self.max_angular_gap_steps = max_angular_gap_steps
        self.include_approximation_warning = include_approximation_warning

    @classmethod
    def detailed(cls, route: Route) -> "IsochroneService":
        """Build the denser route-sampled service used by the full checkup."""
        return cls(
            route,
            direction_count=72,
            refinement_rounds=2,
            extension_step_m=600.0,
            max_radius_m=3000.0,
            max_routing_samples=720,
            max_angular_gap_steps=2,
            include_approximation_warning=True,
        )

    def generate(self, request: IsochroneRequest) -> IsochroneResult:
        bearing_values = bearings(self.direction_count)
        measurements: dict[float, list[MeasuredSample]] = {
            bearing: [] for bearing in bearing_values
        }
        attempted_by_bearing: dict[float, list[SamplePoint]] = {
            bearing: [] for bearing in bearing_values
        }
        successful_sample_ids: set[str] = set()
        sample_count = 0
        success_count = 0
        had_missing_routes = False
        budget_exhausted = False
        routing_failure_codes: set[str] = set()

        initial = initial_samples(
            request.center,
            direction_count=self.direction_count,
            radii_m=self.initial_radii_m,
        )
        measured, attempted, succeeded, missing = self._measure(
            request, initial, routing_failure_codes
        )
        sample_count += attempted
        success_count += succeeded
        had_missing_routes |= missing
        _record_attempts(initial, measured, attempted_by_bearing, successful_sample_ids)
        _append_measurements(measurements, measured)

        next_radius = max(self.initial_radii_m) + self.extension_step_m
        while next_radius <= self.max_radius_m and "DAILY_QUOTA_EXHAUSTED" not in routing_failure_codes:
            missing_bearings = [
                bearing
                for bearing in bearing_values
                if find_bracket(
                    request.center,
                    bearing,
                    measurements[bearing],
                    request.time_limit_s,
                )
                is None
                and _needs_extension(measurements[bearing], request.time_limit_s)
            ]
            if not missing_bearings:
                break
            extension = [
                sample_point(
                    request.center,
                    bearing,
                    next_radius,
                    phase="extend",
                )
                for bearing in missing_bearings
            ]
            if not self._within_budget(sample_count, extension):
                budget_exhausted = True
                break
            measured, attempted, succeeded, missing = self._measure(
                request, extension, routing_failure_codes
            )
            sample_count += attempted
            success_count += succeeded
            had_missing_routes |= missing
            _record_attempts(
                extension, measured, attempted_by_bearing, successful_sample_ids
            )
            _append_measurements(measurements, measured)
            next_radius += self.extension_step_m

        for round_index in range(self.refinement_rounds):
            if budget_exhausted or "DAILY_QUOTA_EXHAUSTED" in routing_failure_codes:
                break
            brackets = _find_brackets(
                request,
                bearing_values,
                measurements,
                attempted_by_bearing,
                successful_sample_ids,
            )
            candidates = [
                midpoint_sample(request.center, bracket, round_index)
                for bracket in brackets.values()
            ]
            if not candidates:
                break
            if not self._within_budget(sample_count, candidates):
                budget_exhausted = True
                break
            measured, attempted, succeeded, missing = self._measure(
                request, candidates, routing_failure_codes
            )
            sample_count += attempted
            success_count += succeeded
            had_missing_routes |= missing
            _record_attempts(
                candidates, measured, attempted_by_bearing, successful_sample_ids
            )
            _append_measurements(measurements, measured)

        brackets = _find_brackets(
            request,
            bearing_values,
            measurements,
            attempted_by_bearing,
            successful_sample_ids,
        )
        gap_retry_count = 0
        if (
            self.max_angular_gap_steps is not None
            and "DAILY_QUOTA_EXHAUSTED" not in routing_failure_codes
            and routing_failure_codes.intersection({"RATE_LIMITED", "TIMEOUT"})
            and _largest_boundary_gap_steps(
                _boundary_points(request, brackets), self.direction_count
            ) > self.max_angular_gap_steps
        ):
            remaining = (
                self.max_routing_samples - sample_count
                if self.max_routing_samples is not None else 24
            )
            retry_samples = [
                sample
                for bearing in bearing_values if bearing not in brackets
                for sample in attempted_by_bearing[bearing]
                if sample.sample_id not in successful_sample_ids
            ][:min(24, remaining)]
            if retry_samples:
                measured, attempted, succeeded, missing = self._measure(
                    request, retry_samples, routing_failure_codes
                )
                gap_retry_count = attempted
                sample_count += attempted
                success_count += succeeded
                had_missing_routes |= missing
                successful_sample_ids.update(item.sample.sample_id for item in measured)
                _append_measurements(measurements, measured)
                brackets = _find_brackets(
                    request, bearing_values, measurements,
                    attempted_by_bearing, successful_sample_ids,
                )

        boundary_points = _boundary_points(request, brackets)
        largest_gap_steps = _largest_boundary_gap_steps(
            boundary_points, self.direction_count
        )
        too_large_gap = (
            self.max_angular_gap_steps is not None
            and largest_gap_steps > self.max_angular_gap_steps
        )
        warnings = _warnings(
            had_missing_routes,
            len(boundary_points),
            self.direction_count,
            _confidence(
                len(boundary_points), self.direction_count, success_count, sample_count
            ),
            include_approximation=self.include_approximation_warning,
            budget_exhausted=budget_exhausted,
            largest_gap_steps=largest_gap_steps if too_large_gap else None,
            max_angular_gap_steps=self.max_angular_gap_steps,
        )
        if gap_retry_count:
            warnings.append(WarningItem(
                code="BOUNDARY_GAP_RETRY",
                message=f"已对 {gap_retry_count} 个失败采样点补测；仅采纳实际返回的步行测时。",
            ))
        if "DAILY_QUOTA_EXHAUSTED" in routing_failure_codes:
            warnings.append(WarningItem(
                code="ROUTING_DAILY_QUOTA_EXHAUSTED",
                message="步行算路当日配额不足，未继续请求；未测方向保持未知。",
            ))
        if len(boundary_points) < 3 or too_large_gap:
            code = (
                IsochroneErrorCode.ROUTING_FAILED
                if success_count == 0
                else IsochroneErrorCode.INSUFFICIENT_ROUTING_DATA
            )
            return IsochroneResult(
                root=IsochroneFailure(
                    ok=False,
                    error={
                        "code": code,
                        "message": (
                            f"{_failure_message(routing_failure_codes)}"
                            f"测时成功 {success_count}/{sample_count}，"
                            f"有效边界方向 {len(boundary_points)}/{self.direction_count}，"
                            f"最大方向缺口 {largest_gap_steps}。"
                        ),
                        "retryable": "DAILY_QUOTA_EXHAUSTED" not in routing_failure_codes,
                    },
                    warnings=warnings,
                    meta=Meta(schema_version="1.0", provider="baidu"),
                )
            )

        confidence = _confidence(
            len(boundary_points), self.direction_count, success_count, sample_count
        )
        ring = [[point.location.lng, point.location.lat] for point in boundary_points]
        ring.append(ring[0])
        quality = IsochroneQuality(
            requested_directions=self.direction_count,
            valid_boundary_directions=len(boundary_points),
            routing_sample_count=sample_count,
            routing_success_count=success_count,
            sample_success_rate=success_count / sample_count,
            confidence=confidence,
            algorithm_version="radial-v1",
        )
        return IsochroneResult(
            root=IsochroneSuccess(
                ok=True,
                data=IsochroneData(
                    center=request.center,
                    time_limit_s=request.time_limit_s,
                    geometry=GeoJSONPolygon(type="Polygon", coordinates=[ring]),
                    algorithm_version="radial-v1",
                    boundary_points=boundary_points,
                    quality=quality,
                ),
                warnings=warnings,
                meta=Meta(schema_version="1.0", provider="baidu"),
            )
        )

    def _measure(
        self,
        request: IsochroneRequest,
        samples: list[SamplePoint],
        routing_failure_codes: set[str],
    ) -> tuple[list[MeasuredSample], int, int, bool]:
        if not samples:
            return [], 0, 0, False
        routing_request = RoutingRequest(
            schema_version="1.0",
            origin=request.center,
            targets=[sample.as_route_target() for sample in samples],
            travel_mode="walking",
        )
        try:
            result = self.route(routing_request).root
        except ProviderStopError:
            # Local budget / provider quota stop: never turn it into "missing".
            raise
        except Exception:
            return [], len(samples), 0, True
        if not result.ok:
            routing_failure_codes.add(result.error.code.value)
            if result.error.code.value == "RATE_LIMITED" and not result.error.retryable:
                routing_failure_codes.add("DAILY_QUOTA_EXHAUSTED")
            return [], len(samples), 0, True
        routing_failure_codes.update(
            warning.code.removeprefix("ROUTING_")
            for warning in result.warnings
            if warning.code in {"ROUTING_RATE_LIMITED", "ROUTING_DAILY_QUOTA_EXHAUSTED", "ROUTING_TIMEOUT"}
        )
        if (
            result.data.origin != request.center
            or result.data.travel_mode != routing_request.travel_mode
        ):
            return [], len(samples), 0, True
        by_id = {sample.sample_id: sample for sample in samples}
        returned_by_id = {}
        duplicate_ids = set()
        unexpected_target = False
        for route in result.data.routes:
            if route.target_id not in by_id:
                unexpected_target = True
            elif route.target_id in returned_by_id:
                duplicate_ids.add(route.target_id)
            else:
                returned_by_id[route.target_id] = route

        measured = []
        for sample_id, sample in by_id.items():
            route = returned_by_id.get(sample_id)
            if (
                route is not None
                and sample_id not in duplicate_ids
                and route.location == sample.location
                and route.status is RouteStatus.SUCCESS
                and route.duration_s is not None
            ):
                measured.append(MeasuredSample(sample, route.duration_s))
        missing = unexpected_target or len(measured) != len(samples)
        return measured, len(samples), len(measured), missing

    def _within_budget(self, used: int, samples: list[SamplePoint]) -> bool:
        return (
            self.max_routing_samples is None
            or used + len(samples) <= self.max_routing_samples
        )


def generate_isochrone(request: IsochroneRequest, route: Route) -> IsochroneResult:
    return IsochroneService(route).generate(request)


def _failure_message(routing_failure_codes: set[str]) -> str:
    if "DAILY_QUOTA_EXHAUSTED" in routing_failure_codes:
        return "检测到步行算路当日配额不足；有效步行测时数据不足，无法生成可靠等时圈。"
    if "RATE_LIMITED" in routing_failure_codes:
        return "检测到步行算路配额受限；有效步行测时数据不足，无法生成可靠等时圈。"
    if "TIMEOUT" in routing_failure_codes:
        return "检测到步行算路请求超时；有效步行测时数据不足，无法生成可靠等时圈。"
    return "有效步行测时数据不足，无法生成可靠等时圈。"


def _append_measurements(
    target: dict[float, list[MeasuredSample]], measured: list[MeasuredSample]
) -> None:
    for item in measured:
        target[item.sample.bearing_deg].append(item)


def _record_attempts(
    samples: list[SamplePoint],
    measured: list[MeasuredSample],
    attempted_by_bearing: dict[float, list[SamplePoint]],
    successful_sample_ids: set[str],
) -> None:
    for sample in samples:
        attempted_by_bearing[sample.bearing_deg].append(sample)
    successful_sample_ids.update(item.sample.sample_id for item in measured)


def _needs_extension(measured: list[MeasuredSample], time_limit_s: int) -> bool:
    return bool(measured) and max(item.duration_s for item in measured) <= time_limit_s


def _find_brackets(
    request: IsochroneRequest,
    bearing_values: list[float],
    measurements: dict[float, list[MeasuredSample]],
    attempted_by_bearing: dict[float, list[SamplePoint]],
    successful_sample_ids: set[str],
) -> dict[float, BoundaryBracket]:
    found = {}
    for bearing in bearing_values:
        bracket = find_bracket(
            request.center,
            bearing,
            measurements[bearing],
            request.time_limit_s,
        )
        if bracket is not None:
            lower = bracket.inner.sample.radial_distance_m
            upper = bracket.outer.sample.radial_distance_m
            unknown_between = any(
                lower < sample.radial_distance_m < upper
                and sample.sample_id not in successful_sample_ids
                for sample in attempted_by_bearing[bearing]
            )
            if not unknown_between:
                found[bearing] = bracket
    return found


def _largest_boundary_gap_steps(
    points: list[BoundaryPoint], requested_directions: int
) -> int:
    if len(points) < 2:
        return requested_directions
    step = 360.0 / requested_directions
    indexes = sorted(
        {round(point.bearing_deg / step) % requested_directions for point in points}
    )
    return max(
        (indexes[(index + 1) % len(indexes)] - bearing_index) % requested_directions
        for index, bearing_index in enumerate(indexes)
    )


def _boundary_points(
    request: IsochroneRequest, brackets: dict[float, BoundaryBracket]
) -> list[BoundaryPoint]:
    points = []
    for bearing, bracket in sorted(brackets.items()):
        radius = interpolate_radius(bracket, request.time_limit_s)
        points.append(
            BoundaryPoint(
                boundary_id=f"boundary:b{bearing:06.2f}".replace(".", "p"),
                bearing_deg=bearing,
                location=destination_point(request.center, bearing, radius),
                radial_distance_m=radius,
            )
        )
    return points


def _confidence(
    valid: int, requested: int, routing_success: int, routing_total: int
) -> IsochroneConfidence:
    valid_rate = valid / requested
    sample_rate = routing_success / routing_total if routing_total else 0.0
    if valid_rate >= 0.8 and sample_rate >= 0.9:
        return IsochroneConfidence.HIGH
    if valid_rate >= 0.6 and sample_rate >= 0.75:
        return IsochroneConfidence.MEDIUM
    return IsochroneConfidence.LOW


def _warnings(
    had_missing_routes: bool,
    valid: int,
    requested: int,
    confidence: IsochroneConfidence,
    *,
    include_approximation: bool = False,
    budget_exhausted: bool = False,
    largest_gap_steps: int | None = None,
    max_angular_gap_steps: int | None = None,
) -> list[WarningItem]:
    warnings = []
    if had_missing_routes:
        warnings.append(
            WarningItem(
                code="PARTIAL_ROUTING_RESULTS",
                message=(
                    "部分采样点的步行路线失败、缺失或目标不匹配；"
                    "相关走时按未知处理。"
                ),
            )
        )
    if valid < requested:
        warnings.extend(
            [
                WarningItem(
                    code="BOUNDARY_GAPS",
                    message="部分方向未能稳定确定目标时间边界。",
                ),
                WarningItem(
                    code="BOUNDARY_NOT_BRACKETED",
                    message="部分方向未形成有效的内外时间夹逼。",
                ),
            ]
        )
    if confidence is IsochroneConfidence.LOW:
        warnings.append(
            WarningItem(code="LOW_CONFIDENCE", message="等时圈结果置信度较低。")
        )
    if largest_gap_steps is not None:
        warnings.append(
            WarningItem(
                code="LARGE_BOUNDARY_GAP",
                message=(
                    f"有效边界最大缺口为 {largest_gap_steps} 个方向采样间隔，"
                    f"超过允许的 {max_angular_gap_steps} 个；"
                    "未输出该不完整等时圈。"
                ),
            )
        )
    if budget_exhausted:
        warnings.append(
            WarningItem(
                code="ROUTING_SAMPLE_BUDGET_REACHED",
                message=(
                    "本次采样预算不足以完成下一批采样，"
                    "剩余等时圈采样未执行。"
                ),
            )
        )
    if include_approximation:
        warnings.append(
            WarningItem(
                code="APPROXIMATE_WALKING_ISOCHRONE",
                message=(
                    "等时圈由真实步行路线采样并按方向径向插值近似生成，"
                    "未使用道路或建筑矢量面。"
                ),
            )
        )
    return warnings
