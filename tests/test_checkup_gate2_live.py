"""Offline checks for bounded, resumable POI verification after a saved Gate 1."""

import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.verify_checkup_gate2_live import run_gate2


ROOT = Path(__file__).resolve().parents[1]


def gate1_file(tmp_path):
    location = json.loads((ROOT / "contracts/v1/location-result.example.json").read_text())
    isochrone = json.loads((ROOT / "contracts/v1/isochrone-result.example.json").read_text())
    source = tmp_path / "gate1.json"
    source.write_text(json.dumps({"location": location, "isochrone": isochrone}))
    return source


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_http_budget_marks_unqueried_categories_pending_and_resume_skips_complete(tmp_path):
    source = gate1_file(tmp_path)
    calls = []

    def http_get(url, timeout):
        calls.append(parse_qs(urlsplit(url).query)["query"][0])
        return response({"status": 0, "results": []})

    first, first_file = run_gate2(source, ak="fake-ak", max_http=3, output_dir=tmp_path,
                                  http_get=http_get, pace_seconds=0)
    assert len(calls) == 3
    assert first["status_counts"] == {"complete": 2, "partial": 0, "failed": 0, "pending": 43}
    assert first["stop_reason"] == "HTTP_BUDGET_EXHAUSTED"
    assert first["http_requests_by_facility_this_run"] == {"childcare_0_3": 2, "kindergarten": 1}
    assert all(row["count"] == 0 for row in first["categories"])

    second, _ = run_gate2(source, ak="fake-ak", max_http=1, output_dir=tmp_path,
                          http_get=http_get, pace_seconds=0, resume_file=first_file)
    assert len(calls) == 4
    assert calls[-1] == "小学"
    assert second["status_counts"] == {"complete": 3, "partial": 0, "failed": 0, "pending": 42}
    assert second["http_requests_this_run"] == 1


@pytest.mark.parametrize("quota_status", [4, 302])
def test_daily_quota_stops_all_further_http_and_preserves_unknown(tmp_path, quota_status):
    source = gate1_file(tmp_path)
    calls = []

    def http_get(url, timeout):
        calls.append(url)
        return response({"status": quota_status, "results": []})

    result, _ = run_gate2(source, ak="fake-ak", max_http=20, output_dir=tmp_path,
                          http_get=http_get, pace_seconds=0)
    assert len(calls) == 1
    assert result["stop_reason"] == "DAILY_POI_QUOTA_EXHAUSTED"
    assert result["status_counts"] == {"complete": 0, "partial": 0, "failed": 1, "pending": 44}
    assert result["categories"][0]["count"] is None


def test_mismatched_gate1_stops_before_http(tmp_path):
    source = gate1_file(tmp_path)
    payload = json.loads(source.read_text())
    payload["isochrone"]["data"]["time_limit_s"] = 600
    source.write_text(json.dumps(payload))

    def forbidden(url, timeout):
        raise AssertionError("must not contact provider")

    with pytest.raises(ValueError, match="matching successful 15-minute"):
        run_gate2(source, ak="fake-ak", output_dir=tmp_path,
                  http_get=forbidden, pace_seconds=0)


def test_single_facility_segment_keeps_mid_page_count_unknown(tmp_path):
    source = gate1_file(tmp_path)
    calls = []

    def http_get(url, timeout):
        calls.append(parse_qs(urlsplit(url).query)["page_num"][0])
        return response({"status": 0, "results": [{
            "uid": "school-1", "name": "小学", "location": {"lng": 121.506123, "lat": 31.282456},
        }]})

    result, _ = run_gate2(source, ak="fake-ak", max_http=1, start=3, end=3,
                          output_dir=tmp_path, http_get=http_get, pace_seconds=0)
    assert calls == ["0"]
    assert result["status_counts"] == {"complete": 0, "partial": 1, "failed": 0, "pending": 44}
    assert result["categories"][0]["facility_id"] == "primary_school"
    assert result["categories"][0]["known_count"] == 1
    assert result["categories"][0]["count"] is None


