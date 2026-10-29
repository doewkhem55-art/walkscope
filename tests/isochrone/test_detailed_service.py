from app.modules.isochrone.service import IsochroneService
from app.schemas.common import WarningItem
from app.schemas.isochrone import IsochroneRequest
from app.schemas.routing import RoutingResult


def _request() -> IsochroneRequest:
    return IsochroneRequest.model_validate(
        {
            "schema_version": "1.0",
            "center": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
            "time_limit_s": 900,
        }
    )


class RadialRouter:
    def __init__(
        self,
        seconds_per_meter: float = 0.8,
        *,
        missing_target_ids: set[str] | None = None,
        missing_bearings: set[str] | None = None,
        wrong_target_ids: bool = False,
        mislocated_target_ids: set[str] | None = None,
    ) -> None:
        self.seconds_per_meter = seconds_per_meter
        self.missing_target_ids = missing_target_ids or set()
        self.missing_bearings = missing_bearings or set()
        self.wrong_target_ids = wrong_target_ids
        self.mislocated_target_ids = mislocated_target_ids or set()
        self.calls: list[int] = []

    def __call__(self, request):
        self.calls.append(len(request.targets))
        routes = []
        for target in request.targets:
            missing = target.target_id in self.missing_target_ids or any(
                f":b{bearing}:" in target.target_id
                for bearing in self.missing_bearings
            )
            radius = float(target.target_id.rsplit(":r", 1)[1].replace("p", "."))
            location = target.location.model_dump()
            if target.target_id in self.mislocated_target_ids:
                location["lng"] += 0.001
            routes.append(
                {
                    "target_id": (
                        f"wrong:{target.target_id}"
                        if self.wrong_target_ids
                        else target.target_id
                    ),
                    "location": location,
                    "status": "unavailable" if missing else "success",
                    "distance_m": None if missing else round(radius),
                    "duration_s": (
                        None if missing else round(radius * self.seconds_per_meter)
                    ),
                }
            )
        success = sum(route["status"] == "success" for route in routes)
        return RoutingResult.model_validate(
            {
                "ok": True,
                "data": {
                    "origin": request.origin.model_dump(),
                    "travel_mode": "walking",
                    "routes": routes,
                    "summary": {
                        "requested": len(routes),
                        "success": success,
                        "no_route": 0,
                        "unavailable": len(routes) - success,
                    },
                },
                "warnings": [],
                "meta": {"schema_version": "1.0", "provider": "mock"},
            }
        )


def test_detailed_factory_samples_72_directions_with_a_720_target_cap() -> None:
    router = RadialRouter(seconds_per_meter=0.35)
    service = IsochroneService.detailed(router)

    result = service.generate(_request()).root

    assert result.ok is True
    assert result.data.quality.requested_directions == 72
    assert result.data.quality.valid_boundary_directions == 72
    assert result.data.quality.routing_sample_count == 720
    assert sum(router.calls) <= 720
    assert "APPROXIMATE_WALKING_ISOCHRONE" in [
        warning.code for warning in result.warnings
    ]


def test_unknown_inner_sample_is_not_skipped_to_make_a_boundary() -> None:
    router = RadialRouter(
        missing_target_ids={"sample:b000p00:r0900p00"},
    )

    result = IsochroneService.detailed(router).generate(_request()).root

    assert result.ok is True
    assert result.data.quality.valid_boundary_directions == 71
    assert all(point.bearing_deg != 0 for point in result.data.boundary_points)
    assert "PARTIAL_ROUTING_RESULTS" in [warning.code for warning in result.warnings]


def test_large_contiguous_boundary_gap_fails_in_detailed_mode() -> None:
    router = RadialRouter(missing_bearings={"000p00", "005p00", "010p00"})

    result = IsochroneService.detailed(router).generate(_request()).root

    assert result.ok is False
    assert result.error.code.value == "INSUFFICIENT_ROUTING_DATA"
    assert "LARGE_BOUNDARY_GAP" in [warning.code for warning in result.warnings]
    assert "有效边界方向" in result.error.message
    assert "最大方向缺口" in result.error.message


