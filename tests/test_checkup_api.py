import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import checkups
from app.api.main import app
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import CheckupError
from app.schemas.checkup import (
    CategoryAssessment,
    CheckupReport,
    GroupAssessment,
)
from app.schemas.common import CenterPoint
from app.schemas.isochrone import BoundaryPoint, IsochroneData
from app.schemas.routing import RoutingRequest


_CHECKUP_REQUEST = {
    "location": {
        "type": "coordinate",
        "lng": 121.5,
        "lat": 31.2,
        "crs": "BD09LL",
    }
}


class HoldingExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args, **kwargs):
        self.pending.append((function, args, kwargs))

    def run_next(self):
        function, args, kwargs = self.pending.pop(0)
        function(*args, **kwargs)


@pytest.fixture
def client(monkeypatch):
    saved_overrides = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    monkeypatch.setattr(checkups, "_jobs", {})
    monkeypatch.setattr(checkups, "_questions_running", set())
    monkeypatch.setattr(checkups, "_lock", threading.Lock())
    executor = HoldingExecutor()
    monkeypatch.setattr(checkups, "_executor", executor)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


def _report(report_id: str) -> CheckupReport:
    catalog = load_catalog()
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    boundary = [
        BoundaryPoint(
            boundary_id=f"boundary:{bearing}",
            bearing_deg=bearing,
            location=location,
            radial_distance_m=1000,
        )
        for bearing, location in (
            (0, CenterPoint(lng=121.5, lat=31.209, crs="BD09LL")),
            (90, CenterPoint(lng=121.51, lat=31.2, crs="BD09LL")),
            (180, CenterPoint(lng=121.5, lat=31.191, crs="BD09LL")),
            (270, CenterPoint(lng=121.49, lat=31.2, crs="BD09LL")),
        )
    ]
    ring = [[point.location.lng, point.location.lat] for point in boundary]
    ring.append(ring[0])
    isochrone = IsochroneData(
        center=center,
        time_limit_s=900,
        geometry={"type": "Polygon", "coordinates": [ring]},
        boundary_points=boundary,
        quality={
            "requested_directions": 4,
            "valid_boundary_directions": 4,
            "routing_sample_count": 24,
            "routing_success_count": 24,
            "sample_success_rate": 1,
            "confidence": "high",
        },
    )
    categories = [
        CategoryAssessment(
            facility_id=item["id"],
            name_zh=item["name_zh"],
            group_id=item["group_id"],
            search_status="failed",
            count=None,
            known_count=0,
            reachable_count=0,
            unresolved_count=0,
            nearest_duration_s=None,
            nearest_distance_m=None,
            access_status="unknown",
            score=None,
            covered_ratio=None,
            blind_ratio=None,
            unknown_ratio=None,
            data_notes=item.get("data_notes", ""),
        )
        for item in catalog["facilities"]
    ]
    groups = [
        GroupAssessment(
            **group,
            score=None,
            assessed_categories=0,
            total_categories=sum(
                category["group_id"] == group["id"]
                for category in catalog["facilities"]
            ),
            summary="测试报告",
        )
        for group in catalog["groups"]
    ]
    return CheckupReport(
        report_id=report_id,
        created_at="2026-10-13T00:00:00+00:00",
        center=center,
        location_label="测试地点",
        catalog_version=catalog["version"],
        isochrone=isochrone,
        search_radius_m=2600,
        categories=categories,
        groups=groups,
        score=None,
        score_method="测试用事实报告",
        summary="测试报告摘要",
        pois=[],
        routes=[],
        blindspot={},
        warnings=[],
        quality={},
    )


def _fake_runner(_request, *, report_id, progress):
    progress("report", 95)
    return _report(report_id)


def _submit(client):
    return client.post("/api/v1/checkups", json=_CHECKUP_REQUEST)


def test_checkup_batches_100m_sampling_within_the_http_budget_by_default(monkeypatch):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    monkeypatch.delenv("BAIDU_ROUTING_BATCH_SIZE", raising=False)
    batch_sizes = []

    def fake_get_json(url, _timeout):
        size = len(parse_qs(urlsplit(url).query)["destinations"][0].split("|"))
        batch_sizes.append(size)
        return {"status": 0, "result": [
            {"distance": {"value": 1}, "duration": {"value": 1}}
            for _ in range(size)
        ]}

    monkeypatch.setattr(checkups, "_get_json", fake_get_json)
    request = RoutingRequest.model_validate({
        "schema_version": "1.0",
        "origin": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
        "targets": [
            {"target_id": str(i), "location": {"lng": 121.501, "lat": 31.201, "crs": "BD09LL"}}
            for i in range(8)
        ],
        "travel_mode": "walking",
    })

    def fake_run(*_args, route, **_kwargs):
        assert route(request).root.ok
        return _report("batch-test")

    monkeypatch.setattr(checkups, "run_checkup", fake_run)
    report = checkups.get_checkup_runner()(None, report_id="batch-test")
    assert batch_sizes == [8]
    assert report.quality["route_pairs_attempted"] == 8