def test_resume_keeps_distinct_known_pois_from_partial_runs(tmp_path):
    source = gate1_file(tmp_path)
    calls = 0

    def http_get(url, timeout):
        nonlocal calls
        calls += 1
        return response({"status": 0, "results": [{
            "uid": f"school-{calls}", "name": "小学",
            "location": {"lng": 121.506123, "lat": 31.282456},
        }]})

    _, first_file = run_gate2(source, ak="fake-ak", max_http=1, start=3, end=3,
                              output_dir=tmp_path, http_get=http_get, pace_seconds=0)
    second, _ = run_gate2(source, ak="fake-ak", max_http=1, start=3, end=3,
                          resume_file=first_file, output_dir=tmp_path,
                          http_get=http_get, pace_seconds=0)
    assert calls == 2
    assert second["categories"][0]["search_status"] == "partial"
    assert second["categories"][0]["count"] is None
    assert second["categories"][0]["known_count"] == 2


def test_resume_reuses_completed_keyword_without_new_http(tmp_path):
    source = gate1_file(tmp_path)
    keywords = []

    def http_get(url, timeout):
        keywords.append(parse_qs(urlsplit(url).query)["query"][0])
        return response({"status": 0, "results": []})

    first, first_file = run_gate2(source, ak="fake-ak", max_http=1, start=1, end=1,
                                  output_dir=tmp_path, http_get=http_get, pace_seconds=0)
    assert first["status_counts"]["partial"] == 1
    assert list(first["completed_keywords"]) == ["托育机构"]

    second, _ = run_gate2(source, ak="fake-ak", max_http=1, start=1, end=1,
                          resume_file=first_file, output_dir=tmp_path,
                          http_get=http_get, pace_seconds=0)
    assert keywords == ["托育机构", "托育中心"]
    assert second["status_counts"]["complete"] == 1
    assert second["status_counts"]["partial"] == 0


def test_provider_page_cap_is_distinguished_from_local_budget(tmp_path):
    source = gate1_file(tmp_path)
    calls = 0

    def http_get(url, timeout):
        nonlocal calls
        calls += 1
        return response({"status": 0, "results": [{
            "uid": f"school-{calls}", "name": "小学",
            "location": {"lng": 121.506123, "lat": 31.282456},
        }]})

    result, _ = run_gate2(source, ak="fake-ak", max_http=8, start=3, end=3,
                          output_dir=tmp_path, http_get=http_get, pace_seconds=0)
    assert calls == 8
    assert result["status_counts"]["partial"] == 1
    assert result["stop_reason"] is None
    assert result["provider_error_codes_this_run"] == {"primary_school": ["INCOMPLETE_RESULTS"]}


def partial_bus_snapshot(tmp_path):
    source = gate1_file(tmp_path)
    point = json.loads(source.read_text())["location"]["data"]["center"]
    _, saved = run_gate2(source, ak="fake-ak", max_http=1, start=37, end=37,
                         output_dir=tmp_path, pace_seconds=0,
                         http_get=lambda url, timeout: response({"status": 0, "results": [{
                             "uid": "inherited", "name": "公交站", "location": point,
                         }]}))
    payload = json.loads(saved.read_text())
    payload["completed_keywords"]["公交站"] = []
    saved.write_text(json.dumps(payload))
    return source, saved, point, payload["search_radius_m"]


def poi(uid, point):
    return {"uid": uid, "name": "公交车站", "location": point}


