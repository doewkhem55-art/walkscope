"""Offline runner/accounting checks; geometry is replayed from a saved real report."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.errors import ProviderStopError
from app.modules.checkup.service import CheckupError
from app.schemas.checkup import CheckupJob
from tools import execute_two_locations as runner


SOURCE_ROOT = Path(__file__).resolve().parents[1]
MATRIX = 'https://api.map.baidu.com/routematrix/v2/walking?origins=1,2&destinations=3,4&ak=placeholder'
SECRET = 'offline-private-credential'


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def replay(tmp_path, monkeypatch):
    source = CheckupJob.model_validate_json(
        (SOURCE_ROOT / 'data/reports' / f'{runner.CURRENT}_report.json').read_text())
    reports = tmp_path / 'data/reports'
    reports.mkdir(parents=True)
    # The handoff index identifies the original 19 immutable reports, even
    # while an unrelated live process is adding later Tongji reports.
    index = json.loads((SOURCE_ROOT / 'docs/报告索引.json').read_text())
    original = {}
    for row in index:
        name = f"{row['id']}_report.json"
        content = (SOURCE_ROOT / 'data/reports' / name).read_bytes()
        (reports / name).write_bytes(content)
        original[name] = hashlib.sha256(content).hexdigest()
    assert len(original) == 19
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'REPORTS', reports)
    monkeypatch.setattr(runner, 'PROGRESS', tmp_path / 'data/progress')
    monkeypatch.setattr(runner, 'credentials', lambda: [
        {'alias': 'first', 'value': SECRET}, {'alias': 'second', 'value': 'offline-other-credential'}])
    monkeypatch.setattr(runner, 'harbin_match', lambda: {
        'name': '地震局家属楼', 'city': '哈尔滨市', 'district': '南岗区',
        'location': {'lng': source.result.center.lng, 'lat': source.result.center.lat}})
    transports = []
    clocks = []
    transport_class = runner.BoundedBaiduTransport

    def get_json(url, timeout):
        if urlsplit(url).path == '/routematrix/v2/walking':
            return {'status': 302, 'message': 'offline quota fixture'}
        return {'status': 0, 'result': {'routes': [{'distance': 800, 'duration': 600}]}}

    def transport(*args, **kwargs):
        clock = Clock()
        value = transport_class(*args, **kwargs, get_json=get_json,
                                clock=clock, sleep=clock.sleep)
        transports.append(value)
        clocks.append(clock)
        return value

    monkeypatch.setattr(runner, 'BoundedBaiduTransport', transport)
    return source.result, transports, clocks, original


def direction(transport, sample):
    return transport('https://api.map.baidu.com/directionlite/v1/walking?'
                     f'origin=1,2&destination=3,{sample}&ak=placeholder', 8)


def test_failed_waves_keep_ledgers_cache_and_exact_cumulative_accounting(replay, monkeypatch, capsys):
    report, transports, clocks, originals = replay
    wave1, wave2, wave3 = [f'{runner.HARBIN}-wave{i}' for i in (1, 2, 3)]

    def interrupted(request, *, progress, on_update, report_id, **kwargs):
        assert report_id == runner.HARBIN
        progress('location', 2)
        progress('isochrone', 8)
        on_update({'report_id': report_id, 'center': report.center.model_dump()})
        transport = transports[-1]
        try:
            assert transport(MATRIX, 8)['status'] == 302
            # Two rejected matrix HTTP calls plus 448 successful single-route
            # calls reach the unchanged 450-request budget in each wave.
            for sample in range(1000):
                direction(transport, sample)
        except ProviderStopError as exc:
            raise CheckupError(exc.code, f'https://private.invalid/?ak={SECRET}') from None
        pytest.fail('the per-wave budget must stop this replay')

    monkeypatch.setattr(runner, 'run_checkup', interrupted)
    with pytest.raises(CheckupError):
        runner.run_harbin(accounting_id=wave1)
    preserved = {path.name: path.read_bytes() for path in runner.PROGRESS.glob(f'{wave1}_*.json')}
    with pytest.raises(CheckupError):
        runner.run_harbin(accounting_id=wave2)
    assert len(preserved) == 3
    assert all((runner.PROGRESS / name).read_bytes() == content for name, content in preserved.items())
    for wave in (wave1, wave2):
        state = json.loads((runner.PROGRESS / f'{wave}_execution.json').read_text())
        ledger = json.loads((runner.PROGRESS / f'{wave}_requests.json').read_text())
        assert state['failed'] is True
        assert state['error_type'] == 'CheckupError'
        assert state['error_code'] == 'PROVIDER_BUDGET_EXHAUSTED'
        assert state['phase'] == 'isochrone'
        assert state['http']['requests'] == 450
        assert state['phase_http_requests'] == {'location': 0, 'isochrone': 450}
        assert ledger['status_counts'] == {'302': 2, '0': 448}
        assert ledger['max_requests'] == 450 and ledger['max_seconds'] == 1200
    assert transports[1].cache_hits == 448
    for clock in clocks:
        assert clock.waits
        assert all(seconds == pytest.approx(0.55) and 1 / seconds < 3 for seconds in clock.waits)

    # Ignore unrelated execution records and never count the current wave twice.
    runner.atomic(runner.PROGRESS / 'tongji-unrelated_execution.json',
                  {'http': {'requests': 999}, 'phase_http_requests': {'coverage': 999}})

    def completed(request, *, report_id, progress, on_update, **kwargs):
        progress('location', 2)
        on_update({'report_id': report_id, 'center': report.center.model_dump()})
        for phase, count in [('isochrone', 2), ('poi', 3), ('routing', 5), ('coverage', 10)]:
            progress(phase, 50)
            for sample in range(count):
                direction(transports[-1], f'{phase}-{sample}')
        progress('report', 94)
        # Replay actual saved evidence; the runner test creates no new coverage.
        return report.model_copy(deep=True, update={'report_id': report_id})

    monkeypatch.setattr(runner, 'run_checkup', completed)
    runner.run_harbin(accounting_id=wave3)
    saved = CheckupJob.model_validate_json((runner.REPORTS / f'{runner.HARBIN}_report.json').read_text())
    assert saved.id == saved.result.report_id == runner.HARBIN
    quality = saved.result.quality
    assert quality['new_http_requests'] == 20
    assert quality['phase_http_requests'] == {
        'location': 0, 'isochrone': 2, 'poi': 3, 'routing': 5, 'coverage': 10, 'report': 0}
    assert quality['new_isochrone_requests'] == 2
    assert quality['new_poi_search_requests'] == 3
    assert quality['new_center_route_requests'] == 5
    assert quality['new_coverage_requests'] == 10
    assert quality['cumulative_new_http_requests'] == 920
    assert quality['cumulative_phase_http_requests'] == {
        'location': 0, 'isochrone': 902, 'poi': 3, 'routing': 5, 'coverage': 10, 'report': 0}
    prior = quality['prior_attempt_accounting']
    assert {row['accounting_id'] for row in prior} == {wave1, wave2}
    assert all(row['failed'] and row['http']['status_counts']['302'] == 2 for row in prior)
    assert json.loads((runner.PROGRESS / f'{wave3}_execution.json').read_text())['phase_http_requests'] == quality['phase_http_requests']
    assert not (runner.PROGRESS / f'{runner.HARBIN}_execution.json').exists()
    for name, digest in originals.items():
        assert hashlib.sha256((runner.REPORTS / name).read_bytes()).hexdigest() == digest
    assert SECRET not in capsys.readouterr().out
    for path in runner.PROGRESS.glob('*_execution.json'):
        assert SECRET not in path.read_text()


@pytest.mark.parametrize(('accounting_id', 'interval'), [
    (runner.HARBIN, 0.55), (f'{runner.HARBIN}-wave1', 0.55), ('tongji-offline', 1.5)])
def test_http_requests_keep_location_specific_pacing(replay, accounting_id, interval):
    _, transports, clocks, _ = replay
    transport = runner.transport_for(accounting_id)
    for sample in range(3):
        direction(transport, sample)
    assert transports[-1].requests == 3 and transports[-1].cache_hits == 0
    assert clocks[-1].waits == pytest.approx([interval, interval])


@pytest.mark.parametrize('accounting_id', ['', '../escape', '/tmp/escape', 'other-wave1',
    f'{runner.HARBIN}-wave/escape', f'{runner.HARBIN}-wave1?ak=private'])
def test_unsafe_accounting_id_is_rejected_before_reading_credentials(tmp_path, monkeypatch, accounting_id):
    monkeypatch.setattr(runner, 'PROGRESS', tmp_path)
    monkeypatch.setattr(runner, 'harbin_match', lambda: pytest.fail('validation must precede private data reads'))
    with pytest.raises(ValueError):
        runner.run_harbin(accounting_id=accounting_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('suffix', ['requests', 'execution', 'partial_evidence'])
def test_existing_wave_evidence_cannot_be_replaced(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(runner, 'REPORTS', tmp_path / 'reports')
    monkeypatch.setattr(runner, 'PROGRESS', tmp_path)
    wave = f'{runner.HARBIN}-wave1'
    existing = tmp_path / f'{wave}_{suffix}.json'
    existing.write_text('{"original": true}')
    monkeypatch.setattr(runner, 'harbin_match', lambda: pytest.fail('existing evidence must be checked first'))
    with pytest.raises(FileExistsError):
        runner.run_harbin(accounting_id=wave)
    assert existing.read_text() == '{"original": true}'


def test_cli_dispatches_accounting_id_and_safely_reports_failure(monkeypatch, capsys):
    wave = f'{runner.HARBIN}-wave4'

    def fail(accounting_id=None):
        assert accounting_id == wave
        raise CheckupError('ISOCHRONE_FAILED', f'https://private.invalid/?ak={SECRET}')

    monkeypatch.setattr(runner, 'run_harbin', fail)
    assert runner.main(['harbin', '--accounting-id', wave]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        'task': 'harbin', 'failed': True, 'error_type': 'CheckupError', 'error_code': 'ISOCHRONE_FAILED'}
    assert not output.err and SECRET not in output.out


def test_setup_failure_preserves_current_snapshot_without_leaking_exception(replay, monkeypatch, capsys):
    _, transports, _, _ = replay
    wave = f'{runner.HARBIN}-wavesetup'

    def fail(transport):
        direction(transport, 'setup')
        raise CheckupError('SETUP_FAILED', f'https://private.invalid/?ak={SECRET}')

    monkeypatch.setattr(runner, 'routing_for', fail)
    with pytest.raises(CheckupError):
        runner.run_harbin(accounting_id=wave)
    state = json.loads((runner.PROGRESS / f'{wave}_execution.json').read_text())
    assert state['failed'] is True and state['phase'] == 'initializing'
    assert state['error_code'] == 'SETUP_FAILED'
    assert state['http']['requests'] == transports[-1].requests == 1
    assert state['phase_http_requests'] == {'initializing': 1}
    assert SECRET not in capsys.readouterr().out


def test_prior_accounting_includes_legacy_and_excludes_current(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'PROGRESS', tmp_path)
    current = f'{runner.HARBIN}-wave2'
    runner.atomic(tmp_path / f'{runner.HARBIN}_execution.json',
                  {'http': {'requests': 2, 'status_counts': {'302': 2}}})
    runner.atomic(tmp_path / f'{current}_execution.json', {'http': {'requests': 999}})
    assert runner.prior_harbin_accounting(current) == [{
        'accounting_id': runner.HARBIN, 'http': {'requests': 2, 'status_counts': {'302': 2}}}]


def test_cli_does_not_publish_an_untrusted_checkup_error_code(monkeypatch, capsys):
    def fail(accounting_id=None):
        raise CheckupError(f'https://private.invalid/?ak={SECRET}', 'private details')

    monkeypatch.setattr(runner, 'run_harbin', fail)
    assert runner.main(['harbin']) == 1
    assert json.loads(capsys.readouterr().out) == {
        'task': 'harbin', 'failed': True, 'error_type': 'CheckupError'}


def _seed_route_http_cache(folder, operation, payload):
    """Use real provider query formatting to seed a credential-free raw cache."""
    from urllib.parse import parse_qsl, urlencode

    def write(url, timeout):
        parts = urlsplit(url)
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if key != 'ak'])
        path = folder / (hashlib.sha256((parts.path + '?' + query).encode()).hexdigest() + '.json')
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return payload

    operation(write)


@pytest.mark.parametrize('service_conflict', [False, True])
def test_harbin_uses_separate_isochrone_policy_and_strict_service_cache_with_durable_stats(
        replay, monkeypatch, service_conflict):
    from app.providers.baidu.direction import BaiduDirectionProvider
    from app.providers.baidu.routing import BaiduRoutingProvider
    from app.schemas.isochrone import IsochroneRequest, IsochroneResult
    from app.schemas.routing import RoutingRequest
    from tools.cached_route_reuse import CacheValidationError

    report, transports, _, _ = replay
    center = report.center
    origin = center.model_dump()

    def route_request(offsets):
        return RoutingRequest.model_validate(dict(schema_version='1.0', origin=origin,
            travel_mode='walking', targets=[dict(target_id=f'point:{offset}', location=dict(
                lng=center.lng + offset, lat=center.lat, crs='BD09LL')) for offset in offsets]))

    iso_request = route_request([0.001, 0.002])
    service_request = iso_request if service_conflict else route_request([0.003, 0.004])
    _seed_route_http_cache(runner.ROOT / 'data/cache/shared500',
        lambda transport: BaiduRoutingProvider('fixture-key', transport=transport).route_matrix(
            center, [target.location for target in iso_request.targets]),
        dict(status=0, result=[dict(distance=dict(value=1000), duration=dict(value=700)),
                              dict(distance=dict(value=1100), duration=dict(value=1000))]))
    _seed_route_http_cache(runner.ROOT / 'data/cache/coverage-20m',
        lambda transport: BaiduDirectionProvider('fixture-key', transport=transport).walking(
            center, iso_request.targets[0].location),
        dict(status=0, result=dict(routes=[dict(distance=900, duration=800)])))
    if not service_conflict:
        _seed_route_http_cache(runner.ROOT / 'data/cache/actual-poi',
            lambda transport: BaiduDirectionProvider('fixture-key', transport=transport).walking(
                center, service_request.targets[0].location),
            dict(status=0, result=dict(routes=[dict(distance=500, duration=400)])))

    def generate(route, request, **options):
        assert options == dict(cell_m=100, max_grid_points=2500, include_tiers=True)
        measured = route(iso_request).root
        assert [(row.distance_m, row.duration_s) for row in measured.data.routes] == [(900, 800), (1100, 1000)]
        assert any(warning.code == 'ISOCHRONE_CONSERVATIVE_OBSERVATIONS' for warning in measured.warnings)
        # Reuse the accepted real geometry fixture; this runner test only drives
        # route selection, cache persistence, and accounting boundaries.
        return IsochroneResult.model_validate(dict(ok=True, data=report.isochrone.model_dump(mode='json'),
            warnings=[warning.model_dump() for warning in measured.warnings],
            meta=dict(schema_version='1.0', provider='fixture')))

    def checkup(request, *, report_id, generate_isochrone, route, progress, on_update, **kwargs):
        progress('location', 2)
        on_update(dict(report_id=report_id, center=origin))
        progress('isochrone', 8)
        iso = generate_isochrone(IsochroneRequest(schema_version='1.0', center=center, time_limit_s=900))
        progress('poi', 25)
        progress('routing', 57)
        route(service_request)
        progress('coverage', 65)
        progress('report', 94)
        return report.model_copy(deep=True, update=dict(report_id=report_id, warnings=iso.root.warnings))

    monkeypatch.setattr(runner, 'generate_grid_isochrone', generate)
    monkeypatch.setattr(runner, 'run_checkup', checkup)
    wave = f'{runner.HARBIN}-wavepolicy{int(service_conflict)}'
    if service_conflict:
        with pytest.raises(CacheValidationError):
            runner.run_harbin(accounting_id=wave)
        state = json.loads((runner.PROGRESS / f'{wave}_execution.json').read_text())
        assert state['failed'] and state['error_type'] == 'CacheValidationError'
        assert state['phase_http_requests'] == dict(location=0, isochrone=0, poi=0, routing=0)
        assert transports[-1].requests == 0
        reuse = state['route_cache_reuse']
        assert reuse['service']['observation_policy'] == 'strict'
        assert reuse['service']['submitted_targets'] == 0
        assert not (runner.REPORTS / f'{runner.HARBIN}_report.json').exists()
    else:
        runner.run_harbin(accounting_id=wave)
        saved = CheckupJob.model_validate_json((runner.REPORTS / f'{runner.HARBIN}_report.json').read_text())
        quality = saved.result.quality
        reuse = quality['route_cache_reuse']
        assert reuse['service'] == dict(observation_policy='strict', cached_target_uses=1,
            submitted_targets=1, duration_variant_pair_count=0, distance_variant_pair_count=0)
        assert quality['new_http_requests'] == quality['new_center_route_requests'] == transports[-1].requests == 3
        assert quality['phase_http_requests'] == dict(location=0, isochrone=0, poi=0, routing=3, coverage=0, report=0)
        state = json.loads((runner.PROGRESS / f'{wave}_execution.json').read_text())
        assert state['route_cache_reuse'] == reuse
    assert reuse['isochrone'] == dict(observation_policy='isochrone_conservative', cached_target_uses=2,
        submitted_targets=0, duration_variant_pair_count=1, distance_variant_pair_count=1)
    iso_pairs = list((runner.ROOT / 'data/cache/harbin-isochrone-route-pairs').glob('*.json'))
    service_pairs = list((runner.ROOT / 'data/cache/harbin-service-route-pairs').glob('*.json'))
    assert len(iso_pairs) == len(service_pairs) == 2
    assert all(json.loads(path.read_text())['selection_policy'] == 'isochrone_conservative' for path in iso_pairs)
    assert all(json.loads(path.read_text())['selection_policy'] == 'strict' for path in service_pairs)
