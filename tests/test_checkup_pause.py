"""Cooperative pause/resume checks with held threads and fake HTTP only."""
import io
import json
import threading

import pytest

from app.api import checkups
from app.api.main import app
from app.modules.checkup.control import CheckupControl
from app.schemas.checkup import CheckupJob
from app.schemas.routing import RoutingRequest
from tests.test_checkup_api import _CHECKUP_REQUEST, _fake_runner, _report
from tests.test_checkup_progress import progress_client, _partial


@pytest.fixture
def pause_client(progress_client, monkeypatch):
    monkeypatch.setattr(checkups, '_controls', {})
    return progress_client


def post(client, identifier, action, **kwargs):
    return client.post(f'/api/v1/checkups/{identifier}/{action}', json={}, **kwargs)


def test_running_pause_retains_evidence_blocks_new_job_and_resumes_same_worker(pause_client):
    client, directory = pause_client
    published, proceed, crossing, continued = (threading.Event() for _ in range(4))
    invocations = []

    def runner(_request, *, report_id, progress):
        invocations.append(report_id)
        progress('poi', 30)
        progress.publish(_partial(report_id))
        published.set()
        assert proceed.wait(3)
        crossing.set()
        progress('routing', 60)
        continued.set()
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert published.wait(3)
        paused = post(client, identifier, 'pause').json()
        assert (paused['status'], paused['stage'], paused['progress']) == ('paused', 'poi', 30)
        assert paused['partial_result']['groups'][0]['score'] == 60
        assert post(client, identifier, 'pause').json() == paused
        assert client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).status_code == 429
        checkpoint = directory / 'progress' / f'{identifier}_partial.json'
        assert json.loads(checkpoint.read_text())['status'] == 'paused'
        proceed.set()
        assert crossing.wait(3) and not continued.wait(.03)
        assert client.get(f'/api/v1/checkups/{identifier}').json() == paused
        resumed = post(client, identifier, 'resume').json()
        assert resumed['id'] == identifier and resumed['status'] == 'running'
        assert resumed['partial_result'] == paused['partial_result']
    finally:
        proceed.set()
        post(client, identifier, 'resume')
        worker.join(3)
    assert not worker.is_alive() and continued.is_set()
    assert invocations == [identifier] and checkups._executor.pending == []
    completed = client.get(f'/api/v1/checkups/{identifier}').json()
    assert completed['status'] == 'completed'
    assert post(client, identifier, 'pause').json() == completed
    assert post(client, identifier, 'resume').json() == completed


