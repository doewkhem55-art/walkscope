from copy import deepcopy

from fastapi.testclient import TestClient

from app.api.main import app
from tests.report_fixtures import build_report
from tools.recompute_case import recompute_saved_case


def test_catalog_declares_outward_poi_service_areas_for_new_analyses():
    with TestClient(app) as client:
        runtime = client.get('/api/v1/checkup-catalog').json()['runtime']
    assert runtime['coverage_method'] == 'actual_poi_origins_measured_contour_1km_service_area_v2'
    assert runtime['coverage_route_direction'] == 'poi_to_sample'
    assert runtime['coverage_origin_mode'] == 'actual_poi'
    assert runtime['coverage_grid_size_m'] == 20


def test_recompute_keeps_service_area_provenance_and_original_facts():
    source = dict(id='original', status='completed', stage='completed', progress=100,
                  result=build_report('original').model_dump(mode='json'), error=None)
    source['result']['quality']['coverage_method'] = 'grid_representative_walking_distance'
    original = deepcopy(source)
    def forbidden(_):
        raise AssertionError('zero budget must not make a request')
    updated = recompute_saved_case(source, forbidden, report_id='service-area-version', max_route_pairs=0)
    report = updated['result']
    assert report['quality']['coverage_method'] == 'actual_poi_origins_measured_contour_1km_service_area_v2'
    assert report['quality']['coverage_route_direction'] == 'poi_to_sample'
    assert 'poi_service_areas' in report['blindspot']
    aggregate = report['blindspot']['total_coverage']
    assert aggregate['calculation_method'] == 'actual_poi_origins_measured_contour_1km_service_area_v2'
    assert 'covered_areas' in aggregate
    assert source == original
    assert report['pois'] == original['result']['pois']
    assert report['score'] == original['result']['score']
    assert report['quality']['source_report_quality']['coverage_method'] == 'grid_representative_walking_distance'


def test_new_report_explains_20m_poi_service_area_precision():
    from tests.test_checkup_service import _run

    report, *_ = _run({}, facility_ids=['childcare_0_3'])
    warning = next(w.message for w in report.warnings if w.code == 'APPROXIMATE_BOUNDARIES')
    assert '1000米等距边界' in warning
    assert '最终边界不由格子拼接' in warning
    assert '设施出发' in warning
    assert '200 米' not in warning
    assert report.quality['coverage_method'] == 'actual_poi_origins_measured_contour_1km_service_area_v2'


def test_recompute_does_not_reuse_an_earlier_runs_provider_costs():
    source = dict(id='earlier', status='completed', stage='completed', progress=100,
                  result=build_report('earlier').model_dump(mode='json'), error=None)
    old_costs = dict(coverage_provider_requests=123, coverage_cached_requests=45,
                     coverage_elapsed_seconds=67, coverage_request_limit=450)
    source['result']['quality'].update(old_costs, coverage_route_budget=5000)
    def forbidden(_):
        raise AssertionError('zero budget must not make a request')
    updated = recompute_saved_case(source, forbidden, report_id='later', max_route_pairs=0)
    quality = updated['result']['quality']
    assert not old_costs.keys() & quality.keys()
    assert quality['coverage_route_budget'] == 0
    assert 'provider_requests' not in quality
    assert 'provider_cache_hits' not in quality
    for key, value in old_costs.items():
        assert quality['source_report_quality'][key] == value
