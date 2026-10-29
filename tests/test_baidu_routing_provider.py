import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.providers.baidu.routing import (
    BaiduRoutingError,
    BaiduRoutingProvider,
)
from app.modules.routing.service import RoutingService
from app.schemas.common import CenterPoint
from app.schemas.routing import RouteStatus, RoutingRequest


ORIGIN = CenterPoint(lng=121.506123, lat=31.282456, crs="BD09LL")
DESTINATIONS = [
    CenterPoint(lng=121.5081, lat=31.2809, crs="BD09LL"),
    CenterPoint(lng=121.5092, lat=31.2921, crs="BD09LL"),
]


def test_provider_builds_lat_lng_request_and_maps_results() -> None:
    seen = {}

    def transport(url: str, timeout: float):
        seen["url"] = url
        seen["timeout"] = timeout
        return {
            "status": 0,
            "result": [
                {"distance": {"text": "1公里", "value": 842}, "duration": {"text": "12分钟", "value": 735}},
                {"distance": {"value": 1280.4}, "duration": {"value": 1012.2}},
            ],
        }

    routes = BaiduRoutingProvider("secret-ak", transport=transport).route_matrix(
        ORIGIN, DESTINATIONS
    )
    query = parse_qs(urlsplit(seen["url"]).query)
    assert query["origins"] == ["31.282456,121.506123"]
    assert query["destinations"] == [
        "31.280900,121.508100|31.292100,121.509200"
    ]
    assert query["coord_type"] == ["bd09ll"]
    assert routes[0].distance_m == 842
    assert routes[1].duration_s == 1012


def test_provider_rejects_more_than_fifty_destinations() -> None:
    provider = BaiduRoutingProvider("secret", transport=lambda *_: {})
    with pytest.raises(BaiduRoutingError) as caught:
        provider.route_matrix(ORIGIN, [DESTINATIONS[0]] * 51)
    assert caught.value.code == "INVALID_REQUEST"


def test_provider_preserves_one_meter_zero_second_route_and_its_batch() -> None:
    # Real Baidu responses can round a very short route to 1 m / 0 s. One
    # rounded result must not discard the other 24 valid routes in the batch.
    items = [{"distance": {"value": 1}, "duration": {"value": 0}}]
    items.extend({"distance": {"value": 100}, "duration": {"value": 80}} for _ in range(24))
    provider = BaiduRoutingProvider("secret", transport=lambda *_: {"status": 0, "result": items})
    routes = provider.route_matrix(ORIGIN, [DESTINATIONS[0]] * 25)
    assert len(routes) == 25
    assert (routes[0].distance_m, routes[0].duration_s) == (1, 0)
    assert all(route.duration_s == 80 for route in routes[1:])


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(1, "PROVIDER_ERROR", True), (2, "INVALID_REQUEST", False),
     (4, "RATE_LIMITED", False), (302, "RATE_LIMITED", False),
     (401, "RATE_LIMITED", True), (210, "PROVIDER_ERROR", False)],
)
def test_provider_maps_baidu_status(status: int, code: str, retryable: bool) -> None:
    provider = BaiduRoutingProvider(
        "secret", transport=lambda *_: {"status": status, "message": "raw secret"}
    )
    with pytest.raises(BaiduRoutingError) as caught:
        provider.route_matrix(ORIGIN, DESTINATIONS[:1])
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "secret" not in str(caught.value)


def test_provider_rejects_wrong_result_count() -> None:
    provider = BaiduRoutingProvider(
        "secret", transport=lambda *_: {"status": 0, "result": []}
    )
    with pytest.raises(BaiduRoutingError) as caught:
        provider.route_matrix(ORIGIN, DESTINATIONS[:1])
    assert caught.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize('distance,duration', [(0, 10), (100, 0)])
def test_provider_rejects_inconsistent_zero_values(distance, duration) -> None:
    provider = BaiduRoutingProvider(
        "secret",
        transport=lambda *_: {
            "status": 0,
            "result": [{"distance": {"value": distance}, "duration": {"value": duration}}],
        },
    )
    with pytest.raises(BaiduRoutingError):
        provider.route_matrix(ORIGIN, DESTINATIONS[:1])


@pytest.mark.parametrize(
    ("error", "code", "retryable", "private_text"),
    [
        (TimeoutError("private budget detail"), "TIMEOUT", True, "private"),
        (URLError(TimeoutError("private timeout detail")), "TIMEOUT", True, "private"),
        (URLError("private URL detail"), "PROVIDER_ERROR", True, "private"),
        (
            HTTPError("https://example.invalid", 429, "private HTTP detail", {}, None),
            "RATE_LIMITED",
            True,
            "private",
        ),
        (
            HTTPError("https://example.invalid", 503, "private HTTP detail", {}, None),
            "PROVIDER_ERROR",
            True,
            "private",
        ),
        (
            json.JSONDecodeError("private JSON detail", "{}", 0),
            "PROVIDER_ERROR",
            False,
            "private",
        ),
        (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private encoding detail"),
            "PROVIDER_ERROR",
            False,
            "private",
        ),
    ],
)
def test_provider_normalizes_transport_errors_without_exposing_details(
    error: Exception, code: str, retryable: bool, private_text: str
) -> None:
    def transport(*_args):
        raise error

    provider = BaiduRoutingProvider("secret-ak", transport=transport)
    with pytest.raises(BaiduRoutingError) as caught:
        provider.route_matrix(ORIGIN, DESTINATIONS[:1])

    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert private_text not in str(caught.value)
    assert "secret-ak" not in str(caught.value)


def test_real_routing_service_keeps_first_fifty_when_second_batch_times_out() -> None:
    calls = 0

    def transport(url: str, timeout: float):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("Checkup request budget exhausted")
        destinations = parse_qs(urlsplit(url).query)["destinations"][0].split("|")
        return {
            "status": 0,
            "result": [
                {"distance": {"value": 100}, "duration": {"value": 80}}
                for _ in destinations
            ],
        }

    request = RoutingRequest.model_validate(
        {
            "schema_version": "1.0",
            "origin": ORIGIN.model_dump(),
            "targets": [
                {
                    "target_id": f"target:{index}",
                    "location": {
                        "lng": 121.51 + index * 0.00001,
                        "lat": 31.29,
                        "crs": "BD09LL",
                    },
                }
                for index in range(51)
            ],
            "travel_mode": "walking",
        }
    )
    result = RoutingService(
        BaiduRoutingProvider("secret-ak", transport=transport), max_retries=0
    ).route(request).root

    assert result.ok is True
    assert calls == 2
    assert len(result.data.routes) == 51
    assert result.data.summary.success == 50
    assert result.data.summary.unavailable == 1
    assert all(route.status is RouteStatus.SUCCESS for route in result.data.routes[:50])
    assert result.data.routes[50].status is RouteStatus.UNAVAILABLE
    assert [warning.code for warning in result.warnings] == [
        "PARTIAL_ROUTING_RESULTS", "ROUTING_TIMEOUT"
    ]
