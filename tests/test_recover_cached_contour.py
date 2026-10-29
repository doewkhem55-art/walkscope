import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.schemas.routing import RoutingRequest


def recovery_tool():
    assert importlib.util.find_spec('tools.recover_cached_contour') is not None, 'offline recovery tool is missing'
    return importlib.import_module('tools.recover_cached_contour')


def test_cached_directions_use_real_forward_six_decimal_request_and_leave_misses_unknown(tmp_path):
    # Literal identity matches the provider request, excluding only the AK.
    canonical = ('/directionlite/v1/walking?origin=31.200000%2C121.500000'
                 '&destination=31.210000%2C121.510000&coord_type=bd09ll&output=json')
    payload = dict(status=0, result=dict(routes=[dict(distance=800, duration=600)]))
    filename = hashlib.sha256(canonical.encode()).hexdigest() + '.json'
    (tmp_path / filename).write_text(json.dumps(payload))
    request = RoutingRequest.model_validate(dict(schema_version='1.0', travel_mode='walking',
        origin=dict(lng=121.5, lat=31.2, crs='BD09LL'), targets=[
            dict(target_id='hit', location=dict(lng=121.51, lat=31.21, crs='BD09LL')),
            dict(target_id='missing', location=dict(lng=121.52, lat=31.22, crs='BD09LL'))]))
    cache = recovery_tool().CachedDirections([tmp_path])
    routes = cache(request).root.data.routes
    assert (routes[0].status.value, routes[0].distance_m, routes[0].duration_s) == ('success', 800, 600)
    assert routes[1].status.value == 'unavailable' and routes[1].distance_m is None
    assert cache.hits == 1 and cache.misses == 1
    (tmp_path / filename).write_text(json.dumps(dict(status=302)))
    assert cache(request).root.data.routes[0].status.value == 'unavailable'


def test_offline_recovery_keeps_source_accounting_and_refuses_saved_output(tmp_path, monkeypatch):
    from app.schemas.checkup import CheckupJob
    from tools.prepare_actual_group import prepare_group
    root = Path(__file__).resolve().parents[1]
    original = json.loads((root / 'data/reports/3de7777d550f4562afc38d96140f0118_report.json').read_text())
    current = CheckupJob.model_validate_json((root / 'data/reports/tongji-education-health-walk-contour-20261025_report.json').read_text())
    source = prepare_group(original, current.result.isochrone, 'community_safety', 'recovery-source')
    source_path, ledger_path, output = [tmp_path / name for name in ('source.json', 'ledger.json', 'recovered.json')]
    source_path.write_text(source.model_dump_json())
    ledger = dict(requests=450, status_counts={'0': 448, '302': 2}, max_requests=450,
                  max_seconds=1200, elapsed=675, attempts=[])
    ledger_path.write_text(json.dumps(ledger))
    before = source_path.read_bytes(), ledger_path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('offline reconstruction must never access the network')
    monkeypatch.setattr('urllib.request.urlopen', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    tool = recovery_tool()
    job = tool.recover_cached_contour(source_path, ledger_path, 'offline-recovered', output,
                                      [tmp_path / 'empty-cache'], max_route_pairs=25)
    quality = job.result.quality
    assert output.exists() and job.id == 'offline-recovered'
    assert quality['provider_requests'] == 450 and quality['new_http_requests'] == 0
    assert quality['reconstruction_new_http_requests'] == 0 and quality['cache_reconstruction'] is True
    assert quality['source_accounting']['status_counts'] == {'0': 448, '302': 2}
    assert quality['coverage_route_pairs'] == 0 and quality['cache_lookup_pairs'] == 25
    assert job.result.blindspot['total_coverage']['covered_ratio'] == 0
    assert quality['coverage_distance_sample_count'] == 0
    assert (source_path.read_bytes(), ledger_path.read_bytes()) == before
    saved = output.read_bytes()
    with pytest.raises(FileExistsError):
        tool.recover_cached_contour(source_path, ledger_path, 'offline-recovered', output, [tmp_path])
    assert output.read_bytes() == saved
