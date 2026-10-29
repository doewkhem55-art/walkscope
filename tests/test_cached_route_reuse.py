"""Cache-first routing exercises real provider formats without network access."""
import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from app.errors import ProviderStopError
from app.modules.routing.service import RoutingService
from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.routing import BaiduRoutingProvider
from app.schemas.common import CenterPoint
from app.schemas.routing import RoutingRequest, RoutingResult
from tools.bounded_baidu import BoundedBaiduTransport


ORIGIN = CenterPoint(lng=126.654321987, lat=45.712345678, crs='BD09LL')


def request(count):
    return RoutingRequest.model_validate(dict(schema_version='1.0', origin=ORIGIN.model_dump(),
        travel_mode='walking', targets=[dict(target_id=f'grid:{i}', location=dict(
            lng=126.65 + i * 0.0001, lat=45.71, crs='BD09LL')) for i in range(count)]))


def wrap(callback, tmp_path, cache_dirs=None, **kwargs):
    try:
        from tools.cached_route_reuse import CacheFirstRouting
    except ModuleNotFoundError:
        pytest.fail('CacheFirstRouting is not implemented')
    return CacheFirstRouting(callback, cache_dirs or [tmp_path / 'http'], tmp_path / 'pairs', **kwargs)


def distance(target):
    return 1000 + round((target.location.lng - 126.65) * 10000)


def response(req, *, statuses=None, warning=False):
    routes = []
    for i, target in enumerate(req.targets):
        status = statuses[i] if statuses else 'success'
        row = dict(target_id=target.target_id, location=target.location.model_dump(), status=status)
        if status == 'success':
            row.update(distance_m=distance(target), duration_s=700)
        routes.append(row)
    return RoutingResult.model_validate(dict(ok=True, data=dict(origin=req.origin.model_dump(),
        travel_mode='walking', routes=routes, summary=dict(requested=len(routes),
        success=sum(r['status'] == 'success' for r in routes),
        no_route=sum(r['status'] == 'no_route' for r in routes),
        unavailable=sum(r['status'] == 'unavailable' for r in routes))),
        warnings=[dict(code='REAL_WARNING', message='Retain callback evidence')] if warning else [],
        meta=dict(schema_version='1.0', provider='offline-provider')))