def test_partition_circles_cover_parent_boundary_and_center():
    from scripts.verify_checkup_gate2_live import partition_circles
    from app.modules.isochrone.sampling import destination_point
    from app.schemas.common import CenterPoint
    from math import asin, cos, radians, sin, sqrt

    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    circles = partition_circles(center, 2516)
    assert len(circles) == 4
    for distance in (0, 1258, 2516):
        for bearing in range(360):
            point = destination_point(center, bearing, distance)
            def distance_to(child):
                dlat, dlng = radians(point.lat - child.lat), radians(point.lng - child.lng)
                return 12742017.6 * asin(sqrt(sin(dlat / 2) ** 2 +
                    cos(radians(point.lat)) * cos(radians(child.lat)) * sin(dlng / 2) ** 2))
            assert any(distance_to(child) <= radius for child, radius in circles)


def test_partition_subdivides_truncation_dedupes_and_filters_original_circle(tmp_path):
    from app.modules.isochrone.sampling import destination_point
    from app.schemas.common import CenterPoint

    source, saved, point, radius = partial_bus_snapshot(tmp_path)
    outside = destination_point(CenterPoint.model_validate(point), 0, radius + 100).model_dump()
    payload = json.loads(saved.read_text())
    payload["completed_keywords"]["公交站"] = [{
        "uid": "completed-outside", "name": "公交站", "lng": outside["lng"],
        "lat": outside["lat"], "address": None,
    }]
    saved.write_text(json.dumps(payload))
    calls, first_child = [], []
    def http_get(url, timeout):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        assert query["query"] == ["公交车站"]
        assert int(query["radius"][0]) < radius
        circle = (query["location"][0], query["radius"][0])
        if not first_child:
            first_child.append(circle)
        page = int(query["page_num"][0])
        items = [poi("truncated-only", point)] if circle == first_child[0] else (
            [poi("same", point), poi("outside", outside)] if page == 0 else [])
        return response({"status": 0, "results": items})

    result, _ = run_gate2(source, ak="fake-ak", max_http=30, start=37, end=37,
                          resume_file=saved, output_dir=tmp_path, http_get=http_get,
                          pace_seconds=0, partition_incomplete=True)
    row = result["categories"][0]
    assert row["search_status"] == "complete"
    assert {p["poi_id"] for p in row["pois"]} == {"baidu:same", "baidu:truncated-only"}
    assert result["center"] == point and result["search_radius_m"] == radius
    queries = list(result["partition_queries"].values())
    assert sum(q["status"] == "complete" for q in queries) == 7
    assert sum(q["status"] == "split" for q in queries) == 1
    assert all(q["captured_at"] for q in queries)
    assert "captured_at" not in result  # Inherited evidence time remains unknown.
    assert "公交车站" in result["completed_keywords"]


def test_partition_budget_resume_reuses_complete_children_and_known_results(tmp_path):
    source, saved, point, radius = partial_bus_snapshot(tmp_path)
    calls = []
    def http_get(url, timeout):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        return response({"status": 0, "results": [poi("new", point)]
                         if query["page_num"] == ["0"] else []})

    first, first_file = run_gate2(source, ak="fake-ak", max_http=3, start=37, end=37,
                                  resume_file=saved, output_dir=tmp_path, http_get=http_get,
                                  pace_seconds=0, partition_incomplete=True)
    assert first["stop_reason"] == "HTTP_BUDGET_EXHAUSTED"
    assert first["categories"][0]["count"] is None
    assert {p["poi_id"] for p in first["categories"][0]["pois"]} == {"baidu:inherited", "baidu:new"}
    complete = [q for q in first["partition_queries"].values() if q["status"] == "complete"]
    assert len(complete) == 1
    first_circle = calls[0]["location"]
    calls.clear()
    second, _ = run_gate2(source, ak="fake-ak", max_http=6, start=37, end=37,
                          resume_file=first_file, output_dir=tmp_path, http_get=http_get,
                          pace_seconds=0, partition_incomplete=True)
    assert second["categories"][0]["search_status"] == "complete"
    assert len(calls) == 6
    assert all(q["query"] == ["公交车站"] and q["location"] != first_circle for q in calls)
    assert complete[0] in second["partition_queries"].values()


