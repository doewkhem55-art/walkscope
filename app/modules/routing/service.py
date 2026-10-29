"""Routing service with batching, limited retry, and partial-result handling."""

import time
from collections.abc import Callable

from app.errors import ProviderStopError
from app.modules.routing.batching import iter_batches
from app.providers.baidu.routing import (
    BaiduRoutingError,
    BaiduRoutingProvider,
    ProviderRoute,
)
from app.schemas.common import Meta, WarningItem
from app.schemas.routing import (
    RouteResult,
    RouteStatus,
    RouteTarget,
    RoutingData,
    RoutingErrorCode,
    RoutingFailure,
    RoutingRequest,
    RoutingResult,
    RoutingSuccess,
    RoutingSummary,
)


_PROVIDER_MESSAGE = "百度地图步行批量算路服务请求失败。"

# Stop reasons that must survive every later route() call in the same task.
_STOP_MESSAGES = {
    "PROVIDER_BUDGET_EXHAUSTED": "本次体检达到地图请求次数或时长上限，已停止新的步行算路请求。",
    "ROUTING_DAILY_QUOTA_EXHAUSTED": "百度步行批量算路当日配额不足，已停止新的步行算路请求。",
}


class RoutingService:
    def __init__(
        self,
        provider: BaiduRoutingProvider,
        *,
        batch_size: int = 50,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        fallback_provider: object | None = None,
    ) -> None:
        if not 1 <= batch_size <= 50:
            raise ValueError("单批路线数必须在 1 到 50 之间。")
        if max_retries < 0 or retry_delay_seconds < 0:
            raise ValueError("重试次数和延迟不能为负数。")
        self.provider = provider
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.sleep = sleep
        # Overflow channel: single-route walking directions (one HTTP per pair).
        # Used only after the batch endpoint refuses for quota, so a second
        # daily quota pool can still be spent inside the same HTTP budget.
        self.fallback_provider = fallback_provider
        self.fallback_pairs = 0
        self.fallback_batches = 0
        # Sticky for the whole task: once the provider refuses for quota or the
        # local budget is spent, later route() calls must not issue new HTTP.
        self._stop_code: str | None = None
        self._batch_quota_exhausted = False
        self._fallback_announced = False

    def route(self, request: RoutingRequest) -> RoutingResult:
        if self._stop_code is not None:
            return self._stopped_result()

        routes: list[RouteResult] = []
        first_error: BaiduRoutingError | None = None
        successful_batches = 0
        daily_quota_exhausted = False

        for batch in iter_batches(request.targets, self.batch_size):
            if daily_quota_exhausted:
                routes.extend(_unavailable(target) for target in batch)
                continue
            try:
                provider_routes = self._measure_batch(request, batch)
            except ProviderStopError as exc:
                # Local request/time budget: stop at once, with no retry wait
                # and no further batch or later call spending HTTP.
                self._stop_code = exc.code
                raise
            except BaiduRoutingError as exc:
                daily_quota_exhausted = exc.code == "RATE_LIMITED" and not exc.retryable
                if daily_quota_exhausted:
                    # Both the batch endpoint and the single-route fallback
                    # refused: nothing left to spend today.
                    self._stop_code = "ROUTING_DAILY_QUOTA_EXHAUSTED"
                if daily_quota_exhausted or first_error is None:
                    first_error = exc
                routes.extend(_unavailable(target) for target in batch)
                continue
            successful_batches += 1
            routes.extend(
                _public_route(request, target, provider_route)
                for target, provider_route in zip(batch, provider_routes, strict=True)
            )

        meta = Meta(schema_version="1.0", provider="baidu")
        if successful_batches == 0:
            error = first_error or BaiduRoutingError(
                "PROVIDER_ERROR", _PROVIDER_MESSAGE, retryable=True
            )
            return RoutingResult(
                root=RoutingFailure(
                    ok=False,
                    error={
                        "code": _public_error_code(error.code),
                        "message": _safe_error_message(error),
                        "retryable": error.retryable,
                    },
                    warnings=_quota_stop_warnings(error),
                    meta=meta,
                )
            )

        summary = _summary(routes)
        warnings: list[WarningItem] = []
        if summary.unavailable:
            warnings.append(
                WarningItem(
                    code="PARTIAL_ROUTING_RESULTS",
                    message="部分目标点的步行数据暂时不可用。",
                )
            )
        if first_error and first_error.code in {"RATE_LIMITED", "TIMEOUT"}:
            daily_quota = first_error.code == "RATE_LIMITED" and not first_error.retryable
            warnings.append(
                WarningItem(
                    code="ROUTING_DAILY_QUOTA_EXHAUSTED" if daily_quota else f"ROUTING_{first_error.code}",
                    message=(
                        "步行算路当日配额不足，部分批次未完成。"
                        if daily_quota
                        else "部分步行算路批次受并发限制或临时限流。"
                        if first_error.code == "RATE_LIMITED"
                        else "部分步行算路批次请求超时。"
                    ),
                )
            )
        if summary.no_route:
            warnings.append(
                WarningItem(
                    code="NO_ROUTE_FOR_TARGETS",
                    message="部分目标点没有可用步行路线。",
                )
            )
        return RoutingResult(
            root=RoutingSuccess(
                ok=True,
                data=RoutingData(
                    origin=request.origin,
                    travel_mode=request.travel_mode,
                    routes=routes,
                    summary=summary,
                ),
                warnings=warnings,
                meta=meta,
            )
        )

    def _stopped_result(self) -> RoutingResult:
        """No-HTTP result for a task that is already stopped.

        The public envelope stays a RoutingResult either way: the local budget
        stop is re-raised as control flow, the provider quota stop is reported
        as the existing non-retryable RATE_LIMITED failure with its warning.
        """
        code = self._stop_code or "ROUTING_DAILY_QUOTA_EXHAUSTED"
        message = _STOP_MESSAGES.get(code, "本次体检已停止新的步行算路请求。")
        if code == "PROVIDER_BUDGET_EXHAUSTED":
            raise ProviderStopError(code, message)
        return RoutingResult(
            root=RoutingFailure(
                ok=False,
                error={"code": RoutingErrorCode.RATE_LIMITED, "message": message, "retryable": False},
                warnings=[WarningItem(code="ROUTING_DAILY_QUOTA_EXHAUSTED", message=message)],
                meta=Meta(schema_version="1.0", provider="baidu"),
            )
        )

    def _measure_batch(
        self, request: RoutingRequest, batch: list[RouteTarget]
    ) -> list[ProviderRoute]:
        """Batch endpoint first; single-route overflow only after a quota refusal."""
        if not self._batch_quota_exhausted:
            try:
                return self._route_batch(request, batch)
            except BaiduRoutingError as exc:
                if self.fallback_provider is None or not (
                    exc.code == "RATE_LIMITED" and not exc.retryable
                ):
                    raise
                # Batch quota for the day is gone: stop calling it and continue
                # with one-route requests so the other quota pool can be spent.
                self._batch_quota_exhausted = True
                self._fallback_announced = True
        if self.fallback_provider is None:
            raise BaiduRoutingError(
                "RATE_LIMITED", "百度步行批量算路当日配额不足。", retryable=False
            )
        return self._fallback_batch(request, batch)

    def _fallback_batch(
        self, request: RoutingRequest, batch: list[RouteTarget]
    ) -> list[ProviderRoute]:
        """Measure one target per HTTP request through the fallback provider."""
        routes: list[ProviderRoute] = []
        for target in batch:
            self.fallback_pairs += 1
            routes.append(self.fallback_provider.walking(request.origin, target.location))
        self.fallback_batches += 1
        return routes

    def _route_batch(
        self, request: RoutingRequest, batch: list[RouteTarget]
    ) -> list[ProviderRoute]:
        for attempt in range(self.max_retries + 1):
            try:
                return self.provider.route_matrix(
                    request.origin, [target.location for target in batch]
                )
            except BaiduRoutingError as exc:
                if not exc.retryable or attempt == self.max_retries:
                    raise
                self.sleep(self.retry_delay_seconds * (attempt + 1))
        raise AssertionError("unreachable")


