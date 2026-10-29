"""Process evidence is visible before the final report, without provider calls."""

import json
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import checkups
from app.api.main import app
from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import CheckupError
from app.providers.baidu.poi import POIProviderError, ProviderPOI
from app.schemas.checkup import CheckupJob
from app.schemas.isochrone import IsochroneResult
from tests import test_checkup_service
from tests.test_checkup_api import HoldingExecutor, _CHECKUP_REQUEST, _report
from tests.test_checkup_service import CENTER, _run


def test_group_scores_and_deduplicated_pois_arrive_before_other_groups_and_routes():
    catalog = load_catalog()
    definitions = {item['id']: item for item in catalog['facilities']}
    shared = ProviderPOI('shared-site', '测试服务点', CENTER.lng, CENTER.lat, '测试地址')
    responses = {definitions[fid]['search_terms'][0]: [shared]
                 for fid in ('childcare_0_3', 'kindergarten')}
    stages, snapshots = [], []

    def publish(snapshot):
        snapshots.append((stages[-1][0] if stages else 'queued', snapshot))

    report, *_ = _run(responses, on_update=publish,
                      progress=lambda stage, value: stages.append((stage, value)))
    initial = snapshots[0][1]
    assert initial['center'] is None and initial['isochrone'] is None
    assert initial['completed_categories'] == initial['complete_categories'] == 0
    assert all(g['status'] == 'pending' and g['score'] is None for g in initial['groups'])

    stage, first = next((stage, value) for stage, value in snapshots
                        if value['completed_categories'] == 1)
    education = next(g for g in first['groups'] if g['id'] == 'education_children')
    assert stage == 'poi' and education['score'] == 60
    assert education['score_provisional'] and education['status'] == 'searching'
    assert education['facility_count'] == 1 and first['pois'] and first['routes'] == []
    assert any(g['status'] == 'pending' for g in first['groups'])

    second = next(value for _, value in snapshots if value['completed_categories'] == 2)
    assert next(g for g in second['groups'] if g['id'] == 'education_children')['facility_count'] == 1
    ready = next(value for stage, value in snapshots
                 if stage == 'poi' and all(g['status'] == 'complete' for g in value['groups']))
    assert ready['complete_categories'] == 45 and ready['routes'] == []
    assert {g['id']: g['score'] for g in ready['groups']} == {g.id: g.score for g in report.groups}
    assert all(not g['score_provisional'] for g in ready['groups'])
    # Callback references keep their own earlier stage instead of becoming the
    # final snapshot after run_checkup mutates its internal working object.
    assert initial['center'] is None and initial['pois'] == []
    assert first['completed_categories'] == 1


def test_quota_skip_remains_pending_and_never_becomes_a_zero_score():
    first = load_catalog()['facilities'][0]
    snapshots = []
    report, provider, *_ = _run({first['search_terms'][0]: POIProviderError(
        'DAILY_QUOTA_EXCEEDED', 'test quota stop', False)}, on_update=snapshots.append)
    latest = snapshots[-1]
    assert len(provider.calls) == 1
    assert latest['completed_categories'] == 1 and latest['complete_categories'] == 0
    assert latest['categories'][0]['search_status'] == 'failed'
    assert all(c['search_status'] == 'pending' for c in latest['categories'][1:])
    assert all(c['score'] is None for c in latest['categories'])
    assert latest['groups'][0]['status'] == 'partial'
    assert all(g['status'] == 'pending' for g in latest['groups'][1:])
    assert report.score is None and all(c.score is None for c in report.categories)


def test_mid_category_budget_stop_publishes_known_points_and_score_before_raising():
    first = load_catalog()['facilities'][0]
    snapshots, attempted = [], []
    stop = ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', 'budget stop')
    point = ProviderPOI('known-before-stop', '已知服务点', CENTER.lng, CENTER.lat, '测试地址')

    class StoppingResponses(dict):
        def get(self, keyword, default=None):
            attempted.append(keyword)
            return super().get(keyword, default)

    with pytest.raises(ProviderStopError) as caught:
        _run(StoppingResponses({first['search_terms'][0]: [point],
                               first['search_terms'][1]: stop}), on_update=snapshots.append)
    assert caught.value is stop
    assert attempted == first['search_terms'][:2]
    latest = snapshots[-1]
    category = latest['categories'][0]
    assert category['search_status'] == 'partial' and category['known_count'] == 1
    assert category['score'] == 60 and category['score_provisional']
    assert latest['groups'][0]['facility_count'] == 1
    assert latest['completed_categories'] == 1 and latest['complete_categories'] == 0
    assert [p['poi_id'] for p in latest['pois']] == ['baidu:known-before-stop']
    assert all(c['search_status'] == 'pending' for c in latest['categories'][1:])


def test_selected_scope_and_real_coverage_counts_keep_progress_monotonic(monkeypatch):
    monkeypatch.setattr(test_checkup_service, '_small_isochrone',
                        lambda: IsochroneResult.model_validate(test_checkup_service.ISOCHRONE_FIXTURE))
    snapshots, updates = [], []
    report, *_ = _run({}, facility_ids=['primary_school'], on_update=snapshots.append,
                      progress=lambda stage, value: updates.append((stage, value)))
    assert len(snapshots[-1]['groups']) == 1
    group = snapshots[-1]['groups'][0]
    assert group['status'] == 'complete' and group['score'] == 0
    assert group['completed_categories'] == group['complete_categories'] == group['total_categories'] == 1
    values = [value for _, value in updates]
    assert values == sorted(values)
    coverage_values = [value for stage, value in updates if stage == 'coverage']
    assert coverage_values[0] == 65 and coverage_values[-1] == 93
    assert len(coverage_values) > 2 and len(set(coverage_values)) > 2
    assert values[-1] == 94 and report.selected_facility_ids == ['primary_school']