@pytest.mark.parametrize("failure", ["quota", "network"])
def test_partition_failure_stops_immediately_and_keeps_complete_child(tmp_path, failure):
    source, saved, point, _ = partial_bus_snapshot(tmp_path)
    calls = []
    def http_get(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return response({"status": 0, "results": []})
        if failure == "network":
            raise OSError("offline")
        return response({"status": 302, "results": []})

    result, _ = run_gate2(source, ak="fake-ak", max_http=20, start=37, end=37,
                          resume_file=saved, output_dir=tmp_path, http_get=http_get,
                          pace_seconds=0, partition_incomplete=True)
    assert len(calls) == 2
    assert result["stop_reason"] == ("DAILY_POI_QUOTA_EXHAUSTED" if failure == "quota" else "NETWORK_ERROR")
    assert result["categories"][0]["search_status"] == "partial"
    assert result["categories"][0]["count"] is None
    assert sum(q["status"] == "complete" for q in result["partition_queries"].values()) == 1
    assert "公交车站" not in result["completed_keywords"]


@pytest.mark.parametrize("failure", ["depth", "conflict", "inherited_conflict"])
def test_partition_depth_and_coordinate_conflicts_stay_partial(tmp_path, failure):
    source, saved, point, radius = partial_bus_snapshot(tmp_path)
    calls, circles = [], []
    def http_get(url, timeout):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        circle = query["location"][0]
        if circle not in circles:
            circles.append(circle)
        changed = {**point, "lng": point["lng"] + (0.0001 if failure != "depth" and len(circles) > 1 else 0)}
        uid = "inherited" if failure == "inherited_conflict" else "same"
        items = [poi(uid, changed)] if failure == "depth" or query["page_num"] == ["0"] else []
        return response({"status": 0, "results": items})

    result, _ = run_gate2(source, ak="fake-ak", max_http=200, start=37, end=37,
                          resume_file=saved, output_dir=tmp_path, http_get=http_get,
                          pace_seconds=0, partition_incomplete=True)
    assert result["categories"][0]["search_status"] == "partial"
    assert result["categories"][0]["count"] is None
    assert "公交车站" not in result["completed_keywords"]
    if failure == "depth":
        assert len(calls) == 16  # One capped child then one capped depth-2 leaf.
        assert max(q["depth"] for q in result["partition_queries"].values()) == 2
    else:
        assert "COORDINATE_CONFLICT" in result["provider_error_codes_this_run"]["bus_stop"]


def test_partition_requires_explicit_partial_resume_selection(tmp_path):
    source = gate1_file(tmp_path)
    with pytest.raises(ValueError, match="partial"):
        run_gate2(source, ak="fake-ak", output_dir=tmp_path, partition_incomplete=True,
                  http_get=lambda *a, **k: pytest.fail("no HTTP"), pace_seconds=0)


def test_partition_repeated_budget_interruptions_preserve_all_partial_observations(tmp_path):
    source, saved, point, _ = partial_bus_snapshot(tmp_path)
    for uid in ("early-partial", "later-partial"):
        result, saved = run_gate2(source, ak="fake-ak", max_http=1, start=37, end=37,
                                 resume_file=saved, output_dir=tmp_path, pace_seconds=0,
                                 partition_incomplete=True,
                                 http_get=lambda url, timeout: response({
                                     "status": 0, "results": [poi(uid, point)],
                                 }))
        assert result["stop_reason"] == "HTTP_BUDGET_EXHAUSTED"
    result, _ = run_gate2(source, ak="fake-ak", max_http=4, start=37, end=37,
                         resume_file=saved, output_dir=tmp_path, pace_seconds=0,
                         partition_incomplete=True,
                         http_get=lambda url, timeout: response({"status": 0, "results": []}))
    assert result["categories"][0]["search_status"] == "complete"
    assert {p["poi_id"] for p in result["categories"][0]["pois"]} == {
        "baidu:early-partial", "baidu:later-partial",
    }
