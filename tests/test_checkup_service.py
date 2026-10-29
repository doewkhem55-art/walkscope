import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import CheckupError, run_checkup
from app.providers.baidu.poi import POIProviderError, ProviderPOI
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.checkup import CheckupRequest
from app.schemas.common import CenterPoint, Meta
from app.schemas.isochrone import IsochroneResult
from app.schemas.location import LocationResult
from app.schemas.routing import (
    RouteResult,
    RouteStatus,
    RoutingData,
    RoutingResult,
    RoutingSuccess,
    RoutingSummary,
)


ROOT = Path(__file__).resolve().parents[1]
LOCATION_FIXTURE = json.loads((ROOT / "contracts/v1/location-result.example.json").read_text())
ISOCHRONE_FIXTURE = json.loads((ROOT / "contracts/v1/isochrone-result.example.json").read_text())
CENTER = CenterPoint.model_validate(LOCATION_FIXTURE["data"]["center"])


def _small_isochrone():
    payload = deepcopy(ISOCHRONE_FIXTURE)
    center = payload["data"]["center"]
    lng, lat = center["lng"], center["lat"]
    points = [
        [lng, lat + 0.0004],
        [lng + 0.0004, lat],
        [lng, lat - 0.0004],
        [lng - 0.0004, lat],
    ]
    payload["data"]["geometry"]["coordinates"] = [points + [points[0]]]
    payload["data"]["boundary_points"] = [
        {
            "boundary_id": f"boundary:b{bearing:03d}",
            "bearing_deg": float(bearing),
            "location": {"lng": point[0], "lat": point[1], "crs": "BD09LL"},
            "radial_distance_m": 45.0,
        }
        for bearing, point in zip((0, 90, 180, 270), points)
    ]
    return IsochroneResult.model_validate(payload)


class FakeProvider:
    def __init__(self, responses, events):
        self.responses = responses
        self.events = events
        self.calls = []
        self.logged_stage = False

    def search(self, keyword, center, radius_m):
        if not self.logged_stage:
            self.events.append("poi")
            self.logged_stage = True
        self.calls.append((keyword, center, radius_m))
        result = self.responses.get(keyword, [])
        if isinstance(result, Exception):
            raise result
        return result


class FakeRoute:
    def __init__(self, events, grid_status=RouteStatus.NO_ROUTE, center_status=RouteStatus.SUCCESS):
        self.events = events
        self.grid_status = grid_status
        self.center_status = center_status
        self.requests = []
        self.logged_stage = False

    def __call__(self, request):
        if not self.logged_stage:
            self.events.append("routing")
            self.logged_stage = True
        self.requests.append(request)
        coverage = all(target.target_id.startswith(("service-cell:", "adaptive:")) for target in request.targets)
        status = self.grid_status if coverage else self.center_status
        routes = [
            RouteResult(
                target_id=target.target_id,
                location=target.location,
                status=status,
                distance_m=500 if status is RouteStatus.SUCCESS else None,
                duration_s=600 if status is RouteStatus.SUCCESS else None,
            )
            for target in request.targets
        ]
        counts = {item: sum(route.status is item for route in routes) for item in RouteStatus}
        return RoutingResult(root=RoutingSuccess(
            ok=True,
            data=RoutingData(
                origin=request.origin,
                travel_mode=request.travel_mode,
                routes=routes,
                summary=RoutingSummary(
                    requested=len(routes),
                    success=counts[RouteStatus.SUCCESS],
                    no_route=counts[RouteStatus.NO_ROUTE],
                    unavailable=counts[RouteStatus.UNAVAILABLE],
                ),
            ),
            warnings=[],
            meta=Meta(schema_version="1.0", provider="fake"),
        ))


