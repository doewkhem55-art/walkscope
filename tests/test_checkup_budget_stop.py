"""Stop-behaviour tests for the fixed checkup main chain (offline, no network).

Covers the four confirmed blockers: float coverage ratios leaving [0, 1], the
RouteMatrix quota state not surviving a batch, the local 450 HTTP / 20 minute
limit turning into a retryable provider timeout, and a generic handler turning
a stop signal into "unknown" evidence.
"""

import pytest

from app.errors import ProviderStopError
from app.modules.blindspot.service import _measure
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import _coverage_ratios, analyze_coverage
from app.modules.routing.service import RoutingService
from app.providers.baidu.routing import BaiduRoutingError
from app.schemas.common import CenterPoint
from app.schemas.isochrone import GeoJSONPolygon
from app.schemas.routing import RoutingRequest


CENTER = CenterPoint(lng=121.5086, lat=31.2883, crs="BD09LL")


def routing_request(count: int) -> RoutingRequest:
    return RoutingRequest.model_validate(
        {
            "schema_version": "1.0",
            "origin": CENTER.model_dump(),
            "targets": [
                {
                    "target_id": f"target:{index}",
                    "location": {"lng": 121.501 + index * 0.00001, "lat": 31.29, "crs": "BD09LL"},
                }
                for index in range(count)
            ],
            "travel_mode": "walking",
        }
    )


# --- coverage ratios stay inside [0, 1] and keep unclassified area unknown ---


def test_coverage_ratios_clamp_float_overflow_and_sum_to_one() -> None:
    area = 1000.0
    ratios = _coverage_ratios(
        {"covered": area * 1.0000000000000002, "blind": 0.0, "unknown": 0.0}, area
    )
    assert ratios["covered_ratio"] == 1.0
    assert ratios["blind_ratio"] == 0.0
    assert ratios["unknown_ratio"] == 0.0
    assert sum(ratios.values()) == pytest.approx(1.0)


def test_coverage_ratios_treat_unclassified_area_as_unknown() -> None:
    area = 1000.0
    ratios = _coverage_ratios({"covered": 400.0, "blind": 100.0, "unknown": 0.0}, area)
    assert ratios["unknown_ratio"] == pytest.approx(0.5)
    assert sum(ratios.values()) == pytest.approx(1.0)


# --- routing stop state ---


class QuotaProvider:
    def __init__(self) -> None:
        self.calls = 0

    def route_matrix(self, origin, destinations):
        self.calls += 1
        raise BaiduRoutingError("RATE_LIMITED", "quota", retryable=False)


class BudgetProvider:
    def __init__(self) -> None:
        self.calls = 0

    def route_matrix(self, origin, destinations):
        self.calls += 1
        raise ProviderStopError("PROVIDER_BUDGET_EXHAUSTED", "budget")


def test_provider_quota_is_sticky_and_keeps_completed_batches() -> None:
    provider = QuotaProvider()
    service = RoutingService(provider, batch_size=3)

    first = service.route(routing_request(4)).root
    assert first.ok is False
    assert provider.calls == 1  # the second batch spends no HTTP
    assert any(w.code == "ROUTING_DAILY_QUOTA_EXHAUSTED" for w in first.warnings)

    second = service.route(routing_request(2)).root
    assert second.ok is False
    assert provider.calls == 1  # sticky across later calls in the same task


def test_local_budget_stops_at_once_without_retry_wait() -> None:
    provider = BudgetProvider()
    slept: list[float] = []
    service = RoutingService(
        provider, batch_size=3, max_retries=3, retry_delay_seconds=2, sleep=slept.append
    )

    with pytest.raises(ProviderStopError):
        service.route(routing_request(6))
    assert provider.calls == 1  # no provider retry
    assert slept == []  # no backoff sleep

    with pytest.raises(ProviderStopError):
        service.route(routing_request(1))
    assert provider.calls == 1  # sticky: no HTTP after the stop


# --- a stop signal is control flow, never "unknown" evidence ---


class CenterPOI:
    poi_id = "baidu:test"
    location = CENTER


def test_measure_propagates_stop_instead_of_returning_unknown() -> None:
    def route(request):
        raise ProviderStopError("PROVIDER_BUDGET_EXHAUSTED", "budget")

    with pytest.raises(ProviderStopError):
        _measure(CENTER, [CenterPOI()], route, [])


