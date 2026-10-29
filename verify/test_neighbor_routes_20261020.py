"""Offline controls for the one-shot A->B evidence harness; no real HTTP calls."""
import importlib.util
from io import StringIO
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('neighbor_probe', Path(__file__).with_name('neighbor_routes_20261020.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_step_paths_preserve_junctions_and_refuse_missing_or_discontinuous_geometry():
    center = [121.5, 31.2]
    route = {'distance': 200, 'duration': 170, 'steps': [
        {'path': '121.5,31.2;121.501,31.2'}, {'path': '121.501,31.2;121.502,31.2'}]}
    payload = {'status': 0, 'result': {'routes': [route]}}
    normalized = probe.normalized_direction(payload, center)
    assert normalized['polyline'] == [[121.5, 31.2], [121.501, 31.2], [121.502, 31.2]]
    route['steps'][1]['path'] = '121.5012,31.2;121.502,31.2'
    normalized = probe.normalized_direction(payload, center)
    assert normalized['polyline'] is None and normalized['max_step_junction_gap_m'] > 1
    for path in ['', 'invalid', 'nan,31.2']:
        route['steps'] = [{'path': path}]
        with pytest.raises(ValueError):
            probe.normalized_direction(payload, center)


def test_candidates_are_diverse_exact_measured_neighbors_and_empty_field_stays_empty():
    size, cell, origin = 31, 100, -1500
    field = SimpleNamespace(rows=size, cols=size, cell_m=cell, origin_x_m=origin, origin_y_m=origin,
        times_s=[[math.hypot(origin + c * cell, origin + r * cell) * 0.8 for c in range(size)] for r in range(size)])
    report = SimpleNamespace(center=SimpleNamespace(lng=121.5, lat=31.2), isochrone=SimpleNamespace(time_field=field))
    selected, _ = probe.select_candidates(report)
    assert len(selected) == 10 and {c['tier_s'] for c in selected} == {300, 600, 900}
    for item in selected:
        a, b = item['a_grid'], item['b_grid']
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        assert item['center_to_a_duration_s'] == field.times_s[a[0]][a[1]]
        assert item['measured_center_to_b_duration_s'] == field.times_s[b[0]][b[1]]
        band = probe.EdgeThresholds().time_band_s(item['tier_s'])
        assert abs(item['center_to_a_duration_s'] - item['tier_s']) <= band
        assert abs(item['measured_center_to_b_duration_s'] - item['tier_s']) <= band
    field.times_s = [[None] * size for _ in range(size)]
    assert probe.select_candidates(report)[0] == []


def record(attempts=0):
    candidate = {'candidate_id': 'test-1', 'tier_s': 300, 'a_grid': [0, 0], 'b_grid': [0, 1],
                 'a': [121.501, 31.2], 'b': [121.502, 31.2],
                 'center_to_a_duration_s': 300, 'measured_center_to_b_duration_s': 320}
    return {'mode': 'execute', 'report_id': 'offline-control', 'center': [121.5, 31.2],
            'attempts_used': attempts, 'results': [{'candidate': dict(candidate), 'outcome': 'not_requested'},
                {'candidate': {**candidate, 'candidate_id': 'test-2'}, 'outcome': 'not_requested'}]}


def test_quota_denial_stops_once_and_uncertain_attempt_is_saved_before_network(tmp_path, monkeypatch):
    calls = []
    def response(*args, **kwargs):
        saved = json.loads((tmp_path / 'execution.json').read_text())
        assert saved['attempts_used'] == 1 and saved['results'][0]['outcome'] == 'attempting'
        calls.append(1)
        return StringIO(json.dumps({'status': 302, 'message': 'raw-secret-body-must-not-be-saved'}))
    monkeypatch.setattr(probe, 'http_open', response)
    monkeypatch.setattr(probe.time, 'sleep', lambda value: None)
    data = record()
    probe.execute(data, tmp_path, 'fake-ak', None)
    assert len(calls) == 1 and data['attempts_used'] == 1
    assert data['stopped_reason'] == 'PROVIDER_NONZERO_STOP_NO_RETRY'
    assert data['results'][1]['outcome'] == 'not_requested'
    assert 'raw-secret-body' not in (tmp_path / 'execution.json').read_text()
    with pytest.raises(ValueError, match='Existing execution ledger'):
        probe.execute(data, tmp_path, 'fake-ak', None)
    assert len(calls) == 1


def test_budget_never_sends_attempt_41(tmp_path, monkeypatch):
    calls = []
    def response(*args, **kwargs):
        saved = json.loads((tmp_path / 'execution.json').read_text())
        assert saved['attempts_used'] == 40
        calls.append(1)
        return StringIO(json.dumps({'status': 0, 'result': {'routes': [{'distance': 100, 'duration': 50,
            'steps': [{'path': '121.501,31.2;121.502,31.2'}]}]}}))
    monkeypatch.setattr(probe, 'http_open', response)
    monkeypatch.setattr(probe, 'constraints', lambda report, tier: (None, None))
    monkeypatch.setattr(probe.time, 'sleep', lambda value: None)
    data = record(39)
    probe.execute(data, tmp_path, 'fake-ak', None)
    assert len(calls) == 1 and data['attempts_used'] == 40 and data['stopped_reason'] == 'ATTEMPT_LIMIT'
    assert data['results'][0]['production_eligible'] is False
    assert data['results'][0]['independent_obstacles_status'] == 'unknown'
