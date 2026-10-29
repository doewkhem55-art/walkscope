"""Offline oracles for POI-origin, distance-based service-area set operations."""
from importlib import import_module

import pytest
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union

from app.errors import ProviderStopError
from app.schemas.common import CenterPoint
from app.schemas.routing import RoutingResult


def to_center(x, y):
    return CenterPoint(lng=120.0 + x / 100000, lat=30.0 + y / 100000, crs="BD09LL")


def to_m(lng, lat):
    return ((lng - 120.0) * 100000, (lat - 30.0) * 100000)


def poi(name, x=0, y=10):
    return {"poi_id": name, "location": to_center(x, y).model_dump()}


def category(fid, pois=(), status="complete"):
    return {"facility_id": fid, "search_status": status, "pois": list(pois)}


def spec(fid, radius=1000, method="grid_walking_distance", group="education"):
    return {"id": fid, "group_id": group, "coverage_method": method, "service_distance_m": radius}


class Oracle:
    def __init__(self, answer=lambda origin, target: ("success", 1000, 9999)):
        self.answer = answer
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        rows = []
        for target in request.targets:
            status, distance, duration = self.answer(request.origin, target)
            rows.append(dict(target_id=target.target_id, location=target.location.model_dump(),
                             status=status, distance_m=distance, duration_s=duration))
        return RoutingResult.model_validate({"ok": True, "data": {
            "origin": request.origin.model_dump(), "travel_mode": "walking", "routes": rows,
            "summary": {"requested": len(rows), **{s: sum(r["status"] == s for r in rows)
                                                   for s in ("success", "no_route", "unavailable")}}},
            "warnings": [], "meta": {"schema_version": "1.0", "provider": "offline-oracle"}})


def analyze(categories, specs, route=None, *, domain=None, cells=None, budget=5000):
    try:
        engine = import_module("app.modules.checkup.poi_service_areas")
    except ImportError:
        pytest.fail("POI-origin service-area engine is missing")
    domain = box(0, 0, 60, 20) if domain is None else domain
    cells = [(str(i), box(i * 20, 0, (i + 1) * 20, 20).intersection(domain))
             for i in range(3)] if cells is None else cells
    warnings = []
    result = engine.analyze_poi_service_areas(domain, cells, to_m, to_center, categories,
                                             {"facilities": specs}, route or Oracle(), warnings,
                                             max_route_pairs=budget)
    return (*result, warnings)


def local_region(collection, fid=None):
    from shapely.ops import transform
    return unary_union([transform(to_m, shape(f["geometry"])) for f in collection["features"]
                        if fid is None or f["properties"]["facility_type"] == fid])


def test_routes_start_at_poi_and_education_uses_1000_metres_not_duration():
    source = poi("school", -50)
    def answer(origin, target):
        if origin != CenterPoint.model_validate(source["location"]):
            return ("no_route", None, None)  # Deliberately asymmetric walking network.
        index = int(target.target_id.split(":")[-1])
        return ("success", [1000, 1001, 20][index], [99999, 1, 99999][index])
    route = Oracle(answer)
    ratios, layers, quality, _ = analyze([category("school", [source])], [spec("school")], route)
    assert ratios["school"] == pytest.approx(dict(covered_ratio=2 / 3, blind_ratio=1 / 3, unknown_ratio=0))
    assert local_region(layers["covered_areas"]).symmetric_difference(
        box(0, 0, 20, 20).union(box(40, 0, 60, 20))).area < 1e-5
    assert quality["coverage_route_direction"] == "poi_to_grid"
    assert quality["coverage_route_pairs"] == 3
    assert all(t.target_id.startswith("service-cell:") for r in route.requests for t in r.targets)