def _run(responses, *, report_id="offline-test", max_grid_routes=1,
         center_status=RouteStatus.SUCCESS, facility_ids=None, on_update=None, progress=None):
    events = []
    location = LocationResult.model_validate(LOCATION_FIXTURE)
    isochrone = _small_isochrone()
    provider = FakeProvider(responses, events)
    route = FakeRoute(events, center_status=center_status)

    def resolve_location(request):
        events.append("location")
        return location

    def generate_isochrone(request):
        events.append("isochrone")
        assert request.center == CENTER
        assert request.time_limit_s == 900
        return isochrone

    result = run_checkup(
        CheckupRequest(
            location={"type": "address", "query": "同济大学", "city": "上海"},
            facility_ids=facility_ids,
        ),
        report_id=report_id,
        resolve_location=resolve_location,
        generate_isochrone=generate_isochrone,
        poi_provider=provider,
        route=route,
        progress=progress or (lambda _stage, _value: None),
        on_update=on_update,
        max_center_routes=1500,
        max_grid_routes=max_grid_routes,
    )
    return result, provider, route, events, isochrone


def _scores_in_steps_of_2_5(report):
    scores = [
        report.score,
        *(group.score for group in report.groups),
        *(category.score for category in report.categories),
    ]
    return all(score is None or abs(score / 2.5 - round(score / 2.5)) < 1e-8 for score in scores)


def test_fixed_flow_reports_full_catalog_and_preserves_evidence_under_budget():
    catalog = load_catalog()
    facilities = {item["id"]: item for item in catalog["facilities"]}
    responses = {
        facilities["childcare_0_3"]["search_terms"][0]: [
            ProviderPOI("child", "托育点", CENTER.lng, CENTER.lat, "同济路1号")
        ],
        facilities["childcare_0_3"]["search_terms"][1]: POIProviderError("TIMEOUT", "secret", True),
        facilities["market"]["search_terms"][0]: [
            ProviderPOI("market-1", "菜市场甲", CENTER.lng, CENTER.lat, "同济路2号"),
            ProviderPOI("market-2", "菜市场乙", CENTER.lng, CENTER.lat, "同济路3号"),
        ],
    }
    for keyword in facilities["supermarket"]["search_terms"]:
        responses[keyword] = POIProviderError("RATE_LIMITED", "secret provider detail", True)

    report, provider, _, events, _ = _run(responses)

    assert events == ["location", "isochrone", "poi", "routing"]
    assert len(report.categories) == 45
    assert len(report.groups) == 7
    assert report.catalog_version == catalog["version"]
    categories = {item.facility_id: item for item in report.categories}
    assert categories["primary_school"].search_status == "complete"
    assert categories["primary_school"].count == 0
    assert categories["supermarket"].search_status == "failed"
    assert categories["supermarket"].count is None

    childcare = categories["childcare_0_3"]
    assert childcare.search_status == "partial"
    assert childcare.count is None
    assert childcare.known_count == 1
    assert childcare.reachable_count == 1
    assert childcare.access_status == "reachable"
    # v3：配置分按已知数量分档（B 档：1 处 = 60 分），路线可达只作为独立证据。
    assert childcare.score == 60
    assert childcare.score_provisional is True
    assert childcare.score_profile == "B"
    assert childcare.score_basis_count == 1
    assert childcare.score_band == "1 处 → 60 分"
    assert childcare.next_band == "2 处 → 75 分"

    market = categories["market"]
    assert market.search_status == "complete"
    assert market.count == market.known_count == 2
    assert market.covered_ratio == 0
    assert market.blind_ratio == 0
    assert market.unknown_ratio == 1
    assert report.quality["coverage_budget_exhausted"] is True
    assert report.quality["coverage_route_pairs"] == 1
    assert not [
        item for item in report.blindspot["blind_spots"]["features"]
        if item["properties"]["facility_type"] == "market"
    ]

    unsupported = [
        item["id"] for item in facilities.values()
        if item["coverage_method"] == "insufficient_evidence"
    ]
    assert len(unsupported) == 7
    for facility_id in unsupported:
        category = categories[facility_id]
        assert category.covered_ratio == 0
        assert category.blind_ratio == 0
        assert category.unknown_ratio == 1
        # v3：完整检索未发现计 0 分，但“未检索到”不代表确认不存在。
        assert category.score == 0, f"{facility_id} 在 v3 口径下按完整零结果计 0 分"
        assert category.access_status in {"not_found", "unverified"}

    max_service_distance = max(
        [1000] + [item.get("service_distance_m") or 0 for item in facilities.values()]
    )
    expected_radius = minimum_poi_search_radius_m(
        CENTER, report.isochrone.geometry, max_service_distance
    )
    assert report.search_radius_m == expected_radius
    assert all(call[1] == CENTER and call[2] == expected_radius for call in provider.calls)
    assert _scores_in_steps_of_2_5(report)
    # v3：可计分类别都有配置分；supermarket 完全失败且无已知点位 → 暂不计分，
    # 因此七组综合分在本次运行中不生成（不伪装成完整报告）。
    assert all(category.score is not None for category in report.categories
               if category.facility_id != "supermarket")
    assert categories["supermarket"].score is None
    assert all(group.score is not None for group in report.groups)
    assert report.score is None
    assert any(warning.code == "CATEGORY_SCORE_UNAVAILABLE" for warning in report.warnings)
    assert any(warning.code == "NOT_FOUND_SCORED_ZERO" for warning in report.warnings)