def write_http(folder, url, payload):
    parts = urlsplit(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if key != 'ak'])
    path = folder / (hashlib.sha256((parts.path + '?' + query).encode()).hexdigest() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


def seed_single(folder, origin, target, distance_m=None, duration_s=700):
    payload = dict(status=0, result=dict(routes=[dict(
        distance=distance(target) if distance_m is None else distance_m, duration=duration_s)]))
    provider = BaiduDirectionProvider('fixture-private-key', transport=lambda url, _: write_http(folder, url, payload))
    try:
        provider.walking(origin, target.location)
    except Exception:
        # Malformed-response tests deliberately persist invalid historical data.
        pass


def seed_matrix(folder, req, targets=None, offset=0):
    targets = req.targets if targets is None else targets
    payload = dict(status=0, result=[dict(distance=dict(value=distance(target) + offset),
        duration=dict(value=700)) for target in targets])
    BaiduRoutingProvider('fixture-private-key', transport=lambda url, _: write_http(folder, url, payload)).route_matrix(
        req.origin, [target.location for target in targets])


def forbidden(_):
    pytest.fail('known routes must not reach the paid callback')


def test_low_quota_wrapper_uses_at_most_three_targets_and_reuses_old_25_target_http_cache(tmp_path):
    calls=[]
    def callback(req):
        calls.append(len(req.targets)); return response(req)
    router=wrap(callback,tmp_path,request_batch_size=3)
    root=router.route(request(8)).root
    assert root.data.summary.success==8 and calls==[3,3,2]
    seed_matrix(tmp_path/'other',request(25))
    root=wrap(forbidden,tmp_path/'old',cache_dirs=[tmp_path/'other'],request_batch_size=3).route(request(25)).root
    assert root.data.summary.success==25


def test_single_cache_prevents_any_matrix_or_other_paid_call(tmp_path):
    req = request(30)
    for target in req.targets:
        seed_single(tmp_path / 'http', req.origin, target)
    result = wrap(forbidden, tmp_path).route(req).root
    assert result.ok and result.data.summary.success == 30
    assert [row.target_id for row in result.data.routes] == [row.target_id for row in req.targets]
    assert [row.distance_m for row in result.data.routes] == list(range(1000, 1030))
    assert len(list((tmp_path / 'pairs').glob('*.json'))) == 30
    assert all('fixture-private-key' not in p.read_text() and 'https://' not in p.read_text()
               for p in (tmp_path / 'pairs').rglob('*.json'))


def test_all_matrix_and_single_caches_are_resolved_before_first_missing_batch(tmp_path):
    req = request(31)
    seed_matrix(tmp_path / 'matrix', req, req.targets[:25])
    for i in (25, 30):
        seed_single(tmp_path / 'single', req.origin, req.targets[i])
    calls = []

    def paid(missing):
        assert len(list((tmp_path / 'pairs').glob('*.json'))) == 27
        calls.append([target.target_id for target in missing.targets])
        return response(missing, warning=True)

    result = wrap(paid, tmp_path, [tmp_path / 'matrix', tmp_path / 'single']).route(req).root
    assert calls == [['grid:26', 'grid:27', 'grid:28', 'grid:29']]
    assert [row.distance_m for row in result.data.routes] == list(range(1000, 1031))
    assert result.meta.provider == 'offline-provider'
    assert [warning.code for warning in result.warnings] == ['REAL_WARNING']


def test_only_missing_targets_are_rebatched_to_at_most_twenty_five(tmp_path):
    req = request(61)
    for i in (0, 29, 60):
        seed_single(tmp_path / 'http', req.origin, req.targets[i])
    batches = []

    def paid(missing):
        batches.append([target.target_id for target in missing.targets])
        return response(missing)

    result = wrap(paid, tmp_path).route(req).root
    assert [len(batch) for batch in batches] == [25, 25, 8]
    assert [label for batch in batches for label in batch] == [
        f'grid:{i}' for i in range(61) if i not in (0, 29, 60)]
    assert [row.target_id for row in result.data.routes] == [f'grid:{i}' for i in range(61)]


def test_stop_preserves_completed_pairs_and_recovers_partial_single_http_cache(tmp_path):
    req = request(60)
    calls = []

    def stop_second_batch(missing):
        calls.append([target.target_id for target in missing.targets])
        if len(calls) == 2:
            for target in missing.targets[:3]:
                seed_single(tmp_path / 'http', missing.origin, target)
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', 'offline stop')
        return response(missing)

    with pytest.raises(ProviderStopError):
        wrap(stop_second_batch, tmp_path).route(req)
    assert len(calls) == 2
    assert len(list((tmp_path / 'pairs').glob('*.json'))) == 25
    resumed = []

    def paid(missing):
        resumed.extend(target.target_id for target in missing.targets)
        return response(missing)

    result = wrap(paid, tmp_path).route(req).root
    assert resumed == [f'grid:{i}' for i in range(28, 60)]
    assert result.data.summary.success == 60


def test_physical_pair_cache_rebinds_new_target_labels(tmp_path):
    req = request(2)
    wrap(response, tmp_path).route(req)
    renamed = req.model_copy(deep=True)
    renamed.targets[0].target_id = 'poi:new-label'
    renamed.targets[1].target_id = 'another-label'
    result = wrap(forbidden, tmp_path).route(renamed).root
    assert [row.target_id for row in result.data.routes] == ['poi:new-label', 'another-label']
    assert [row.distance_m for row in result.data.routes] == [1000, 1001]


def test_reverse_direction_cache_is_not_used(tmp_path):
    req = request(1)
    reversed_target = req.targets[0].model_copy(update={'location': req.origin})
    seed_single(tmp_path / 'http', req.targets[0].location, reversed_target, distance_m=555)
    calls = []

    def paid(missing):
        calls.append(missing)
        return response(missing)

    result = wrap(paid, tmp_path).route(req).root
    assert len(calls) == 1 and calls[0].origin == ORIGIN
    assert result.data.routes[0].distance_m == 1000


def test_exact_position_collision_with_same_provider_rounding_is_rejected(tmp_path):
    req = request(1)
    wrap(response, tmp_path).route(req)
    changed = req.model_copy(deep=True)
    changed.targets[0].location.lng += 0.00000001
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path).route(changed)