def test_overlapping_service_areas_are_unioned_before_domain_difference():
    def answer(origin, target):
        i = int(target.target_id.split(":")[-1])
        x, _ = to_m(origin.lng, origin.lat)
        return ("success", 10 if i in ([0, 1] if x < 1 else [1, 2]) else 1001, 50)
    ratios, layers, _, _ = analyze([category("a", [poi("one"), poi("two", 1)])], [spec("a")], Oracle(answer))
    assert ratios["a"]["covered_ratio"] == pytest.approx(1)
    assert layers["blind_spots"]["features"] == []
    assert len(layers["poi_service_areas"]["features"]) == 2
    assert local_region(layers["covered_areas"]).area == pytest.approx(1200, abs=1e-5)


def test_group_and_total_union_coverage_and_known_coverage_wins_unknown():
    def answer(origin, target):
        i = int(target.target_id.split(":")[-1])
        x, _ = to_m(origin.lng, origin.lat)
        return ("success", 10 if i == (0 if x < 1 else 1) else 1001, 50)
    ratios, layers, _, _ = analyze([category("a", [poi("one")]), category("b", [poi("two", 1)]),
                                   category("c", status="partial")],
                                  [spec("a"), spec("b"), spec("c", group="other")], Oracle(answer))
    group = layers["group_coverage"]["education"]
    assert group["covered_ratio"] == pytest.approx(2 / 3)
    assert group["blind_ratio"] == pytest.approx(1 / 3)
    assert group["unknown_ratio"] == 0
    total = layers["total_coverage"]
    assert total["covered_ratio"] == pytest.approx(2 / 3)
    assert total["unknown_ratio"] == pytest.approx(1 / 3)
    assert total["blind_ratio"] == 0
    assert total["definition"] == "all_categories_confirmed_blind"
    assert total["calculation_method"] == "poi_origin_service_area_union_difference_v1"
    assert local_region(total["covered_areas"]).area == pytest.approx(800, abs=1e-5)
    assert ratios["c"]["unknown_ratio"] == 1


def test_shared_poi_reuses_distances_across_thresholds_and_never_merges_conflicting_locations():
    route = Oracle(lambda *_: ("success", 800, 1))
    shared = poi("same")
    ratios, _, quality, _ = analyze([category("a", [shared, shared]), category("b", [shared])],
                                   [spec("a", 500), spec("b", 1000)], route)
    assert quality["coverage_route_pairs"] == 3
    assert ratios["a"]["blind_ratio"] == 1
    assert ratios["b"]["covered_ratio"] == 1
    _, layers, quality, _ = analyze([category("a", [poi("same"), poi("same", 1)])], [spec("a")])
    assert quality["coverage_route_pairs"] == 6
    assert len(layers["poi_service_areas"]["features"]) == 2


def test_clipping_holes_and_outside_pois_preserve_area_weights():
    domain = Polygon([(0, 0), (50, 0), (50, 20), (0, 20)],
                     holes=[[(5, 5), (5, 15), (15, 15), (15, 5)]])
    route = Oracle(lambda origin, target: ("success", 10 if target.target_id.endswith(":2") else 1001, 1))
    ratios, layers, _, _ = analyze([category("a", [poi("outside", -10)])], [spec("a")], route, domain=domain)
    assert ratios["a"]["covered_ratio"] == pytest.approx(200 / 900)
    blind = local_region(layers["blind_spots"])
    assert blind.intersection(box(5, 5, 15, 15)).area < 1e-6
    assert blind.area == pytest.approx(700, abs=1e-5)


@pytest.mark.parametrize("method,radius", [("insufficient_evidence", 1000), ("grid_walking_distance", None)])
def test_unsupported_coverage_stays_wholly_unknown(method, radius):
    route = Oracle()
    ratios, _, quality, _ = analyze([category("a", [poi("one")])], [spec("a", radius, method)], route)
    assert ratios["a"]["unknown_ratio"] == 1
    assert quality["coverage_route_pairs"] == 0
    assert route.requests == []


