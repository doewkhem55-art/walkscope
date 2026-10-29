import hashlib
import json
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.errors import ProviderStopError
from app.modules.routing.service import RoutingService
from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.routing import BaiduRoutingProvider
from app.schemas.routing import RoutingRequest


MATRIX = "https://api.map.baidu.com/routematrix/v2/walking?origins=1%2C2&destinations=3%2C4&ak=provider-placeholder"
DIRECTION = "https://api.map.baidu.com/directionlite/v1/walking?origin=1%2C2&destination=3%2C4&ak=provider-placeholder"
CREDENTIALS = [dict(alias="first", value="fakecredential-one"), dict(alias="second", value="fakecredential-two")]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def transport(tmp_path, get_json, **kwargs):
    try:
        from tools.bounded_baidu import BoundedBaiduTransport
    except ModuleNotFoundError:
        pytest.fail("BoundedBaiduTransport is not implemented")
    clock = kwargs.pop("clock", FakeClock())
    return BoundedBaiduTransport(
        credentials=kwargs.pop("credentials", CREDENTIALS),
        cache_dirs=kwargs.pop("cache_dirs", [tmp_path / "cache"]),
        ledger_path=tmp_path / "ledger.json", get_json=get_json,
        clock=clock, sleep=clock.sleep, **kwargs,
    )


def test_matrix_concurrency_uses_route_pairs_and_rejects_a_batch_above_the_quota(tmp_path):
    clock=FakeClock(); seen=[]
    bounded=transport(tmp_path,lambda url,_:(seen.append(clock.now) or {'status':0}),
        clock=clock,min_interval=.36,route_pair_qps=3)
    three=MATRIX.replace('destinations=3%2C4','destinations=3%2C4%7C5%2C6%7C7%2C8')
    bounded(three,8)
    bounded(three.replace('origins=1%2C2','origins=2%2C3'),8)
    assert seen[1]-seen[0]>=1.0
    too_many=three.replace('%7C7%2C8','%7C7%2C8%7C9%2C10')
    with pytest.raises(ValueError,match='route-pair concurrency'):
        bounded(too_many,8)
    assert bounded.requests==2


@pytest.mark.parametrize("key_count", [1, 2, 4])
def test_exhausted_endpoint_tries_each_key_once_and_stops_http(tmp_path, key_count):
    seen = []

    def request(url, timeout):
        seen.append(parse_qs(urlsplit(url).query)["ak"][0])
        return {"status": 302, "message": "daily quota"}

    credentials = [dict(alias=f"key-{index}", value=f"fakecredential-{index}") for index in range(key_count)]
    bounded = transport(tmp_path, request, credentials=credentials)
    assert bounded(MATRIX, 8)["status"] == 302
    assert bounded(MATRIX.replace("3%2C4", "5%2C6"), 8)["status"] == 302
    assert seen == [f"fakecredential-{index}" for index in range(key_count)]
    assert bounded.requests == key_count
    assert bounded.snapshot()["endpoint_exhausted"] == ["/routematrix/v2/walking"]


@pytest.mark.parametrize("status", [3, 4, 5, 101, 102, 200, 201, 202, 203, 210, 211, 220, 230, 240, 301, 302])
def test_quota_or_credential_rejection_moves_to_next_key(tmp_path, status):
    seen = []

    def request(url, timeout):
        key = parse_qs(urlsplit(url).query)["ak"][0]
        seen.append(key)
        return {"status": status} if key == "fakecredential-one" else {"status": 0, "result": []}

    bounded = transport(tmp_path, request)
    assert bounded(MATRIX, 8)["status"] == 0
    assert bounded(MATRIX.replace("3%2C4", "5%2C6"), 8)["status"] == 0
    assert seen == ["fakecredential-one", "fakecredential-two", "fakecredential-two"]


def test_success_cache_hash_ignores_key_and_reads_other_directories(tmp_path):
    old_cache = tmp_path / "old-cache"
    old_cache.mkdir()
    # Hand-derived canonical request matches recompute_actual_poi_case.py.
    filename = hashlib.sha256(b"/routematrix/v2/walking?origins=1%2C2&destinations=3%2C4").hexdigest() + ".json"
    (old_cache / filename).write_text(json.dumps({"status": 0, "result": ["reused"]}))

    def no_network(url, timeout):
        pytest.fail("cached requests must not issue HTTP")

    bounded = transport(tmp_path, no_network, cache_dirs=[tmp_path / "new-cache", old_cache])
    assert bounded(MATRIX, 8)["result"] == ["reused"]
    assert bounded(MATRIX.replace("provider-placeholder", "different-key"), 8)["result"] == ["reused"]
    assert bounded.requests == 0
    assert bounded.cache_hits == 2


def test_only_success_is_cached_and_written_to_first_directory(tmp_path):
    responses = iter([{"status": 1}, {"status": 0, "result": ["fresh"]}])
    bounded = transport(tmp_path, lambda *_: next(responses), cache_dirs=[tmp_path / "new", tmp_path / "old"])
    assert bounded(MATRIX, 8)["status"] == 1
    assert not list((tmp_path / "new").glob("*.json"))
    assert bounded(MATRIX, 8)["result"] == ["fresh"]
    assert bounded(MATRIX, 8)["result"] == ["fresh"]
    assert len(list((tmp_path / "new").glob("*.json"))) == 1
    assert not list((tmp_path / "old").glob("*.json"))
    assert (bounded.requests, bounded.cache_hits) == (2, 1)