def test_tampered_pair_identity_is_rejected(tmp_path):
    req = request(1)
    wrap(response, tmp_path).route(req)
    path = next((tmp_path / 'pairs').glob('*.json'))
    record = json.loads(path.read_text())
    record['identity']['origin']['lng'] = 120.0
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path).route(req)


def test_disagreeing_matrix_and_single_observations_are_rejected_before_paid_calls(tmp_path):
    req = request(2)
    seed_matrix(tmp_path / 'http', req)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=9999)
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path).route(req)


@pytest.mark.parametrize('distance_m,duration_s', [(-1, 700), (float('nan'), 700),
    (True, 700), (100, -1), (100, float('inf')), (100, 0), (0, 700)])
def test_invalid_cached_distances_are_rejected(tmp_path, distance_m, duration_s):
    req = request(1)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m, duration_s)
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path).route(req)


@pytest.mark.parametrize('mutation', ['id', 'location', 'origin'])
def test_paid_response_identity_is_checked_before_persisting(tmp_path, mutation):
    req = request(1)

    def invalid(missing):
        result = response(missing)
        if mutation == 'id':
            result.root.data.routes[0].target_id = 'wrong'
        elif mutation == 'location':
            result.root.data.routes[0].location = ORIGIN
        else:
            result.root.data.origin = missing.targets[0].location
        return result

    with pytest.raises(ValueError):
        wrap(invalid, tmp_path).route(req)
    assert not list((tmp_path / 'pairs').glob('*.json'))


def test_unavailable_and_no_route_are_preserved_with_callback_warnings(tmp_path):
    req = request(3)
    result = wrap(lambda missing: response(missing,
        statuses=['success', 'no_route', 'unavailable'], warning=True), tmp_path).route(req).root
    assert [row.status.value for row in result.data.routes] == ['success', 'no_route', 'unavailable']
    assert result.data.summary.model_dump() == dict(requested=3, success=1, no_route=1, unavailable=1)
    assert result.meta.provider == 'offline-provider'
    assert [warning.code for warning in result.warnings] == ['REAL_WARNING']
    assert len(list((tmp_path / 'pairs').glob('*.json'))) == 1


def test_real_transport_budget_still_controls_each_wave_without_repeating_successful_pairs(tmp_path):
    req = request(12)
    measured = []

    def get_json(url, timeout):
        parts = urlsplit(url)
        if parts.path == '/routematrix/v2/walking':
            return {'status': 302}
        measured.append(dict(parse_qsl(parts.query))['destination'])
        return dict(status=0, result=dict(routes=[dict(distance=1000, duration=700)]))

    for wave in (1, 2):
        transport = BoundedBaiduTransport([
            dict(alias='first', value='offline-key-one'), dict(alias='second', value='offline-key-two')],
            [tmp_path / 'http'], tmp_path / f'wave{wave}.json', get_json=get_json,
            max_requests=4, min_interval=0)
        service = RoutingService(BaiduRoutingProvider('placeholder', transport=transport),
            batch_size=25, fallback_provider=BaiduDirectionProvider('placeholder', transport=transport))
        with pytest.raises(ProviderStopError):
            wrap(service.route, tmp_path).route(req)
        assert transport.requests == 4
    assert len(measured) == len(set(measured)) == 4