def test_euclidean_window_is_only_a_prefilter_and_never_a_synthetic_circle():
    route = Oracle(lambda *_: ("no_route", None, None))
    ratios, layers, quality, _ = analyze([category("a", [poi("one")])], [spec("a", 25)], route)
    assert quality["coverage_route_pairs"] == 1
    assert ratios["a"]["blind_ratio"] == 1
    assert layers["covered_areas"]["features"] == []
    assert layers["poi_service_areas"]["features"] == []


def test_unavailable_and_unmeasured_potential_remain_unknown_but_known_coverage_wins():
    route = Oracle(lambda origin, target: ("success", 10, 1) if origin == to_center(0, 10)
                   else ("unavailable", None, None))
    ratios, _, quality, _ = analyze([category("a", [poi("one"), poi("two", 1)])], [spec("a")], route)
    assert ratios["a"]["covered_ratio"] == 1
    assert quality["coverage_route_unavailable"] == 3
    ratios, _, quality, warnings = analyze([category("a", [poi("one")])], [spec("a")],
                                           Oracle(lambda *_: ("no_route", None, None)), budget=1)
    assert ratios["a"] == pytest.approx(dict(covered_ratio=0, blind_ratio=1 / 3, unknown_ratio=2 / 3))
    assert quality["coverage_budget_exhausted"] is True
    assert quality["coverage_route_pairs"] == 1
    assert "ROUTING_BUDGET_EXCEEDED" in {w.code for w in warnings}


def test_budget_is_shared_fairly_between_categories_and_origins():
    route = Oracle(lambda *_: ("no_route", None, None))
    ratios, _, quality, _ = analyze([category("a", [poi("a1"), poi("a2", 2)]),
                                   category("b", [poi("b1", 1)])], [spec("a"), spec("b")], route, budget=2)
    origins = [round(to_m(r.origin.lng, r.origin.lat)[0]) for r in route.requests]
    assert origins == [0, 1]
    assert ratios["b"]["blind_ratio"] == pytest.approx(1 / 3)
    assert quality["coverage_route_pairs"] == 2


def test_provider_stop_is_caught_once_and_no_route_follows_it():
    calls = []
    def stop(request):
        calls.append(request)
        raise ProviderStopError("QUOTA_STOP", "offline stop")
    ratios, _, quality, warnings = analyze([category("a", [poi("one"), poi("two", 1)])], [spec("a")], stop)
    assert len(calls) == 1
    assert quality["coverage_stop_reason"] == "QUOTA_STOP"
    assert quality["coverage_route_pairs"] == 0
    assert ratios["a"]["unknown_ratio"] == 1
    assert "QUOTA_STOP" in {w.code for w in warnings}


def test_incomplete_search_is_unknown_outside_known_service_area_and_empty_complete_is_blind():
    ratios, _, _, _ = analyze([category("a", [poi("one")], "partial"), category("b")],
                              [spec("a", 25), spec("b")], Oracle(lambda *_: ("success", 20, 1)))
    assert ratios["a"] == pytest.approx(dict(covered_ratio=1 / 3, blind_ratio=0, unknown_ratio=2 / 3))
    assert ratios["b"]["blind_ratio"] == 1


def test_empty_complete_search_still_reports_intermediate_progress_without_routes():
    engine = import_module("app.modules.checkup.poi_service_areas")
    updates = []
    route = Oracle()
    ratios, _, quality = engine.analyze_poi_service_areas(
        box(0, 0, 20, 20), [("one", box(0, 0, 20, 20))], to_m, to_center,
        [category("a")], {"facilities": [spec("a")]}, route, [], progress=updates.append)
    assert ratios["a"]["blind_ratio"] == 1
    assert quality["coverage_route_pairs"] == 0
    assert len(set(updates)) > 3  # Real preparation, dissolution/difference, and finish.
    assert any(0 < value < 1 for value in updates)
    assert updates == sorted(updates)
    assert updates[-1] == 1