@pytest.mark.parametrize("value", ["0", "51", "invalid"])
def test_checkup_rejects_invalid_route_batch_size(monkeypatch, value):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    monkeypatch.setenv("BAIDU_ROUTING_BATCH_SIZE", value)
    with pytest.raises(HTTPException) as caught:
        checkups.get_checkup_runner()
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "INVALID_ROUTE_BATCH_SIZE"


def test_local_request_budget_failure_is_not_reported_as_provider_timeout(monkeypatch):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    monkeypatch.setenv("CHECKUP_MAX_HTTP_REQUESTS", "450")
    monkeypatch.setattr(checkups, "_get_json", lambda _url, _timeout: {
        "status": 0,
        "result": [{"distance": {"value": 1}, "duration": {"value": 1}}],
    })
    request = RoutingRequest.model_validate({
        "schema_version": "1.0",
        "origin": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
        "targets": [{"target_id": "one", "location": {"lng": 121.501, "lat": 31.201, "crs": "BD09LL"}}],
        "travel_mode": "walking",
    })

    def fake_run(*_args, route, **_kwargs):
        for _ in range(450):
            assert route(request).root.ok
        assert not route(request).root.ok
        raise CheckupError("ISOCHRONE_FAILED", "generic routing timeout", True)

    monkeypatch.setattr(checkups, "run_checkup", fake_run)
    runner = checkups.get_checkup_runner()
    with pytest.raises(CheckupError) as caught:
        runner(None, report_id="budget-test")

    assert caught.value.code == "PROVIDER_BUDGET_EXHAUSTED"
    assert "450 次请求" in caught.value.message
    assert "450 条步行路线" in caught.value.message
    assert caught.value.retryable is False


def test_default_checkup_budget_continues_past_450_requests(monkeypatch):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    for name in ("CHECKUP_MAX_HTTP_REQUESTS", "CHECKUP_MAX_SECONDS", "CHECKUP_MAX_COVERAGE_ROUTE_PAIRS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(checkups, "_get_json", lambda _url, _timeout: {
        "status": 0, "result": [{"distance": {"value": 1}, "duration": {"value": 1}}],
    })
    request = RoutingRequest.model_validate({
        "schema_version": "1.0", "origin": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
        "targets": [{"target_id": "one", "location": {"lng": 121.501, "lat": 31.201, "crs": "BD09LL"}}],
        "travel_mode": "walking",
    })

    def fake_run(*_args, route, max_grid_routes=5000, **_kwargs):
        for _ in range(451):
            assert route(request).root.ok
        assert max_grid_routes == 50000
        return _report("expanded-budget")

    monkeypatch.setattr(checkups, "run_checkup", fake_run)
    report = checkups.get_checkup_runner()(None, report_id="expanded-budget")
    assert report.quality["provider_requests"] == 451
    assert report.quality["provider_request_limit"] == 10000
    assert report.quality["provider_time_limit_s"] == 14400
    assert report.quality["provider_budget_exhausted"] is False


@pytest.mark.parametrize("limit", ["http", "time"])
def test_configured_checkup_limits_stop_without_an_extra_request(monkeypatch, limit):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    monkeypatch.setenv("CHECKUP_MAX_HTTP_REQUESTS", "2")
    monkeypatch.setenv("CHECKUP_MAX_SECONDS", "3")
    monkeypatch.setenv("CHECKUP_MAX_COVERAGE_ROUTE_PAIRS", "17")
    now = [0]
    calls = []
    monkeypatch.setattr(checkups, "_active_time", lambda: now[0])

    def transport(_url, _timeout):
        calls.append(1)
        return {"status": 0, "result": [{"distance": {"value": 1}, "duration": {"value": 1}}]}

    monkeypatch.setattr(checkups, "_get_json", transport)
    request = RoutingRequest.model_validate({
        "schema_version": "1.0", "origin": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
        "targets": [{"target_id": "one", "location": {"lng": 121.501, "lat": 31.201, "crs": "BD09LL"}}],
        "travel_mode": "walking",
    })

    def fake_run(*_args, route, max_grid_routes, **_kwargs):
        assert max_grid_routes == 17
        assert route(request).root.ok
        if limit == "time":
            now[0] = 3
        else:
            assert route(request).root.ok
        assert not route(request).root.ok
        raise CheckupError("ISOCHRONE_FAILED", "generic routing timeout", True)

    monkeypatch.setattr(checkups, "run_checkup", fake_run)
    with pytest.raises(CheckupError) as caught:
        checkups.get_checkup_runner()(None, report_id="configured-budget")
    assert caught.value.code == "PROVIDER_BUDGET_EXHAUSTED"
    assert "2 次" in caught.value.message and "3 秒" in caught.value.message
    assert len(calls) == (1 if limit == "time" else 2)