def test_rounded_one_meter_matrix_observation_remains_success(tmp_path):
    req = request(1)
    payload = dict(status=0, result=[dict(distance=dict(value=1), duration=dict(value=0))])
    BaiduRoutingProvider('fixture-private-key', transport=lambda url, _: write_http(
        tmp_path / 'http', url, payload)).route_matrix(req.origin, [req.targets[0].location])
    result = wrap(forbidden, tmp_path).route(req).root
    assert result.data.routes[0].status.value == 'success'
    assert (result.data.routes[0].distance_m, result.data.routes[0].duration_s) == (1, 0)


def test_no_route_cached_observation_is_not_changed_to_success(tmp_path):
    req = request(1)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=0, duration_s=0)
    result = wrap(forbidden, tmp_path).route(req).root
    assert result.data.routes[0].status.value == 'no_route'
    assert result.data.routes[0].distance_m is None
    assert not list((tmp_path / 'pairs').glob('*.json'))


def test_pair_cache_hit_does_not_hide_a_conflicting_http_observation(tmp_path):
    req = request(1)
    wrap(response, tmp_path).route(req)
    seed_matrix(tmp_path / 'http', req, offset=10)
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path).route(req)


def test_same_request_label_at_a_different_point_cannot_reuse_old_measurement(tmp_path):
    req = request(1)
    wrap(response, tmp_path).route(req)
    changed = req.model_copy(deep=True)
    changed.targets[0].location.lng += 0.01
    calls = []

    def paid(missing):
        calls.append(missing)
        return response(missing)

    result = wrap(paid, tmp_path).route(changed).root
    assert len(calls) == 1 and calls[0].targets[0].location == changed.targets[0].location
    assert result.data.routes[0].location == changed.targets[0].location
    assert result.data.routes[0].distance_m != 1000


def failure():
    return RoutingResult.model_validate(dict(ok=False,
        error=dict(code='RATE_LIMITED', message='Observed quota refusal', retryable=False),
        warnings=[dict(code='ROUTING_DAILY_QUOTA_EXHAUSTED', message='Retain quota evidence')],
        meta=dict(schema_version='1.0', provider='refusing-provider')))


def test_all_missing_failure_keeps_error_metadata_and_stops_later_batches(tmp_path):
    req = request(30)
    calls = []

    def paid(missing):
        calls.append(missing)
        return failure()

    result = wrap(paid, tmp_path).route(req)
    assert result == failure()
    assert len(calls) == 1


def test_cached_success_with_failed_missing_batch_keeps_remaining_routes_unavailable(tmp_path):
    req = request(3)
    seed_single(tmp_path / 'http', req.origin, req.targets[0])
    result = wrap(lambda _: failure(), tmp_path).route(req).root
    assert result.ok
    assert [route.status.value for route in result.data.routes] == ['success', 'unavailable', 'unavailable']
    assert result.meta.provider == 'refusing-provider'
    assert [warning.code for warning in result.warnings] == ['ROUTING_DAILY_QUOTA_EXHAUSTED']


def test_conflicting_observations_in_two_cache_directories_are_rejected(tmp_path):
    req = request(1)
    seed_single(tmp_path / 'first', req.origin, req.targets[0], distance_m=1000)
    seed_single(tmp_path / 'second', req.origin, req.targets[0], distance_m=1001)
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path, [tmp_path / 'first', tmp_path / 'second']).route(req)


def seed_matrix_observation(folder, req, distance_m, duration_s):
    payload = dict(status=0, result=[dict(distance=dict(value=distance_m), duration=dict(value=duration_s))])
    BaiduRoutingProvider('fixture-private-key', transport=lambda url, _: write_http(folder, url, payload)).route_matrix(
        req.origin, [req.targets[0].location])


