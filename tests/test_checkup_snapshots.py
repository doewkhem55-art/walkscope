"""Offline staged identity and real checkup-runner integration checks."""

import hashlib
import json
from copy import deepcopy

import pytest

from app.api import checkups
from app.modules.checkup.catalog import _CATALOG_PATH, load_catalog
from app.modules.checkup.service import CheckupError, run_checkup
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.checkup import CheckupRequest
from app.schemas.isochrone import GeoJSONPolygon
from test_checkup_service import CENTER, LOCATION_FIXTURE, FakeRoute, _small_isochrone, _run
from test_checkup_api import client


INPUT = {"type": "address", "query": "同济大学", "city": "上海"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def save(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def poi(fid="childcare_0_3", pid="one", **changes):
    return {"poi_id": pid, "name": "测试设施", "facility_id": fid,
            "location": CENTER.model_dump(), "address": None,
            "in_isochrone": False, **changes}


def category(fid="childcare_0_3", status="complete", pois=None):
    return {"facility_id": fid, "name_zh": "不可信名称", "group_id": "不可信组",
            "search_status": status, "count": 999, "known_count": 999,
            "pois": [poi(fid)] if pois is None else pois, "warnings": []}


@pytest.fixture
def staged(tmp_path):
    iso = _small_isochrone()
    catalog = load_catalog()
    radius = minimum_poi_search_radius_m(CENTER, iso.root.data.geometry,
        max([1000] + [f.get("service_distance_m") or 0 for f in catalog["facilities"]]))
    gate1 = {"location": deepcopy(LOCATION_FIXTURE), "isochrone": iso.model_dump(mode="json")}
    gate2 = {"gate1_file": "gate1.json", "catalog_version": catalog["version"],
             "center": CENTER.model_dump(), "search_radius_m": radius,
             "categories": [category()]}
    manifest = {"source": "baidu-live-staged", "input": INPUT,
                "input_sha256": fingerprint(INPUT), "catalog_version": catalog["version"],
                "catalog_sha256": hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest(),
                "gate1": {"path": "gate1.json"}, "gate2": {"path": "gate2.json"}}

    def write():
        manifest["gate1"]["sha256"] = save(tmp_path / "gate1.json", gate1)
        manifest["gate2"]["sha256"] = save(tmp_path / "gate2.json", gate2)
        save(tmp_path / "manifest.json", manifest)
        return tmp_path / "manifest.json"

    return gate1, gate2, manifest, write


def forbidden(*args, **kwargs):
    pytest.fail("staged reuse must not rerun location, isochrone or POI")


def run_staged(path, location=None):
    route = FakeRoute([])
    report = run_checkup(CheckupRequest(location=location or INPUT), report_id="staged",
        stage_manifest=path, resolve_location=forbidden, generate_isochrone=forbidden,
        poi_provider=type("ForbiddenPOI", (), {"search": forbidden})(), route=route,
        max_grid_routes=1)
    return report, route


@pytest.mark.parametrize("manifest", [None, "", " \t\n"])
def test_blank_stage_configuration_allows_new_address_checkup(client, monkeypatch, tmp_path, manifest):
    from urllib.parse import urlsplit

    if manifest is None:
        monkeypatch.delenv("CHECKUP_STAGE_MANIFEST", raising=False)
    else:
        monkeypatch.setenv("CHECKUP_STAGE_MANIFEST", manifest)
    monkeypatch.setenv("BAIDU_MAP_AK", "offline-test-only")
    monkeypatch.setenv("CHECKUP_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(checkups, "generate_grid_isochrone", lambda *_args, **_kwargs: _small_isochrone())

    def transport(url, timeout):
        path = urlsplit(url).path
        if path == "/geocoding/v3/":
            return {"status": 0, "result": {"location": {"lng": CENTER.lng, "lat": CENTER.lat}}}
        assert path == "/place/v2/search"
        return {"status": 0, "total": 0, "results": []}

    monkeypatch.setattr(checkups, "_get_json", transport)
    submitted = client.post("/api/v1/checkups", json={
        "location": {"type": "address", "query": "武汉小学", "city": "武汉市"},
        "facility_ids": ["primary_school"],
    })
    assert submitted.status_code == 202
    checkups._executor.run_next()
    done = client.get("/api/v1/checkups/" + submitted.json()["id"]).json()
    assert done["status"] == "completed", done.get("error")
    assert done["result"]["location_label"] == "武汉小学"
    assert done["result"]["categories"][0]["search_status"] == "complete"
    assert done["result"]["quality"]["provider_requests"] >= 2
    assert "stage_reuse" not in done["result"]["quality"]


def test_matching_stages_skip_three_providers_and_continue_routes(staged):
    _, gate2, manifest, write = staged
    gate2["categories"] = [category(pois=[poi(), poi(), poi(pid="outside",
        location={**CENTER.model_dump(), "lng": CENTER.lng + 0.001}, in_isochrone=True)]),
        category("kindergarten", "partial"), category("market", "failed", pois=[])]
    report, route = run_staged(write())
    rows = {c.facility_id: c for c in report.categories}
    assert len(report.categories) == 45
    assert rows["childcare_0_3"].count == rows["childcare_0_3"].known_count == 1
    assert rows["childcare_0_3"].name_zh == "婴幼儿托育"
    assert rows["kindergarten"].count is None
    assert rows["kindergarten"].known_count == 1
    assert rows["primary_school"].search_status == "failed"
    assert rows["primary_school"].count is None
    assert rows["market"].count is None and rows["market"].search_status == "failed"
    assert len(report.pois) == 3
    assert not next(p for p in report.pois if p.poi_id == "outside").in_isochrone
    assert all(p.in_isochrone for p in report.pois if p.poi_id == "one")
    assert route.requests and route.requests[0].origin == CENTER
    assert report.quality["center_route_pairs"] == 1
    assert report.quality["coverage_route_pairs"] == 1
    provenance = report.quality["stage_reuse"]
    assert provenance["source"] == "baidu-live-staged"
    assert provenance["gate1"]["sha256"] == manifest["gate1"]["sha256"]
    assert provenance["gate2"]["sha256"] == manifest["gate2"]["sha256"]
    assert provenance["gate1"]["captured_at"] is None
    assert len(provenance["categories"]) == 45
    assert all(row["captured_at"] is None for row in provenance["categories"])
    assert any(w.code == "STAGED_DATA_REUSED" for w in report.warnings)


@pytest.mark.parametrize("damage", ["hash", "input_hash", "input", "source", "catalog_hash",
    "catalog_version", "gate2_catalog", "gate1_center", "gate2_center", "radius", "time",
    "gate1_link", "gate1_sha", "failed_envelope", "crs", "geometry", "outside_center",
    "category_id", "duplicate_category", "status", "poi_facility", "poi_position", "conflict",
    "within_category_conflict", "missing_file", "manifest_json", "blank_poi_id", "capture_time"])
def test_invalid_identity_or_evidence_fails_without_provider_fallback(staged, damage):
    gate1, gate2, manifest, write = staged
    location = None
    if damage == "input": location = {**INPUT, "city": "北京市"}
    elif damage == "source": manifest["source"] = "uploaded"
    elif damage == "input_hash": manifest["input_sha256"] = "0" * 64
    elif damage == "catalog_hash": manifest["catalog_sha256"] = "0" * 64
    elif damage == "catalog_version": manifest["catalog_version"] = "stale"
    elif damage == "gate2_catalog": gate2["catalog_version"] = "stale"
    elif damage == "gate1_center": gate1["location"]["data"]["center"] = {**CENTER.model_dump(), "lng": 121.0}
    elif damage == "gate2_center": gate2["center"] = {**CENTER.model_dump(), "lng": 121.0}
    elif damage == "radius": gate2["search_radius_m"] += 1
    elif damage == "time": gate1["isochrone"]["data"]["time_limit_s"] = 600
    elif damage == "gate1_link": gate2["gate1_file"] = "another.json"
    elif damage == "gate1_sha": gate2["gate1_sha256"] = "0" * 64
    elif damage == "failed_envelope": gate1["location"]["ok"] = False
    elif damage == "crs": gate1["location"]["data"]["center"] = {**CENTER.model_dump(), "crs": "WGS84"}
    elif damage in {"geometry", "outside_center"}:
        iso = gate1["isochrone"]["data"]
        points = iso["boundary_points"]
        if damage == "geometry": points[1]["location"], points[2]["location"] = points[2]["location"], points[1]["location"]
        else:
            for p in points: p["location"]["lng"] += 0.01
        ring = [[p["location"]["lng"], p["location"]["lat"]] for p in points]
        iso["geometry"]["coordinates"] = [ring + [ring[0]]]
    elif damage == "category_id": gate2["categories"][0]["facility_id"] = "unknown"
    elif damage == "duplicate_category": gate2["categories"].append(category())
    elif damage == "status": gate2["categories"][0]["search_status"] = "pending"
    elif damage == "poi_facility": gate2["categories"][0]["pois"][0]["facility_id"] = "market"
    elif damage == "poi_position": gate2["categories"][0]["pois"][0]["location"]["lng"] = 181.0
    elif damage == "blank_poi_id": gate2["categories"][0]["pois"][0]["poi_id"] = ""
    elif damage == "capture_time": gate2["categories"][0]["captured_at"] = "yesterday"
    elif damage == "within_category_conflict": gate2["categories"][0]["pois"].append(poi(location={**CENTER.model_dump(), "lng": CENTER.lng + 0.0001}))
    elif damage == "conflict": gate2["categories"].append(category("kindergarten", pois=[poi("kindergarten", location={**CENTER.model_dump(), "lng": CENTER.lng + 0.0001})]))
    path = write()
    if damage == "hash": (path.parent / "gate2.json").write_text("{}")
    elif damage == "missing_file": manifest["gate1"]["path"] = "absent.json"; save(path, manifest)
    elif damage == "manifest_json": path.write_text("{")
    with pytest.raises(CheckupError) as caught:
        run_staged(path, location)
    assert caught.value.code == "STAGE_SNAPSHOT_INVALID"
    assert caught.value.retryable is False


def test_coordinate_reuse_requires_exact_verified_bd09_center(staged):
    *_, write = staged
    path = write()
    report, _ = run_staged(path, {"type": "coordinate", **CENTER.model_dump()})
    assert report.center == CENTER
    with pytest.raises(CheckupError):
        run_staged(path, {"type": "coordinate", **CENTER.model_dump(), "lng": CENTER.lng + 1e-8})


def test_normalized_original_address_and_valid_gate1_sha_are_accepted(staged):
    _, gate2, manifest, write = staged
    write()
    gate2["gate1_sha256"] = manifest["gate1"]["sha256"]
    report, _ = run_staged(write(), {"type": "address", "query": " 同济大学 ", "city": " 上海 "})
    assert report.center == CENTER


def test_explicit_capture_time_is_preserved_from_snapshot(staged):
    gate1, gate2, _, write = staged
    gate1["captured_at"] = "2026-10-15T08:00:00+00:00"
    gate2["captured_at"] = "2026-10-15T09:00:00+00:00"
    gate2["categories"][0]["captured_at"] = "2026-10-15T08:30:00+00:00"
    report, _ = run_staged(write())
    provenance = report.quality["stage_reuse"]
    assert provenance["gate1"]["captured_at"] == gate1["captured_at"]
    assert provenance["gate2"]["captured_at"] == gate2["captured_at"]
    assert provenance["categories"][0]["captured_at"] == "2026-10-15T08:30:00+00:00"
    assert provenance["categories"][1]["captured_at"] is None


def test_nearest_distance_and_duration_are_from_fastest_same_route(monkeypatch):
    class CrossedRoute(FakeRoute):
        def __call__(self, request):
            result = super().__call__(request)
            for item in result.root.data.routes:
                item.duration_s, item.distance_m = (100, 800) if item.target_id.endswith("fast") else (200, 200)
            return result
    monkeypatch.setattr("test_checkup_service.FakeRoute", CrossedRoute)
    from app.providers.baidu.poi import ProviderPOI
    report, *_ = _run({"托育机构": [ProviderPOI("fast", "快", CENTER.lng, CENTER.lat, ""),
                                      ProviderPOI("short", "近", CENTER.lng, CENTER.lat, "")]}, max_grid_routes=0)
    row = report.categories[0]
    assert (row.nearest_duration_s, row.nearest_distance_m) == (100, 800)


def test_formal_post_uses_real_runner_and_recovers_after_bad_snapshot(client, monkeypatch, staged):
    _, _, _, write = staged
    path = write()
    monkeypatch.setenv("CHECKUP_STAGE_MANIFEST", str(path))
    monkeypatch.setenv("BAIDU_MAP_AK", "offline-test-only")
    monkeypatch.setattr(checkups, "resolve_location", forbidden)
    monkeypatch.setattr(checkups.IsochroneService, "detailed", lambda *_: type("NoIso", (), {"generate": forbidden})())
    monkeypatch.setattr(checkups.BaiduPOIProvider, "search", forbidden)
    calls = []

    def transport(url, _timeout):
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(url)
        assert parts.path == "/routematrix/v2/walking"
        calls.append(parts.path)
        size = len(parse_qs(parts.query)["destinations"][0].split("|"))
        return {"status": 0, "result": [{"distance": {"value": 30}, "duration": {"value": 20}} for _ in range(size)]}

    monkeypatch.setattr(checkups, "_get_json", transport)
    (path.parent / "gate2.json").write_text("{}")
    first = client.post("/api/v1/checkups", json={"location": INPUT})
    assert first.status_code == 202
    checkups._executor.run_next()
    failed = client.get("/api/v1/checkups/" + first.json()["id"]).json()
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "STAGE_SNAPSHOT_INVALID"
    assert calls == []
    write()
    second = client.post("/api/v1/checkups", json={"location": INPUT})
    assert second.status_code == 202
    checkups._executor.run_next()
    done = client.get("/api/v1/checkups/" + second.json()["id"]).json()
    assert done["status"] == "completed"
    assert done["result"]["quality"]["stage_reuse"]["source"] == "baidu-live-staged"
    assert done["result"]["quality"]["provider_requests"] > 0
    assert calls


def _one_coverage_cell_geometry():
    # These route-selection tests need one cell at the new 20 m resolution.
    low_lng, high_lng = CENTER.lng + 0.00001, CENTER.lng + 0.00005
    low_lat, high_lat = CENTER.lat + 0.00001, CENTER.lat + 0.00005
    return GeoJSONPolygon(type="Polygon", coordinates=[[
        [low_lng, low_lat], [high_lng, low_lat], [high_lng, high_lat],
        [low_lng, high_lat], [low_lng, low_lat],
    ]])


def _legacy_poi_coverage(center, geometry, categories, catalog, route, warnings, *, max_route_pairs=5000):
    # Retain regression coverage for the saved-report POI-origin engine. The
    # live entry now uses shared origins, tested in test_representative_service_areas.
    from app.modules.checkup.service import _projection
    from app.modules.isochrone.geometry import geometry_to_local
    from app.modules.blindspot.service import _grid_cells
    from app.modules.checkup.poi_service_areas import analyze_poi_service_areas
    to_m, to_center = _projection(center)
    domain = geometry_to_local(geometry, to_m)
    return analyze_poi_service_areas(domain, _grid_cells(domain, 20), to_m, to_center,
        categories, catalog, route, warnings, max_route_pairs=max_route_pairs)


def test_poi_service_areas_share_routes_across_categories():
    analyze_coverage = _legacy_poi_coverage
    from app.schemas.routing import RouteStatus
    nearby = poi(pid="near")
    farther = poi(pid="far", location={**CENTER.model_dump(), "lng": CENTER.lng + 0.0002})
    categories = [category(pois=[nearby, farther]), category("kindergarten", pois=[nearby, farther])]
    route = FakeRoute([], grid_status=RouteStatus.SUCCESS)
    ratios, _, quality = analyze_coverage(CENTER, _one_coverage_cell_geometry(),
        categories, load_catalog(), route, [], max_route_pairs=10)
    assert {(request.origin.lng, request.origin.lat) for request in route.requests} == {
        (item["location"]["lng"], item["location"]["lat"]) for item in (nearby, farther)}
    target_ids = [t.target_id for r in route.requests for t in r.targets]
    assert len(target_ids) == 2 and len(set(target_ids)) == 1
    assert all(target_id.startswith("service-cell:") for target_id in target_ids)
    assert ratios["childcare_0_3"]["covered_ratio"] == 1
    assert ratios["kindergarten"]["covered_ratio"] == 1
    assert quality["coverage_route_pairs"] == 2
    assert quality["coverage_budget_exhausted"] is False


@pytest.mark.parametrize("first,second,status,budget,state,pairs,exhausted", [
    ("unavailable", "success", "complete", 10, "covered", 2, False),
    ("missing", "success", "complete", 10, "covered", 2, False),
    ("outside", "no_route", "complete", 10, "blind", 2, False),
    ("threshold", "no_route", "complete", 10, "covered", 2, False),
    ("just_outside", "no_route", "complete", 10, "blind", 2, False),
    ("slow", "no_route", "complete", 10, "covered", 2, False),
    ("unavailable", "no_route", "complete", 10, "unknown", 2, False),
    ("no_route", "no_route", "partial", 10, "unknown", 2, False),
    ("no_route", "success", "complete", 1, "unknown", 1, True),
])
def test_coverage_continues_unresolved_candidates_and_only_complete_exclusion_is_blind(
        first, second, status, budget, state, pairs, exhausted):
    analyze_coverage = _legacy_poi_coverage
    from app.schemas.routing import RoutingResult
    requests = []

    def route(request):
        requests.append(request)
        results = []
        for target in request.targets:
            outcome = first if request.origin == CENTER else second
            result = {"target_id": target.target_id, "location": target.location.model_dump(),
                      "status": "success" if outcome in {"outside", "missing", "threshold", "just_outside", "slow"} else outcome}
            if outcome == "missing": result["target_id"] = "unrelated"
            if result["status"] == "success":
                distance = {"outside": 10000, "threshold": 1000, "just_outside": 1001}.get(outcome, 50)
                result.update(distance_m=distance, duration_s=1800 if outcome == "slow" else 60)
            results.append(result)
        return RoutingResult.model_validate({"ok": True, "data": {
            "origin": request.origin.model_dump(), "travel_mode": "walking", "routes": results,
            "summary": {"requested": len(results), **{s: sum(r["status"] == s for r in results)
                for s in ("success", "no_route", "unavailable")}}},
            "warnings": [], "meta": {"schema_version": "1.0", "provider": "offline"}})

    categories = [category(status=status, pois=[poi(pid="near"),
        poi(pid="far", location={**CENTER.model_dump(), "lng": CENTER.lng + 0.0002})])]
    ratios, _, quality = analyze_coverage(CENTER, _one_coverage_cell_geometry(),
        categories, load_catalog(), route, [], max_route_pairs=budget)
    assert ratios["childcare_0_3"][state + "_ratio"] == 1
    assert [request.origin.model_dump() for request in requests] == [
        item["location"] for item in categories[0]["pois"][:pairs]]
    assert all(target.target_id.startswith("service-cell:")
               for request in requests for target in request.targets)
    assert all(len(r.targets) == 1 for r in requests)
    assert quality["coverage_route_pairs"] == pairs
    assert quality["coverage_budget_exhausted"] is exhausted