def test_matrix_exhaustion_allows_routing_service_direction_fallback(tmp_path):
    seen = []

    def request(url, timeout):
        seen.append((urlsplit(url).path, parse_qs(urlsplit(url).query)["ak"][0]))
        if "/routematrix/" in url:
            return {"status": 302}
        return {"status": 0, "result": {"routes": [{"distance": 800, "duration": 600}]}}

    bounded = transport(tmp_path, request)
    service = RoutingService(BaiduRoutingProvider("placeholder", transport=bounded),
        fallback_provider=BaiduDirectionProvider("placeholder", transport=bounded))
    result = service.route(RoutingRequest.model_validate({
        "schema_version": "1.0",
        "origin": {"lng": 121.5, "lat": 31.2, "crs": "BD09LL"},
        "targets": [{"target_id": "school", "location": {"lng": 121.51, "lat": 31.21, "crs": "BD09LL"}}],
        "travel_mode": "walking",
    }))
    assert result.root.ok
    assert result.root.data.routes[0].distance_m == 800
    assert seen == [("/routematrix/v2/walking", "fakecredential-one"),
                    ("/routematrix/v2/walking", "fakecredential-two"),
                    ("/directionlite/v1/walking", "fakecredential-one")]


def test_concurrency_response_stays_on_same_key_for_provider_retry(tmp_path):
    seen = []

    def request(url, timeout):
        seen.append(parse_qs(urlsplit(url).query)["ak"][0])
        return {"status": 401}

    bounded = transport(tmp_path, request)
    assert bounded(MATRIX, 8)["status"] == 401
    assert bounded(MATRIX, 8)["status"] == 401
    assert seen == ["fakecredential-one", "fakecredential-one"]
    assert bounded.snapshot()["endpoint_exhausted"] == []


def test_request_budget_stops_mid_rotation_and_remains_stopped(tmp_path):
    bounded = transport(tmp_path, lambda *_: {"status": 302}, max_requests=1)
    for _ in range(2):
        with pytest.raises(ProviderStopError) as caught:
            bounded(MATRIX, 8)
        assert caught.value.code == "PROVIDER_BUDGET_EXHAUSTED"
    assert bounded.requests == 1


def test_time_budget_is_rechecked_after_throttle_and_clamps_timeout(tmp_path):
    clock = FakeClock()
    timeouts = []

    def request(url, timeout):
        timeouts.append(timeout)
        return {"status": 1}

    bounded = transport(tmp_path, request, clock=clock, max_seconds=1, min_interval=1.5)
    assert bounded(MATRIX, 8)["status"] == 1
    with pytest.raises(ProviderStopError):
        bounded(MATRIX, 8)
    assert timeouts == [1]
    assert bounded.requests == 1


def test_attempt_count_is_durable_before_http_and_diagnostics_hide_keys(tmp_path):
    def request(url, timeout):
        state = json.loads((tmp_path / "ledger.json").read_text())
        assert state["requests"] == 1
        assert state["attempts"][-1] == {
            "attempt": 1, "alias": "first", "endpoint": "/routematrix/v2/walking", "status": "pending",
        }
        return {"status": 0, "result": []}

    bounded = transport(tmp_path, request)
    bounded(MATRIX, timeout=8)
    state = json.loads((tmp_path / "ledger.json").read_text())
    assert state["attempts"][-1]["status"] == 0
    assert state["requests_by_alias"] == {"first": 1, "second": 0}
    assert state["status_counts"] == {"0": 1}
    text = json.dumps(state) + json.dumps(bounded.snapshot())
    for credential in CREDENTIALS:
        assert credential["value"] not in text
    assert "https://" not in text
    assert "?" not in text


@pytest.mark.parametrize("kind", ["url", "http", "timeout", "other"])
def test_network_error_is_safe_counted_and_not_internally_retried(tmp_path, kind):
    def request(url, timeout):
        detail = url + " fakecredential-one fakecredential-two"
        if kind == "url":
            raise URLError(detail)
        if kind == "http":
            raise HTTPError(url, 429, detail, None, None)
        if kind == "timeout":
            raise TimeoutError(detail)
        raise RuntimeError(detail)

    bounded = transport(tmp_path, request)
    with pytest.raises((OSError, URLError)) as caught:
        bounded(MATRIX, 8)
    diagnostics = "".join(traceback.format_exception(caught.value)) + (tmp_path / "ledger.json").read_text()
    for credential in CREDENTIALS:
        assert credential["value"] not in diagnostics
    assert MATRIX not in diagnostics
    assert bounded.requests == 1
    assert bounded.snapshot()["endpoint_exhausted"] == []


def test_provider_error_payload_redacts_echoed_urls_and_credentials(tmp_path):
    def request(url, timeout):
        return {"status": 302, "message": url, "detail": {"echo": ["fakecredential-one", "fakecredential-two"]}}

    bounded = transport(tmp_path, request)
    payload = bounded(MATRIX, 8)
    assert payload["status"] == 302
    assert bounded(MATRIX, 8) == payload
    diagnostics = json.dumps(payload) + (tmp_path / "ledger.json").read_text()
    for credential in CREDENTIALS:
        assert credential["value"] not in diagnostics
    assert "https://" not in diagnostics