def test_queued_pause_prevents_runner_start_and_enforces_origin(pause_client):
    client, _ = pause_client
    started = threading.Event()

    def runner(*args, **kwargs):
        started.set()
        return _fake_runner(*args, **kwargs)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    assert post(client, identifier, 'pause', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert post(client, 'not-a-job', 'pause').status_code == 404
    assert post(client, identifier, 'pause').json()['stage'] == 'queued'
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert not started.wait(.03)
        assert post(client, identifier, 'resume').json()['status'] == 'queued'
    finally:
        post(client, identifier, 'resume')
        worker.join(3)
    assert not worker.is_alive() and started.is_set()


def test_pause_during_throttle_checks_again_before_real_http(pause_client, monkeypatch):
    client, _ = pause_client
    sleeping, release_sleep, sent = (threading.Event() for _ in range(3))
    requests = []

    def fake_sleep(_seconds):
        sleeping.set()
        assert release_sleep.wait(3)

    def fake_urlopen(url, timeout):
        requests.append(url)
        sent.set()
        return io.StringIO('{"mock":true}')

    monkeypatch.setattr(checkups.time, 'sleep', fake_sleep)
    monkeypatch.setattr(checkups, 'urlopen', fake_urlopen)

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        assert checkups._get_json('https://mock.invalid/poi', 1) == {'mock': True}
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert sleeping.wait(3)
        assert post(client, identifier, 'pause').json()['status'] == 'paused'
        release_sleep.set()
        assert not sent.wait(.03), 'no urlopen after a pause accepted during throttling'
        post(client, identifier, 'resume')
    finally:
        release_sleep.set()
        post(client, identifier, 'resume')
        worker.join(3)
    assert not worker.is_alive() and requests == ['https://mock.invalid/poi']
    assert client.get(f'/api/v1/checkups/{identifier}').json()['status'] == 'completed'


def test_inflight_http_may_finish_but_next_request_waits_for_resume(pause_client, monkeypatch):
    client, _ = pause_client
    inflight, release_first, next_attempt, second_sent = (threading.Event() for _ in range(4))
    requests = []

    def fake_urlopen(url, timeout):
        requests.append(url)
        if len(requests) == 1:
            inflight.set()
            assert release_first.wait(3)
        else:
            second_sent.set()
        return io.StringIO('{}')

    monkeypatch.setattr(checkups, 'urlopen', fake_urlopen)
    monkeypatch.setattr(checkups.time, 'sleep', lambda _: None)

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        checkups._get_json('https://mock.invalid/first', 1)
        next_attempt.set()
        checkups._get_json('https://mock.invalid/second', 1)
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert inflight.wait(3)
        assert post(client, identifier, 'pause').json()['status'] == 'paused'
        release_first.set()
        assert next_attempt.wait(3) and not second_sent.wait(.03)
        post(client, identifier, 'resume')
    finally:
        release_first.set()
        post(client, identifier, 'resume')
        worker.join(3)
    assert not worker.is_alive() and len(requests) == 2


def test_paused_minutes_do_not_consume_the_twenty_minute_transport_budget(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(checkups.time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('BAIDU_MAP_AK', 'offline-test-only')
    monkeypatch.setattr(checkups, '_get_json', lambda *_: {'status': 0, 'result': [
        {'distance': {'value': 1}, 'duration': {'value': 1}}
    ]})
    control = CheckupControl()
    request = RoutingRequest.model_validate({
        'schema_version': '1.0', 'origin': _CHECKUP_REQUEST['location'] | {'type': 'coordinate'},
        'targets': [{'target_id': 'one', 'location': {'lng': 121.501, 'lat': 31.201, 'crs': 'BD09LL'}}],
        'travel_mode': 'walking',
    } | {'origin': {k: v for k, v in _CHECKUP_REQUEST['location'].items() if k != 'type'}})

    def runner(*args, route, **kwargs):
        assert route(request).root.ok
        control.pause()
        now[0] += 1300
        assert control.active_time() == 100
        control.resume()
        assert route(request).root.ok, '1300 paused seconds must not exhaust a 1200 active-second budget'
        return _report('pause-budget')

    monkeypatch.setattr(checkups, 'run_checkup', runner)
    checkups._execution.control = control
    try:
        report = checkups.get_checkup_runner()(None, report_id='pause-budget')
    finally:
        del checkups._execution.control
    assert report.quality['provider_requests'] == 2
    assert report.quality['provider_budget_exhausted'] is False


def test_restarted_paused_snapshot_keeps_evidence_but_never_claims_resumable(pause_client):
    client, directory = pause_client
    identifier = 'paused-before-restart'
    snapshot = CheckupJob(id=identifier, status='paused', stage='poi', progress=30,
                         partial_revision=1, partial_result=_partial(identifier))
    path = directory / 'progress' / f'{identifier}_partial.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json())
    assert checkups.load_persisted_reports() == [identifier]
    restored = client.get(f'/api/v1/checkups/{identifier}').json()
    assert restored['status'] == 'failed' and restored['progress'] == 30
    assert restored['error']['code'] == 'PAUSED_TASK_INTERRUPTED'
    assert restored['partial_result']['groups'][0]['score'] == 60
    assert restored['partial_result']['active_facility_id'] is None
    assert post(client, identifier, 'resume').json() == restored
    assert checkups._executor.pending == []


def test_shutdown_releases_a_paused_worker_without_resuming_requests(pause_client):
    client, directory = pause_client
    started, next_attempt = threading.Event(), threading.Event()

    def runner(_request, *, report_id, progress):
        started.set()
        progress('poi', 30)
        next_attempt.set()
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    assert post(client, identifier, 'pause').json()['status'] == 'paused'
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert not started.wait(.03)
        checkups.stop_checkup_workers()
        worker.join(3)
        assert not worker.is_alive() and not started.is_set() and not next_attempt.is_set()
        checkpoint = directory / 'progress' / f'{identifier}_partial.json'
        assert json.loads(checkpoint.read_text())['status'] == 'paused'
    finally:
        checkups.stop_checkup_workers()
        worker.join(3)