def test_transient_gap_remeasures_only_missing_samples_before_failing() -> None:
    missing_ids = {
        "sample:b000p00:r0900p00",
        "sample:b005p00:r0900p00",
        "sample:b010p00:r0900p00",
    }
    router = RadialRouter(missing_target_ids=missing_ids)
    calls = []

    def transient(request):
        calls.append({target.target_id for target in request.targets})
        result = router(request)
        if len(calls) == 1:
            result.root.warnings.append(WarningItem(code="ROUTING_RATE_LIMITED", message="临时限流。"))
            router.missing_target_ids = set()
        return result

    result = IsochroneService.detailed(transient).generate(_request()).root

    assert result.ok is True
    assert result.data.quality.valid_boundary_directions == 72
    assert calls[-1] == missing_ids
    assert "BOUNDARY_GAP_RETRY" in [warning.code for warning in result.warnings]


def test_unexpected_route_target_ids_are_treated_as_unknown() -> None:
    result = IsochroneService.detailed(
        RadialRouter(wrong_target_ids=True)
    ).generate(_request()).root

    assert result.ok is False
    assert result.error.code.value == "ROUTING_FAILED"
    assert "PARTIAL_ROUTING_RESULTS" in [warning.code for warning in result.warnings]


def test_route_for_a_different_location_is_treated_as_unknown() -> None:
    router = RadialRouter(
        mislocated_target_ids={"sample:b000p00:r0900p00"},
    )

    result = IsochroneService.detailed(router).generate(_request()).root

    assert result.ok is True
    assert result.data.quality.valid_boundary_directions == 71
    assert all(point.bearing_deg != 0 for point in result.data.boundary_points)


def test_budget_skips_a_refinement_round_as_a_whole() -> None:
    router = RadialRouter()
    service = IsochroneService(
        router,
        direction_count=4,
        max_routing_samples=26,
        include_approximation_warning=True,
    )

    result = service.generate(_request()).root

    assert result.ok is True
    assert result.data.quality.routing_sample_count == 24
    assert sum(router.calls) == 24
    assert "ROUTING_SAMPLE_BUDGET_REACHED" in [
        warning.code for warning in result.warnings
    ]


def test_rate_limited_routing_failure_surfaces_safe_reason() -> None:
    def rate_limited(_request):
        return RoutingResult.model_validate({
            "ok": False,
            "error": {
                "code": "RATE_LIMITED",
                "message": "secret provider detail",
                "retryable": True,
            },
            "warnings": [],
            "meta": {"schema_version": "1.0", "provider": "mock"},
        })

    result = IsochroneService.detailed(rate_limited).generate(_request()).root

    assert result.ok is False
    assert result.error.code.value == "ROUTING_FAILED"
    assert "配额受限" in result.error.message
    assert "测时成功 0/" in result.error.message
    assert "secret provider detail" not in result.error.message


def test_partial_routing_timeout_surfaces_safe_reason_when_geometry_fails() -> None:
    router = RadialRouter(wrong_target_ids=True)

    def partly_timed_out(request):
        result = router(request)
        result.root.warnings.append(WarningItem(code="ROUTING_TIMEOUT", message="safe"))
        return result

    result = IsochroneService.detailed(partly_timed_out).generate(_request()).root

    assert result.ok is False
    assert "超时" in result.error.message


def test_daily_quota_exhaustion_has_distinct_safe_reason() -> None:
    calls = []

    def daily_quota(_request):
        calls.append(1)
        return RoutingResult.model_validate({
            "ok": False,
            "error": {"code": "RATE_LIMITED", "message": "secret provider detail", "retryable": False},
            "warnings": [],
            "meta": {"schema_version": "1.0", "provider": "mock"},
        })

    result = IsochroneService.detailed(daily_quota).generate(_request()).root
    assert result.ok is False
    assert len(calls) == 1
    assert "当日配额不足" in result.error.message
    assert result.error.retryable is False
    assert "ROUTING_DAILY_QUOTA_EXHAUSTED" in [warning.code for warning in result.warnings]
    assert "secret provider detail" not in result.error.message