@pytest.mark.parametrize("name,value", [
    ("CHECKUP_MAX_HTTP_REQUESTS", "0"), ("CHECKUP_MAX_HTTP_REQUESTS", "1000001"),
    ("CHECKUP_MAX_SECONDS", "invalid"), ("CHECKUP_MAX_SECONDS", "604801"),
    ("CHECKUP_MAX_COVERAGE_ROUTE_PAIRS", "-1"), ("CHECKUP_MAX_COVERAGE_ROUTE_PAIRS", "1000001"),
])
def test_invalid_checkup_limits_are_rejected_before_running(monkeypatch, name, value):
    monkeypatch.setenv("BAIDU_MAP_AK", "test-only")
    monkeypatch.setenv(name, value)
    with pytest.raises(HTTPException) as caught:
        checkups.get_checkup_runner()
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "INVALID_CHECKUP_LIMIT"


def test_catalog_and_checkup_job_endpoints_follow_their_contract(client):
    catalog_response = client.get("/api/v1/checkup-catalog")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert len(catalog["facilities"]) == 45
    assert len(catalog["groups"]) == 7
    assert catalog["runtime"]["coverage_grid_size_m"] == 20
    assert all("search_terms" not in item for item in catalog["facilities"])

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: _fake_runner
    queued_response = _submit(client)
    assert queued_response.status_code == 202
    queued = queued_response.json()
    assert queued["status"] == "queued"
    assert queued["stage"] == "queued"
    assert queued["progress"] == 0
    assert queued["result"] is None

    checkup_id = queued["id"]
    assert client.get(f"/api/v1/checkups/{checkup_id}").json()["status"] == "queued"
    assert client.post(
        f"/api/v1/checkups/{checkup_id}/questions",
        json={"question": "先回答"},
    ).status_code == 409
    assert _submit(client).status_code == 429

    checkups._executor.run_next()
    completed_response = client.get(f"/api/v1/checkups/{checkup_id}")
    assert completed_response.status_code == 200
    completed = completed_response.json()
    assert (completed["status"], completed["stage"], completed["progress"]) == (
        "completed",
        "completed",
        100,
    )
    assert completed["result"]["report_id"] == checkup_id
    assert len(completed["result"]["categories"]) == 45
    assert len(completed["result"]["groups"]) == 7


def test_missing_server_key_unknown_job_and_untrusted_origin(client, monkeypatch):
    monkeypatch.delenv("BAIDU_MAP_AK", raising=False)
    missing_key = _submit(client)
    assert missing_key.status_code == 503
    assert missing_key.json()["detail"]["code"] == "MISSING_SERVER_AK"
    assert client.get("/api/v1/checkups/not-a-real-id").status_code == 404

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: _fake_runner
    refused = client.post(
        "/api/v1/checkups",
        json=_CHECKUP_REQUEST,
        headers={"Origin": "https://untrusted.example"},
    )
    assert refused.status_code == 403
    assert checkups._executor.pending == []


def test_model_free_question_uses_report_facts_and_does_not_mutate_report(client):
    app.dependency_overrides[checkups.get_checkup_runner] = lambda: _fake_runner
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    queued = _submit(client).json()
    checkups._executor.run_next()
    checkup_id = queued["id"]

    before = client.get(f"/api/v1/checkups/{checkup_id}").json()["result"]
    answer_response = client.post(
        f"/api/v1/checkups/{checkup_id}/questions",
        json={"question": "婴幼儿托育有多少个？"},
    )

    assert answer_response.status_code == 200
    answer = answer_response.json()
    assert answer["mode"] == "report_facts"
    assert answer["evidence_ids"] == ["facility:childcare_0_3"]
    assert "未启用模型解读" in answer["answer"]
    after = client.get(f"/api/v1/checkups/{checkup_id}").json()["result"]
    assert after == before


def test_invalid_model_evidence_becomes_a_limited_answer_without_changing_report(client):
    class InvalidEvidenceModel:
        def generate_object(self, **_kwargs):
            return {
                "answer": "没有证据支持的答案",
                "evidence_ids": ["facility:not-in-report"],
                "mode": "model",
            }

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: _fake_runner
    app.dependency_overrides[checkups.get_report_model] = lambda: InvalidEvidenceModel()
    queued = _submit(client).json()
    checkups._executor.run_next()
    checkup_id = queued["id"]
    before = client.get(f"/api/v1/checkups/{checkup_id}").json()["result"]

    response = client.post(
        f"/api/v1/checkups/{checkup_id}/questions",
        json={"question": "解释一下这个报告"},
    )

    # An unsupported answer is dropped and replaced by a limited report-facts answer;
    # the stored report is never modified by a question.
    assert response.status_code == 200
    assert response.json()["mode"] == "report_facts"
    assert "未通过证据或来源校验" in response.json()["answer"]
    assert client.get(f"/api/v1/checkups/{checkup_id}").json()["result"] == before