@pytest.fixture
def progress_client(tmp_path, monkeypatch):
    monkeypatch.setenv('CHECKUP_REPORT_DIR', str(tmp_path / 'reports'))
    monkeypatch.delenv('CHECKUP_REPLAY_DIR', raising=False)
    monkeypatch.delenv('CHECKUP_REPLAY_REPORTS', raising=False)
    monkeypatch.setattr(checkups, '_jobs', {})
    monkeypatch.setattr(checkups, '_questions_running', set())
    monkeypatch.setattr(checkups, '_lock', threading.Lock())
    monkeypatch.setattr(checkups, '_executor', HoldingExecutor())
    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    try:
        with TestClient(app) as client:
            yield client, tmp_path
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


def _partial(report_id):
    return dict(report_id=report_id, center=None, location_label='', isochrone=None,
                categories=[], groups=[dict(id='education_children', score=60, status='searching')],
                pois=[], routes=[], completed_categories=1, complete_categories=1,
                total_categories=45, active_facility_id='kindergarten')


def test_incremental_poll_and_refresh_restore_the_same_running_snapshot(progress_client):
    client, directory = progress_client
    published, resume = threading.Event(), threading.Event()

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        snapshot = _partial(report_id)
        progress.publish(snapshot)
        snapshot['groups'][0]['score'] = 999  # publisher must retain its own copy
        published.set()
        assert resume.wait(3)
        return _report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    accepted = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()
    identifier = accepted['id']
    worker = threading.Thread(target=checkups._executor.run_next)
    worker.start()
    try:
        assert published.wait(3)
        status = client.get(f'/api/v1/checkups/{identifier}').json()
        assert status['status'] == 'running' and status['result'] is None
        assert status['partial_revision'] == 1 and status['partial_result']['groups'][0]['score'] == 60
        checkpoint = directory / 'progress' / f'{identifier}_partial.json'
        saved = json.loads(checkpoint.read_text())
        assert saved['partial_result']['report_id'] == identifier and saved['result'] is None
        assert not (directory / 'reports' / f'{identifier}_report.json').exists()
        unchanged = client.get(f'/api/v1/checkups/{identifier}?after_revision=1').json()
        assert unchanged['partial_revision'] == 1 and unchanged['partial_result'] is None
        refreshed = client.get(f'/api/v1/checkups/{identifier}').json()
        assert refreshed['partial_result'] == status['partial_result']
        assert checkups.load_persisted_reports() == []
    finally:
        resume.set()
        worker.join(3)
    assert not worker.is_alive()
    final = client.get(f'/api/v1/checkups/{identifier}?after_revision=1').json()
    assert final['status'] == 'completed' and final['progress'] == 100
    assert final['result']['report_id'] == identifier and final['partial_result'] is None
    assert (directory / 'reports' / f'{identifier}_report.json').is_file()


def test_failed_analysis_retains_partial_evidence_without_an_active_spinner(progress_client):
    client, _ = progress_client

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        progress.publish(_partial(report_id))
        raise CheckupError('TEST_INTERRUPTION', 'Test stopped', False)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    checkups._executor.run_next()
    result = client.get(f'/api/v1/checkups/{identifier}').json()
    assert result['status'] == 'failed' and result['result'] is None
    assert result['partial_result']['active_facility_id'] is None
    assert result['partial_result']['groups'][0]['status'] == 'partial'
    assert result['partial_result']['groups'][0]['score'] == 60
    checkups._jobs.clear()
    restored = client.get(f'/api/v1/checkups/{identifier}').json()
    assert restored == result


def test_wrong_task_process_evidence_is_rejected(progress_client):
    client, _ = progress_client

    def runner(_request, *, report_id, progress):
        progress('poi', 30)
        progress.publish(_partial('another-task'))
        raise AssertionError('must reject before continuing')

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    identifier = client.post('/api/v1/checkups', json=_CHECKUP_REQUEST).json()['id']
    checkups._executor.run_next()
    result = client.get(f'/api/v1/checkups/{identifier}').json()
    assert result['status'] == 'failed' and result['partial_result'] is None
    assert result['error']['code'] == 'PROGRESS_IDENTITY_MISMATCH'


def test_older_jobs_remain_compatible_and_progress_identity_is_validated():
    old = dict(id='older-task', status='queued', stage='queued', progress=0)
    job = CheckupJob.model_validate(old)
    assert job.partial_result is None and job.partial_revision == 0
    with pytest.raises(ValidationError):
        CheckupJob.model_validate({**old, 'partial_revision': 1, 'partial_result': _partial('wrong-id')})
    with pytest.raises(ValidationError):
        CheckupJob.model_validate({**old, 'status': 'completed', 'partial_revision': 1,
                                  'partial_result': _partial('older-task')})
