"""Single-route overflow channel: batch first, directionlite only after refusal.

The batch endpoint packs up to 50 routes per HTTP, so it always wins. When it
refuses for the day, the routing service continues one route per request so the
second daily quota pool can still be spent inside the same local HTTP budget.
"""

import pytest

from app.modules.routing.service import RoutingService
from app.providers.baidu.routing import BaiduRoutingError, ProviderRoute
from app.schemas.common import CenterPoint
from app.schemas.routing import RouteStatus, RoutingRequest

CENTER = CenterPoint(lng=121.5086, lat=31.2883, crs="BD09LL")


def request_with(count: int) -> RoutingRequest:
    return RoutingRequest.model_validate({
        "schema_version": "1.0",
        "origin": CENTER.model_dump(),
        "travel_mode": "walking",
        "targets": [
            {"target_id": f"target:{index}",
             "location": {"lng": 121.501 + index * 0.00001, "lat": 31.29, "crs": "BD09LL"}}
            for index in range(count)
        ],
    })


class QuotaBatchProvider:
    def __init__(self, *, error: BaiduRoutingError | None = None) -> None:
        self.calls = 0
        self.error = error or BaiduRoutingError("RATE_LIMITED", "day quota", retryable=False)

    def route_matrix(self, origin, destinations):
        self.calls += 1
        raise self.error


class TimeoutBatchProvider(QuotaBatchProvider):
    def __init__(self) -> None:
        super().__init__(error=BaiduRoutingError("TIMEOUT", "timeout", retryable=True))


class SingleRouteProvider:
    """Stands in for BaiduDirectionProvider (one route per call)."""

    def __init__(self) -> None:
        self.calls = 0

    def walking(self, origin, destination):
        self.calls += 1
        return ProviderRoute(distance_m=100 + self.calls, duration_s=80 + self.calls)


class WorkingBatchProvider:
    def __init__(self) -> None:
        self.calls = 0

    def route_matrix(self, origin, destinations):
        self.calls += 1
        return [ProviderRoute(200, 160) for _ in destinations]


class RefusingSingleRouteProvider:
    """A single-route provider that also hits its own daily quota."""

    def __init__(self) -> None:
        self.calls = 0

    def walking(self, origin, destination):
        self.calls += 1
        raise BaiduRoutingError("RATE_LIMITED", "day quota", retryable=False)


def test_batch_quota_switches_to_single_route_and_keeps_going() -> None:
    batch, fallback = QuotaBatchProvider(), SingleRouteProvider()
    service = RoutingService(batch, batch_size=3, fallback_provider=fallback)

    result = service.route(request_with(4)).root

    assert result.ok is True
    assert all(route.status is RouteStatus.SUCCESS for route in result.data.routes)
    assert batch.calls == 1            # the batch endpoint is not retried after refusal
    assert fallback.calls == 4         # every target measured one HTTP at a time
    assert service.fallback_pairs == 4
    assert result.data.summary.success == 4


def test_fallback_is_sticky_so_later_calls_skip_the_batch_endpoint() -> None:
    batch, fallback = QuotaBatchProvider(), SingleRouteProvider()
    service = RoutingService(batch, batch_size=3, fallback_provider=fallback)

    service.route(request_with(2))
    service.route(request_with(2))

    assert batch.calls == 1            # no second batch attempt
    assert fallback.calls == 4


def test_report_does_not_label_the_fallback() -> None:
    batch, fallback = QuotaBatchProvider(), SingleRouteProvider()
    service = RoutingService(batch, batch_size=3, fallback_provider=fallback)

    result = service.route(request_with(2)).root

    codes = [warning.code for warning in result.warnings]
    assert all("FALLBACK" not in code and "SINGLE_ROUTE" not in code for code in codes)


def test_batch_failure_without_fallback_still_stops_sticky() -> None:
    batch = QuotaBatchProvider()
    service = RoutingService(batch, batch_size=3)

    first = service.route(request_with(2)).root
    second = service.route(request_with(2)).root

    assert first.ok is False
    assert second.ok is False
    assert batch.calls == 1
    assert service.fallback_pairs == 0


def test_fallback_quota_refusal_stops_the_task() -> None:
    batch, fallback = QuotaBatchProvider(), RefusingSingleRouteProvider()
    service = RoutingService(batch, batch_size=3, fallback_provider=fallback)

    first = service.route(request_with(2)).root
    second = service.route(request_with(2)).root

    assert first.ok is False
    assert second.ok is False
    assert batch.calls == 1            # batch refused once
    assert fallback.calls == 1         # fallback refused once, then the task stopped
    assert service.fallback_pairs == 1


def test_retryable_batch_error_never_triggers_the_fallback() -> None:
    batch, fallback = TimeoutBatchProvider(), SingleRouteProvider()
    slept: list[float] = []
    service = RoutingService(batch, batch_size=3, max_retries=1, retry_delay_seconds=0.5,
                             sleep=slept.append, fallback_provider=fallback)

    result = service.route(request_with(2)).root

    assert result.ok is False
    assert batch.calls == 2            # one retry of the same batch endpoint
    assert fallback.calls == 0         # a transient timeout is not a quota refusal
    assert slept == [0.5]


def test_fallback_stays_unused_while_the_batch_endpoint_works() -> None:
    batch, fallback = WorkingBatchProvider(), SingleRouteProvider()
    service = RoutingService(batch, batch_size=3, fallback_provider=fallback)

    result = service.route(request_with(4)).root

    assert result.ok is True
    assert batch.calls == 2
    assert fallback.calls == 0
    assert service.fallback_pairs == 0
