"""Minimal Baidu light walking direction provider (single origin -> destination).

Used only as an overflow channel: when the batch RouteMatrix endpoint refuses
for the day, the routing service continues with these one-route calls so the
remaining local HTTP budget can still be spent. Errors reuse
``BaiduRoutingError`` so the routing service applies exactly the same
quota/retry semantics to both endpoints.
"""

from __future__ import annotations

import json
import math
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from app.providers.baidu.routing import BaiduRoutingError, ProviderRoute
from app.schemas.common import CenterPoint


DIRECTION_URL = "https://api.map.baidu.com/directionlite/v1/walking"
Transport = Callable[[str, float], Mapping[str, Any]]


class BaiduDirectionProvider:
    """Single-route walking directions; one HTTP request per route pair."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 8.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("百度服务端 AK 不能为空。")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是正数。")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _get_json

    def walking(self, origin: CenterPoint, destination: CenterPoint) -> ProviderRoute:
        params = {
            "origin": _format_point(origin),
            "destination": _format_point(destination),
            "coord_type": "bd09ll",
            "output": "json",
            "ak": self._api_key,
        }
        try:
            payload = self._transport(
                f"{DIRECTION_URL}?{urlencode(params)}", self._timeout_seconds
            )
        except BaiduRoutingError:
            raise
        except HTTPError as exc:
            code = "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR"
            raise BaiduRoutingError(
                code,
                "百度步行路线规划 HTTP 请求失败。",
                retryable=exc.code == 429 or exc.code >= 500,
            ) from None
        except (socket.timeout, TimeoutError):
            raise BaiduRoutingError(
                "TIMEOUT", "百度步行路线规划请求超时。", retryable=True
            ) from None
        except URLError as exc:
            is_timeout = isinstance(
                getattr(exc, "reason", None), (socket.timeout, TimeoutError)
            )
            raise BaiduRoutingError(
                "TIMEOUT" if is_timeout else "PROVIDER_ERROR",
                "百度步行路线规划请求失败。",
                retryable=True,
            ) from None
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise BaiduRoutingError(
                "PROVIDER_ERROR", "百度步行路线规划响应无效。", retryable=False
            ) from None

        if not isinstance(payload, Mapping):
            raise _invalid_response()
        status = payload.get("status")
        if status in {4, 302, "4", "302"}:
            raise BaiduRoutingError(
                "RATE_LIMITED", "百度步行路线规划当日配额不足。", retryable=False
            )
        if status != 0:
            raise _status_error(status)
        result = payload.get("result")
        routes = result.get("routes") if isinstance(result, Mapping) else None
        if not isinstance(routes, list) or not routes:
            raise _invalid_response()
        return _parse_route(routes[0])


def _format_point(point: CenterPoint) -> str:
    if point.crs != "BD09LL":
        raise BaiduRoutingError(
            "INVALID_REQUEST", "步行路线规划仅接受 BD09LL 坐标。", retryable=False
        )
    return f"{point.lat:.6f},{point.lng:.6f}"


def _parse_route(item: Any) -> ProviderRoute:
    if not isinstance(item, Mapping):
        raise _invalid_response()
    distance_m = _nonnegative_int(item.get("distance"))
    duration_s = _nonnegative_int(item.get("duration"))
    rounded_short_route = distance_m == 1 and duration_s == 0
    if distance_m is None or duration_s is None or (
        ((distance_m == 0) != (duration_s == 0)) and not rounded_short_route
    ):
        raise _invalid_response()
    return ProviderRoute(distance_m=distance_m, duration_s=duration_s)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(round(number))


def _status_error(status: Any) -> BaiduRoutingError:
    if status == 2:
        return BaiduRoutingError(
            "INVALID_REQUEST", "百度步行路线规划参数无效。", retryable=False
        )
    if status == 429:
        return BaiduRoutingError(
            "RATE_LIMITED", "百度步行路线规划并发超限。", retryable=True
        )
    if status in {3, 5}:
        return BaiduRoutingError(
            "PROVIDER_ERROR", "百度步行路线规划权限或 AK 校验失败。", retryable=False
        )
    return BaiduRoutingError(
        "PROVIDER_ERROR", "百度步行路线规划服务返回错误。", retryable=status == 1
    )


def _invalid_response() -> BaiduRoutingError:
    return BaiduRoutingError(
        "INVALID_RESPONSE", "百度步行路线规划响应格式无效。", retryable=False
    )


def _get_json(url: str, timeout_seconds: float) -> Mapping[str, Any]:
    with urlopen(url, timeout=timeout_seconds) as response:
        return json.load(response)
