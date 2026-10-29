from copy import deepcopy

from tests.report_fixtures import build_report


def test_recompute_case_preserves_source_and_counts_but_replaces_coverage():
    from tools.recompute_case import recompute_saved_case

    source = dict(id='original', status='completed', stage='completed', progress=100,
                  result=build_report('original').model_dump(mode='json'), error=None)
    source['result']['quality']['grid_size_m'] = 200
    source['result']['quality']['provider_requests'] = 17
    source['result']['quality']['stage_timings_s'] = {'coverage': 4.5, 'total': 9.0}
    source['result']['warnings'].append({'code': 'APPROXIMATE_BOUNDARIES', 'message': '盲区为200米网格'})
    original = deepcopy(source)
    def no_routes(_):
        raise AssertionError('zero route budget must not call the provider')
    result = recompute_saved_case(source, no_routes, report_id='fine-grid', max_route_pairs=0)
    assert source == original
    assert result['id'] == result['result']['report_id'] == 'fine-grid'
    report = result['result']
    assert report['quality']['grid_size_m'] == 20
    assert report['quality']['grid_cell_count'] > 1000
    assert report['quality']['coverage_recomputed_from'] == 'original'
    assert report['quality']['coverage_route_pairs'] == 0
    assert report['quality']['source_report_quality']['provider_requests'] == 17
    assert 'provider_requests' not in report['quality']
    assert 'stage_timings_s' not in report['quality']
    assert not any('盲区为200米网格' in w['message'] for w in report['warnings'])
    assert report['quality']['coverage_origin_grid_m'] is None
    assert report['quality']['coverage_origin_mode'] == 'actual_poi'
    assert report['score'] == original['result']['score']
    assert report['pois'] == original['result']['pois']
    for old, new in zip(original['result']['categories'], report['categories']):
        assert (new['count'], new['score']) == (old['count'], old['score'])
        assert abs(new['covered_ratio'] + new['blind_ratio'] + new['unknown_ratio'] - 1) < 1e-9
    assert report['blindspot']['unknown_areas']['features']
