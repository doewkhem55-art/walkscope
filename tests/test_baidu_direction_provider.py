"""Baidu light walking direction provider: parsing and error mapping."""

import pytest

from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.routing import BaiduRoutingError
from app.schemas.common import CenterPoint

CENTER = CenterPoint(lng=121.5086, lat=31.2883, crs="BD09LL")
TARGET = CenterPoint(lng=121.51, lat=31.29, crs="BD09LL")


def provider_returning(payload):
    return BaiduDirectionProvider("dummy", transport=lambda url, timeout: payload)


def test_success_returns_distance_and_duration() -> None:
    route = provider_returning({
        "status": 0, "message": "ok",
        "result": {"routes": [{"distance": 380, "duration": 324}]},
    }).walking(CENTER, TARGET)

    assert route.distance_m == 380
    assert route.duration_s == 324


def test_rounded_one_metre_zero_second_route_keeps_the_observed_values():
    route = provider_returning({"status": 0, "result": {
        "routes": [{"distance": 1, "duration": 0}]}}).walking(CENTER, TARGET)
    assert (route.distance_m, route.duration_s) == (1, 0)


@pytest.mark.parametrize("distance,duration", [(2, 0), (0, 1)])
def test_other_zero_distance_duration_mismatches_remain_invalid(distance, duration):
    with pytest.raises(BaiduRoutingError):
        provider_returning({"status": 0, "result": {"routes": [
            {"distance": distance, "duration": duration}]}}).walking(CENTER, TARGET)


@pytest.mark.parametrize("status", [4, 302, "302"])
def test_day_quota_status_is_non_retryable_rate_limit(status) -> None:
    with pytest.raises(BaiduRoutingError) as excinfo:
        provider_returning({"status": status}).walking(CENTER, TARGET)

    assert excinfo.value.code == "RATE_LIMITED"
    assert excinfo.value.retryable is False


def test_permission_status_is_not_retryable() -> None:
    with pytest.raises(BaiduRoutingError) as excinfo:
        provider_returning({"status": 3}).walking(CENTER, TARGET)

    assert excinfo.value.code == "PROVIDER_ERROR"
    assert excinfo.value.retryable is False


def test_invalid_request_status() -> None:
    with pytest.raises(BaiduRoutingError) as excinfo:
        provider_returning({"status": 2}).walking(CENTER, TARGET)

    assert excinfo.value.code == "INVALID_REQUEST"


def test_missing_routes_is_an_invalid_response() -> None:
    with pytest.raises(BaiduRoutingError) as excinfo:
        provider_returning({"status": 0, "result": {"routes": []}}).walking(CENTER, TARGET)

    assert excinfo.value.code == "INVALID_RESPONSE"


def test_non_bd09ll_coordinates_are_rejected() -> None:
    # The contract already only allows BD09LL, so bypass validation to exercise
    # the provider's own guard (it must never silently query a wrong CRS).
    off_crs = CenterPoint.model_construct(lng=121.5, lat=31.2, crs="WGS84")

    with pytest.raises(BaiduRoutingError) as excinfo:
        provider_returning({"status": 0, "result": {"routes": [{"distance": 1, "duration": 1}]}}).walking(
            off_crs, TARGET
        )

    assert excinfo.value.code == "INVALID_REQUEST"


def test_http_429_is_a_retryable_rate_limit() -> None:
    import urllib.error

    def transport(url, timeout):
        raise urllib.error.HTTPError(url, 429, "too many", {}, None)

    with pytest.raises(BaiduRoutingError) as excinfo:
        BaiduDirectionProvider("dummy", transport=transport).walking(CENTER, TARGET)

    assert excinfo.value.code == "RATE_LIMITED"
    assert excinfo.value.retryable is True