def test_grid_coverage_stops_and_reports_unknown() -> None:
    catalog = load_catalog()
    facility = next(
        item
        for item in catalog["facilities"]
        if item.get("coverage_method") == "grid_walking_distance"
        and item.get("service_distance_m")
    )
    geometry = GeoJSONPolygon(
        type="Polygon",
        coordinates=[
            [
                [CENTER.lng - 0.001, CENTER.lat - 0.001],
                [CENTER.lng + 0.001, CENTER.lat - 0.001],
                [CENTER.lng + 0.001, CENTER.lat + 0.001],
                [CENTER.lng - 0.001, CENTER.lat + 0.001],
                [CENTER.lng - 0.001, CENTER.lat - 0.001],
            ]
        ],
    )
    category = {
        "facility_id": facility["id"],
        "name_zh": facility["name_zh"],
        "group_id": facility["group_id"],
        "search_status": "complete",
        "count": 1,
        "known_count": 1,
        "pois": [
            {
                "poi_id": "baidu:test",
                "location": {"lng": CENTER.lng, "lat": CENTER.lat, "crs": "BD09LL"},
                "in_isochrone": True,
            }
        ],
        "warnings": [],
    }

    def route(request):
        raise ProviderStopError("PROVIDER_BUDGET_EXHAUSTED", "budget")

    warnings: list = []
    ratios, _blindspot, quality = analyze_coverage(
        CENTER, geometry, [category], catalog, route, warnings
    )

    assert quality["coverage_stop_reason"] == "PROVIDER_BUDGET_EXHAUSTED"
    assert any(warning.code == "PROVIDER_BUDGET_EXHAUSTED" for warning in warnings)
    assert ratios[facility["id"]]["unknown_ratio"] == 1.0
    assert sum(ratios[facility["id"]].values()) == pytest.approx(1.0)


class UnknownStopProvider:
    def __init__(self) -> None:
        self.calls = 0

    def route_matrix(self, origin, destinations):
        self.calls += 1
        raise ProviderStopError("SOMETHING_ELSE", "injected")


def test_unknown_stop_code_stops_instead_of_raising_key_error() -> None:
    provider = UnknownStopProvider()
    service = RoutingService(provider, batch_size=3)

    with pytest.raises(ProviderStopError):
        service.route(routing_request(2))
    second = service.route(routing_request(2)).root
    assert second.ok is False
    assert provider.calls == 1


def test_stopped_grid_asks_for_no_further_routes() -> None:
    catalog = load_catalog()
    facility = next(
        item
        for item in catalog["facilities"]
        if item.get("coverage_method") == "grid_walking_distance"
        and item.get("service_distance_m")
    )
    half = 0.003  # several 200 m cells
    geometry = GeoJSONPolygon(
        type="Polygon",
        coordinates=[
            [
                [CENTER.lng - half, CENTER.lat - half],
                [CENTER.lng + half, CENTER.lat - half],
                [CENTER.lng + half, CENTER.lat + half],
                [CENTER.lng - half, CENTER.lat + half],
                [CENTER.lng - half, CENTER.lat - half],
            ]
        ],
    )
    category = {
        "facility_id": facility["id"],
        "name_zh": facility["name_zh"],
        "group_id": facility["group_id"],
        "search_status": "complete",
        "count": 1,
        "known_count": 1,
        "pois": [
            {
                "poi_id": "baidu:test",
                "location": {"lng": CENTER.lng, "lat": CENTER.lat, "crs": "BD09LL"},
                "in_isochrone": True,
            }
        ],
        "warnings": [],
    }
    calls = {"count": 0}

    def route(request):
        calls["count"] += 1
        raise ProviderStopError("PROVIDER_BUDGET_EXHAUSTED", "budget")

    ratios, _blindspot, quality = analyze_coverage(
        CENTER, geometry, [category], catalog, route, []
    )

    assert calls["count"] == 1  # one stop, then no further route request
    assert quality["coverage_stop_reason"] == "PROVIDER_BUDGET_EXHAUSTED"
    assert sum(ratios[facility["id"]].values()) == pytest.approx(1.0)
