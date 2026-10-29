"""Cancellation is terminal and prevents late worker responses from reviving a job."""
import io
import json
import threading

import pytest

from app.api import checkups
from app.api.main import app
from app.modules.checkup.control import CheckupCancelled, CheckupControl
from app.modules.checkup.service import CheckupError
from app.providers.baidu.poi import BaiduPOIProvider
from tests.test_checkup_api import _CHECKUP_REQUEST, _fake_runner, _report
from tests.test_checkup_progress import _partial, progress_client
from tests.test_checkup_pause import pause_client, post


def test_cancel_queued_skips_runner_and_allows_a_new_task(pause_client):
    client, _ = pause_client
    invocations = []

    def runner(*args, **kwargs):
        invocations.append(kwargs['report_id'])
        return _fake_runner(*args, **kwargs)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    cancelled = post(client, identifier, 'cancel').json()
    assert (cancelled['status'], cancelled['stage'], cancelled['progress']) == ('cancelled', 'cancelled', 0)
    assert cancelled['error'] is None and cancelled['result'] is None
    assert post(client, identifier, 'cancel').json() == cancelled
    assert post(client, identifier, 'resume').json() == cancelled
    assert post(client, identifier, 'pause').json() == cancelled
    next_response = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST)
    assert next_response.status_code == 202
    checkups._executor.run_next()  # The cancelled queued callback exits before runner/transport.
    assert invocations == []
    checkups._executor.run_next()
    assert invocations == [next_response.json()['id']]
    assert post(client, next_response.json()['id'], 'cancel').json()['status'] == 'completed'


@pytest.mark.parametrize('late_failure', [False, True])
def test_cancel_inflight_retains_partial_and_late_completion_cannot_overwrite(pause_client, monkeypatch, late_failure):
    client, directory = pause_client
    inflight, release = threading.Event(), threading.Event()
    requests = []

    def fake_urlopen(url, timeout):
        requests.append(url)
        inflight.set()
        assert release.wait(3)
        return io.StringIO('{}')

    monkeypatch.setattr(checkups, 'urlopen', fake_urlopen)
    monkeypatch.setattr(checkups.time, 'sleep', lambda _: None)

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        progress.publish(_partial(report_id))
        checkups._get_json('https://mock.invalid/inflight', 1)
        if late_failure:
            raise CheckupError('LATE_PROVIDER_FAILURE', 'mock failure', False)
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert inflight.wait(3)
        cancelled = post(client, identifier, 'cancel').json()
        assert cancelled['status'] == 'cancelled' and cancelled['progress'] == 30
        assert cancelled['partial_result']['groups'][0]['score'] == 60
        assert cancelled['partial_result']['groups'][0]['status'] == 'partial'
        assert cancelled['partial_result']['active_facility_id'] is None
        assert client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).status_code == 202
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert requests == ['https://mock.invalid/inflight']
    assert client.get(f'/api/v1/checkups/{identifier}').json() == cancelled
    checkups._publish_terminal_job(identifier, result=_report(identifier))
    path = directory / 'reports' / f'{identifier}_report.json'
    assert json.loads(path.read_text()) == cancelled, 'late success/failure must not overwrite cancellation on disk'
    checkups._jobs.clear()
    assert client.get(f'/api/v1/checkups/{identifier}').json() == cancelled
    assert post(client, identifier, 'resume').json() == cancelled


def test_cancel_wakes_a_paused_worker_without_sending_the_next_http(pause_client, monkeypatch):
    client, _ = pause_client
    published, proceed = threading.Event(), threading.Event()
    requests = []
    monkeypatch.setattr(checkups, 'urlopen', lambda *args, **kwargs: requests.append(args))

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        progress.publish(_partial(report_id))
        published.set()
        assert proceed.wait(3)
        checkups._get_json('https://mock.invalid/must-not-send', 1)
        raise AssertionError('cancelled HTTP boundary must abort the worker')

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert published.wait(3)
        assert post(client, identifier, 'pause').json()['status'] == 'paused'
        proceed.set()
        assert post(client, identifier, 'cancel').json()['status'] == 'cancelled'
    finally:
        proceed.set()
        post(client, identifier, 'cancel')
        worker.join(3)
    assert not worker.is_alive() and requests == []
    assert post(client, identifier, 'resume').json()['status'] == 'cancelled'


def test_cancel_signal_bypasses_provider_generic_error_recovery():
    control = CheckupControl()
    control.cancel()

    def transport(*_, **kwargs):
        control.checkpoint()

    provider = BaiduPOIProvider(ak='offline-test-only', http_get=transport)
    with pytest.raises(CheckupCancelled):
        provider.search('mock keyword', _report('point').center, 1000)


def test_cancel_rejects_untrusted_origin_and_unknown_id(pause_client):
    client, _ = pause_client
    app.dependency_overrides[checkups.get_checkup_runner] = lambda: _fake_runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    assert post(client, identifier, 'cancel', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert post(client, 'unknown-id', 'cancel').status_code == 404
    assert client.get(f'/api/v1/checkups/{identifier}').json()['status'] == 'queued'


def test_cancelled_contract_rejects_a_fake_report_or_failure_state():
    from pydantic import ValidationError
    from app.schemas.checkup import CheckupJob
    with pytest.raises(ValidationError):
        CheckupJob(id='cancelled', status='cancelled', stage='report', progress=30)
    with pytest.raises(ValidationError):
        CheckupJob(id='cancelled', status='cancelled', stage='cancelled', progress=30,
                   result=_report('cancelled'))