@pytest.mark.parametrize('durations', [(700, 800), (1000, 1200)])
def test_isochrone_policy_selects_longer_complete_observation_and_retains_variants(tmp_path, durations):
    req = request(1)
    seed_matrix_observation(tmp_path / 'http', req, 1100, durations[0])
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=900, duration_s=durations[1])
    router = wrap(forbidden, tmp_path, observation_policy='isochrone_conservative')
    result = router.route(req).root
    assert (result.data.routes[0].distance_m, result.data.routes[0].duration_s) == (900, durations[1])
    record = json.loads(next((tmp_path / 'pairs').glob('*.json')).read_text())
    assert record['selection_policy'] == 'isochrone_conservative'
    assert record['observations'] == [
        dict(distance_m=1100, duration_s=durations[0], source='matrix_http_cache'),
        dict(distance_m=900, duration_s=durations[1], source='direction_http_cache')]
    assert any(warning.code == 'ISOCHRONE_CONSERVATIVE_OBSERVATIONS' for warning in result.warnings)
    stats = router.snapshot()
    assert stats['observation_policy'] == 'isochrone_conservative'
    assert stats['cached_target_uses'] == 1 and stats['submitted_targets'] == 0
    assert stats['duration_variant_pair_count'] == stats['distance_variant_pair_count'] == 1


def test_isochrone_duration_tie_selects_larger_real_distance(tmp_path):
    req = request(1)
    seed_matrix_observation(tmp_path / 'http', req, 1000, 900)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=1001, duration_s=900)
    result = wrap(forbidden, tmp_path, observation_policy='isochrone_conservative').route(req).root
    assert (result.data.routes[0].distance_m, result.data.routes[0].duration_s) == (1001, 900)


@pytest.mark.parametrize('durations', [(900, 901), (901, 900), (800, 1000)])
def test_isochrone_policy_rejects_observations_across_inclusive_900_second_threshold(tmp_path, durations):
    req = request(1)
    seed_matrix_observation(tmp_path / 'http', req, 1100, durations[0])
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=900, duration_s=durations[1])
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path, observation_policy='isochrone_conservative').route(req)


def test_strict_consumer_cannot_silently_reuse_conservatively_selected_variant_cache(tmp_path):
    req = request(1)
    seed_matrix_observation(tmp_path / 'http', req, 1100, 1000)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=900, duration_s=1200)
    wrap(forbidden, tmp_path, observation_policy='isochrone_conservative').route(req)
    # Only the physical pair file remains available to the strict consumer.
    with pytest.raises(ValueError):
        wrap(forbidden, tmp_path, [tmp_path / 'absent-http']).route(req)


def test_conservative_variant_cache_rebinds_new_labels_without_http(tmp_path):
    req = request(1)
    seed_matrix_observation(tmp_path / 'http', req, 1100, 700)
    seed_single(tmp_path / 'http', req.origin, req.targets[0], distance_m=900, duration_s=800)
    wrap(forbidden, tmp_path, observation_policy='isochrone_conservative').route(req)
    req.targets[0].target_id = 'isochrone:renamed'
    result = wrap(forbidden, tmp_path, [tmp_path / 'absent-http'],
                  observation_policy='isochrone_conservative').route(req).root
    assert result.data.routes[0].target_id == 'isochrone:renamed'
    assert (result.data.routes[0].distance_m, result.data.routes[0].duration_s) == (900, 800)


def test_safe_snapshot_counts_cached_uses_and_submitted_missing_targets(tmp_path):
    req = request(3)
    seed_single(tmp_path / 'http', req.origin, req.targets[0])
    router = wrap(response, tmp_path)
    router.route(req)
    assert router.snapshot() == dict(observation_policy='strict', cached_target_uses=1,
        submitted_targets=2, duration_variant_pair_count=0, distance_variant_pair_count=0)