def test_unavailable_categories_are_listed_and_do_not_fake_a_total():
    """v3: a failed search with no known point is unavailable, not a fake zero."""
    catalog = load_catalog()
    daily = [
        item for item in catalog["facilities"]
        if item["group_id"] == "daily_commerce"
    ]
    responses = {
        keyword: POIProviderError("NETWORK_ERROR", "secret", True)
        for item in daily[:3]
        for keyword in item["search_terms"]
    }

    report, _, _, _, _ = _run(responses, report_id="incomplete-test")

    groups = {item.id: item for item in report.groups}
    categories = {item.facility_id: item for item in report.categories}
    failed = {item["id"] for item in daily[:3]}
    assert groups["daily_commerce"].total_categories == 10
    assert groups["daily_commerce"].assessed_categories == 7
    assert groups["daily_commerce"].score_provisional is True
    assert groups["daily_commerce"].score is not None
    # 45 类没有全部可计分 → 不生成完整七组综合分。
    assert report.score is None
    for facility_id in failed:
        assert categories[facility_id].access_status == "unverified"
        assert categories[facility_id].score is None
        assert categories[facility_id].score_provisional is False
        assert categories[facility_id].count is None  # 事实字段不变：检索不完整仍为 null
    assert failed <= set(report.quality["unavailable_categories"])
    assert failed <= set(report.quality["unverified_categories"])
    assert any(warning.code == "CATEGORY_SCORE_UNAVAILABLE" for warning in report.warnings)



def test_group_marks_known_partial_scores_as_provisional_without_changing_value():
    poi = ProviderPOI("child", "托育点", CENTER.lng, CENTER.lat, "同济路1号")
    selected = ["childcare_0_3", "kindergarten"]
    complete, *_ = _run({"托育机构": [poi]}, facility_ids=selected, max_grid_routes=0)
    partial, *_ = _run({
        "托育机构": [poi],
        "托育中心": POIProviderError("TIMEOUT", "private provider detail", True),
    }, facility_ids=selected, max_grid_routes=0)

    assert partial.categories[0].score_provisional is True
    assert partial.groups[0].assessed_categories == partial.groups[0].total_categories == 2
    assert partial.groups[0].score == complete.groups[0].score == 30
    assert partial.score == complete.score == 30
    assert complete.groups[0].score_provisional is False
    assert partial.groups[0].score_provisional is True
    assert partial.score_provisional is True


def test_same_poi_can_belong_to_two_categories_but_has_one_center_route():
    shared = ProviderPOI("shared", "共用设施", CENTER.lng, CENTER.lat, "同济路1号")
    report, _, _, _, _ = _run({"托育机构": [shared], "幼儿园": [shared]}, max_grid_routes=0)

    members = [poi for poi in report.pois if poi.poi_id == "baidu:shared"]
    assert {poi.facility_id for poi in members} == {"childcare_0_3", "kindergarten"}
    assert len(members) == 2
    assert [route.target_id for route in report.routes].count("baidu:shared") == 1


def test_same_poi_id_with_conflicting_coordinates_stops_the_report():
    first = ProviderPOI("shared", "共用设施", CENTER.lng, CENTER.lat, "同济路1号")
    shifted = ProviderPOI("shared", "共用设施", CENTER.lng + 0.0001, CENTER.lat, "同济路1号")

    with pytest.raises(CheckupError) as error:
        _run({"托育机构": [first], "幼儿园": [shifted]}, max_grid_routes=0)

    assert error.value.code == "POI_CONFLICT"


