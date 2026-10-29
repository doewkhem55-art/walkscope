from copy import deepcopy
import json

import pytest

from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD
from app.schemas.checkup import CheckupJob
from app.schemas.common import CenterPoint
from app.schemas.isochrone import IsochroneData
from tests.report_fixtures import build_report
from tools.recompute_actual_poi_case import load_actual_sample_cache, main, recompute_actual_case


def saved_case():
    report = build_report('source')
    categories = [c for c in report.categories if c.group_id in {'education_children', 'health_medical'}]
    report = report.model_copy(update=dict(categories=categories,
        groups=[g for g in report.groups if g.id in {'education_children', 'health_medical'}],
        selected_facility_ids=[c.facility_id for c in categories]))
    report.score = 57.5
    center = report.center
    report.isochrone = IsochroneData(center=center, time_limit_s=900, algorithm_version='grid-test',
        geometry=dict(type='Polygon', coordinates=[[[center.lng-.0002,center.lat-.0002],
            [center.lng+.0002,center.lat-.0002],[center.lng+.0002,center.lat+.0002],
            [center.lng-.0002,center.lat+.0002],[center.lng-.0002,center.lat-.0002]]]),
        quality=dict(routing_sample_count=1,routing_success_count=1,sample_success_rate=1,
                     confidence='high',grid_cell_m=100))
    report.pois[0].location = center.model_copy()
    report.pois[1].location = CenterPoint(lng=center.lng+.003,lat=center.lat,crs='BD09LL')
    return CheckupJob(id=report.report_id,status='completed',stage='completed',progress=100,result=report)


def test_offline_recompute_preserves_source_scoring_and_uses_actual_inside_and_outside_origins():
    source = saved_case().model_dump(mode='json')
    before = deepcopy(source)
    def forbidden(_):
        raise AssertionError('zero route budget must not call a provider')
    result = recompute_actual_case(source,forbidden,report_id='actual',max_route_pairs=0)
    report = result.result
    assert source == before
    assert report.score == 57.5
    assert report.isochrone.model_dump(mode='json') == before['result']['isochrone']
    assert report.pois == CheckupJob.model_validate(source).result.pois
    assert report.groups == CheckupJob.model_validate(source).result.groups
    assert report.quality['coverage_method'] == ACTUAL_POI_METHOD
    assert report.quality['coverage_origin_mode'] == 'actual_poi'
    assert report.quality['coverage_poi_origin_count'] == 2
    assert report.quality['coverage_outside_poi_origins_included'] == 1
    assert report.quality['new_http_requests'] == 0
    assert report.quality['new_center_route_requests'] == 0
    assert report.quality['new_poi_search_requests'] == 0
    assert report.categories[0].unknown_ratio == 1
    assert all(abs(c.covered_ratio+c.blind_ratio+c.unknown_ratio-1)<1e-8 for c in report.categories)
    CheckupJob.model_validate_json(result.model_dump_json())


def test_cache_binds_exact_poi_and_provider_origin_and_ignores_invalid_distances(tmp_path):
    report = saved_case().result
    poi = report.pois[0]
    row = dict(poi_id=poi.poi_id, origin_location=poi.location.model_dump(mode='json'),
        target_location=report.center.model_dump(mode='json'),distance_m=10,status='success')
    rows = [row]
    for distance in [None,-1,float('inf'),float('nan'),True]:
        rows.append(dict(row,distance_m=distance))
    rows.append(dict(row,origin_location=poi.location.model_dump(mode='json') | {'lng':poi.location.lng+.0001}))
    rows.append(dict(row,source_origin_location=poi.location.model_dump(mode='json') | {'lng':poi.location.lng+.0001}))
    path = tmp_path/'samples.json'
    path.write_text(json.dumps(dict(meta=dict(provider_origin_precision=6),records=rows)))
    cache,info = load_actual_sample_cache(path,report)
    assert info['available_samples'] == info['available_origins'] == 1
    assert next(iter(cache.values()))['samples'] == {(0.0,0.0):10.0}
    rows.append(dict(row,distance_m=11))
    path.write_text(json.dumps(dict(meta=dict(provider_origin_precision=6),records=rows)))
    with pytest.raises(ValueError,match='Conflicting'):
        load_actual_sample_cache(path,report)


def test_cli_requires_explicit_live_before_configuration_or_calls():
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


def test_offline_cli_rebuilds_contours_from_saved_actual_samples_without_keys_or_network(tmp_path, monkeypatch):
    import tools.recompute_actual_poi_case as tool
    from test_poi_service_areas import Oracle
    measured = recompute_actual_case(saved_case().model_dump(mode='json'),
        Oracle(lambda *_: ('success', 100, 1)), report_id='measured')
    reports = tmp_path/'data/reports'
    reports.mkdir(parents=True)
    source_path = reports/'measured_report.json'
    source_path.write_text(measured.model_dump_json())
    original = source_path.read_bytes()
    monkeypatch.setattr(tool, 'ROOT', tmp_path)
    monkeypatch.delenv('BAIDU_MAP_AK', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline mode must never access a provider')
    monkeypatch.setattr(tool, 'urlopen', forbidden)
    assert main(['--offline', '--source', 'measured', '--id', 'continuous']) == 0
    result = CheckupJob.model_validate_json((reports/'continuous_report.json').read_text()).result
    assert source_path.read_bytes() == original
    assert result.quality['new_http_requests'] == 0
    assert result.quality['coverage_geometry_grid_m'] is None
    assert result.quality['coverage_geometry_method'] == 'measured_distance_triangles_1000m_isoline_v1'
    assert result.score == measured.result.score
    assert result.isochrone == measured.result.isochrone