def route_targets(
    request: RoutingRequest, provider: BaiduRoutingProvider
) -> RoutingResult:
    return RoutingService(provider).route(request)


def _quota_stop_warnings(error: BaiduRoutingError) -> list[WarningItem]:
    """Make a refused-quota failure say so, as the partial-success path does."""
    if error.code == "RATE_LIMITED" and not error.retryable:
        return [
            WarningItem(
                code="ROUTING_DAILY_QUOTA_EXHAUSTED",
                message="百度步行批量算路当日配额不足，已停止新的步行算路请求。",
            )
        ]
    return []


def _public_route(
    request: RoutingRequest, target: RouteTarget, route: ProviderRoute
) -> RouteResult:
    same_point = target.location == request.origin
    no_route = route.distance_m == 0 and route.duration_s == 0 and not same_point
    return RouteResult(
        target_id=target.target_id,
        location=target.location,
        status=RouteStatus.NO_ROUTE if no_route else RouteStatus.SUCCESS,
        distance_m=None if no_route else route.distance_m,
        duration_s=None if no_route else route.duration_s,
    )


def _unavailable(target: RouteTarget) -> RouteResult:
    return RouteResult(
        target_id=target.target_id,
        location=target.location,
        status=RouteStatus.UNAVAILABLE,
        distance_m=None,
        duration_s=None,
    )


def _summary(routes: list[RouteResult]) -> RoutingSummary:
    return RoutingSummary(
        requested=len(routes),
        success=sum(route.status is RouteStatus.SUCCESS for route in routes),
        no_route=sum(route.status is RouteStatus.NO_ROUTE for route in routes),
        unavailable=sum(route.status is RouteStatus.UNAVAILABLE for route in routes),
    )


def _public_error_code(code: str) -> RoutingErrorCode:
    try:
        return RoutingErrorCode(code)
    except ValueError:
        return RoutingErrorCode.PROVIDER_ERROR


def _safe_error_message(error: BaiduRoutingError) -> str:
    if error.code == "RATE_LIMITED":
        return (
            "百度步行批量算路当日配额不足。"
            if not error.retryable
            else "百度步行批量算路受并发限制或临时限流。"
        )
    if error.code == "TIMEOUT":
        return "百度步行批量算路请求超时。"
    return _PROVIDER_MESSAGE