@pytest.mark.parametrize("center_status,access_status", [
    (RouteStatus.NO_ROUTE, "unreachable"),
    (RouteStatus.UNAVAILABLE, "unverified"),
])
def test_failed_center_route_never_invents_distance_or_duration(center_status, access_status):
    poi = ProviderPOI("child", "托育点", CENTER.lng, CENTER.lat, "同济路1号")
    report, _, _, _, _ = _run({"托育机构": [poi]}, max_grid_routes=0,
                               center_status=center_status)

    childcare = next(item for item in report.categories if item.facility_id == "childcare_0_3")
    assert childcare.access_status == access_status
    assert childcare.nearest_distance_m is None
    assert childcare.nearest_duration_s is None
    # v3：路线不可达/未测只影响可达性证据，配置分仍按已发现的 1 处计 60 分，
    # 且绝不编造距离与时长。
    assert childcare.score == 60
    assert childcare.score_provisional is False


def test_failed_isochrone_preserves_safe_reason_and_stops_before_poi():
    events = []
    failure = IsochroneResult.model_validate({
        "ok": False,
        "error": {
            "code": "ROUTING_FAILED",
            "message": "步行算路请求受配额限制，无法生成可靠等时圈。",
            "retryable": True,
        },
        "warnings": [{"code": "LARGE_BOUNDARY_GAP", "message": "方向缺口过大。"}],
        "meta": {"schema_version": "1.0", "provider": "baidu"},
    })

    with pytest.raises(CheckupError) as caught:
        run_checkup(
            CheckupRequest(location={"type": "address", "query": "同济大学", "city": "上海"}),
            report_id="failed-isochrone",
            resolve_location=lambda _request: LocationResult.model_validate(LOCATION_FIXTURE),
            generate_isochrone=lambda _request: failure,
            poi_provider=FakeProvider({}, events),
            route=FakeRoute(events),
        )

    assert caught.value.code == "ISOCHRONE_FAILED"
    assert "配额限制" in caught.value.message
    assert "ROUTING_FAILED" in caught.value.message
    assert "LARGE_BOUNDARY_GAP" in caught.value.message
    assert "不会替换为圆形范围" in caught.value.message
    assert caught.value.retryable is True
    assert events == []


def test_selected_categories_limit_search_and_denominator():
    catalog = load_catalog()
    facilities = {item["id"]: item for item in catalog["facilities"]}
    selected = ["primary_school", "secondary_school"]
    report, provider, _, _, _ = _run({}, facility_ids=selected)

    assert {category.facility_id for category in report.categories} == set(selected)
    assert [group.id for group in report.groups] == ["education_children"]
    assert report.selected_facility_ids == selected
    assert report.quality["total_categories"] == 2
    assert report.quality["selected_categories"] == 2
    assert report.score == 0  # both complete searches found no facility
    searched = {call[0] for call in provider.calls}
    expected = {
        term
        for facility_id in selected
        for term in facilities[facility_id]["search_terms"]
    }
    assert searched == expected
    other_terms = {
        term
        for facility_id, facility in facilities.items()
        if facility_id not in selected
        for term in facility["search_terms"]
    }
    assert not searched & other_terms



def test_explicit_full_catalog_matches_default_without_subset_warning():
    selected = [item["id"] for item in load_catalog()["facilities"]]
    default, default_provider, *_ = _run({}, max_grid_routes=0)
    explicit, explicit_provider, *_ = _run({}, facility_ids=selected, max_grid_routes=0)

    assert len(selected) == 45
    assert explicit.selected_facility_ids == default.selected_facility_ids == selected
    assert explicit.categories == default.categories
    assert explicit.groups == default.groups
    assert explicit.score == default.score
    assert explicit.summary == default.summary
    assert explicit.summary.startswith("目录共 45 类设施")
    assert not any(w.code == "SELECTED_SCOPE" for w in explicit.warnings)
    assert explicit_provider.calls == default_provider.calls


def test_unknown_selected_facility_is_rejected():
    with pytest.raises(CheckupError) as error:
        _run({}, facility_ids=["not_a_facility"])
    assert error.value.code == "INVALID_FACILITY_SELECTION"
    assert "not_a_facility" in error.value.message
