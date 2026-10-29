"""Behaviour checks for shared origins, query avoidance and adaptive boundaries."""
import math

import pytest
from shapely.geometry import Polygon, box

from app.errors import ProviderStopError
from app.modules.blindspot.service import _grid_cells
from app.modules.checkup.representative_service_areas import analyze_representative_service_areas, build_origins
from test_poi_service_areas import Oracle, category, poi, spec, to_center, to_m


def analyze(rows, route=None, *, domain=None, budget=5000):
    domain = box(-100,-100,100,100) if domain is None else domain
    warnings = []
    result = analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        rows,{'facilities':[spec(r['facility_id']) for r in rows]},route or Oracle(),warnings,
        max_route_pairs=budget)
    return (*result,warnings)


def test_same_grid_uses_one_origin_and_shares_real_measurements_across_categories():
    route = Oracle(lambda *_: ('success',100,8000))
    ratios,layers,q,_ = analyze([category('a',[poi('one',-30),poi('two',30)]),category('b',[poi('three',50)])],route)
    assert q['coverage_poi_origin_count'] == 1
    assert q['coverage_source_poi_count'] == 3
    assert q['coverage_shared_category_uses'] == 2
    assert q['coverage_route_pairs'] == 13  # Four coarse tiles, shared vertices/centres.
    assert all(r['covered_ratio'] == 1 for r in ratios.values())
    assert layers['total_coverage']['unknown_ratio'] == 0
    assert len({(r.origin.lng,r.origin.lat) for r in route.requests}) == 1
    assert all(r.origin == to_center(0,0) for r in route.requests)


def test_half_open_cell_edges_and_inside_only_membership_are_deterministic():
    rows=[category('a',[poi('left',249,0),poi('right',250,0),poi('outside',1001,0)])]
    origins,unique,outside=build_origins(box(-500,-500,1000,500),rows,{'a':spec('a')},to_m)
    assert len(origins)==2 and unique==2 and outside==1
    assert [o['xy'] for o in origins] == [(0,0),(500,0)]


def test_uniform_fields_stop_early_and_boundary_fields_refine_with_no_duplicate_queries():
    domain=box(-600,-400,600,400)
    flat=Oracle(lambda *_: ('success',100,1))
    _,_,q_flat,_=analyze([category('a',[poi('one',0,0)])],flat,domain=domain)
    boundary=Oracle(lambda origin,target: ('success',round(1000+to_m(target.location.lng,target.location.lat)[0]),1))
    ratios,_,q,_=analyze([category('a',[poi('one',0,0)])],boundary,domain=domain)
    assert q['coverage_refined_tiles']>0
    assert q['coverage_route_pairs']>q_flat['coverage_route_pairs']
    assert ratios['a']['covered_ratio']==pytest.approx(.5,abs=.02)
    assert ratios['a']['unknown_ratio']==0
    pairs=[(r.origin.lng,r.origin.lat,t.location.lng,t.location.lat) for r in boundary.requests for t in r.targets]
    assert len(pairs)==len(set(pairs))


def test_already_covered_category_cells_skip_later_origin_queries():
    domain=box(-600,-100,600,100)
    route=Oracle(lambda *_: ('success',100,1))
    _,_,q,_=analyze([category('a',[poi('one',0,0),poi('two',500,0)])],route,domain=domain)
    assert q['coverage_poi_origin_count']==2
    assert q['coverage_skipped_tiles_or_cells']>0
    # Fair scheduling gives both origins a turn, then skips work made redundant
    # by the first origin's completed tiles instead of finishing both fields.
    independent = sum(analyze([category('a',[point])], Oracle(lambda *_: ('success',100,1)),
        domain=domain)[2]['coverage_route_pairs'] for point in [poi('one',0,0),poi('two',500,0)])
    assert q['coverage_route_pairs'] < independent


@pytest.mark.parametrize('status',['unavailable','no_route'])
def test_failed_samples_do_not_prove_unsampled_interiors_blind(status):
    ratios,_,q,_=analyze([category('a',[poi('one')])],Oracle(lambda *_:(status,None,None)))
    assert ratios['a']['unknown_ratio']==1 and ratios['a']['blind_ratio']==0
    assert q['coverage_route_unavailable']+q['coverage_route_no_route']>0


def test_exact_1000_m_is_covered_even_when_duration_is_long():
    ratios,_,_,_=analyze([category('a',[poi('one')])],Oracle(lambda *_:('success',1000,90000)))
    assert ratios['a']['covered_ratio']==1
    ratios,_,_,_=analyze([category('a',[poi('one')])],Oracle(lambda *_:('success',1001,1)))
    assert ratios['a']['blind_ratio']==1


def test_stop_and_zero_budget_leave_unknown_and_make_no_later_requests():
    calls=[]
    def stop(request):
        calls.append(request)
        raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED','stop')
    rows=[category('a',[poi('one',0,0),poi('two',500,0)])]
    ratios,_,q,_=analyze(rows,stop,domain=box(-600,-100,600,100))
    assert len(calls)==1 and q['coverage_stop_reason']=='PROVIDER_BUDGET_EXHAUSTED'
    assert ratios['a']['unknown_ratio']==1
    def forbidden(_): raise AssertionError('zero budget')
    ratios,_,q,_=analyze(rows,forbidden,budget=0)
    assert ratios['a']['unknown_ratio']==1 and q['coverage_route_pairs']==0


def test_complete_absence_is_blind_and_partial_absence_is_unknown():
    ratios,_,_,_=analyze([category('a'),category('b',status='partial')])
    assert ratios['a']['blind_ratio']==1
    assert ratios['b']['unknown_ratio']==1


def test_geometry_holes_remain_excluded_and_aggregate_ratios_sum_to_one():
    domain=Polygon([(-100,-100),(100,-100),(100,100),(-100,100)],holes=[[(-20,-20),(20,-20),(20,20),(-20,20)]])
    rows=[category('a',[poi('one',80,0)]),category('b',status='partial')]
    _,layers,_,_=analyze(rows,Oracle(lambda *_:('success',1200,1)),domain=domain)
    for row in [*layers['group_coverage'].values(),layers['total_coverage']]:
        assert sum(row[k] for k in ['covered_ratio','blind_ratio','unknown_ratio'])==pytest.approx(1)
        assert row['unknown_ratio']==1


def test_live_runtime_uses_100m_isochrone_and_has_room_for_1961_samples():
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.modules.isochrone.grid import generate_grid_isochrone
    from app.schemas.isochrone import IsochroneRequest
    with TestClient(app) as client:
        runtime = client.get('/api/v1/checkup-catalog').json()['runtime']
    assert runtime['isochrone_grid_cell_m'] == 100
    route = Oracle(lambda *_: ('success',100,100))
    result = generate_grid_isochrone(route, IsochroneRequest(schema_version='1.0',center=to_center(0,0),time_limit_s=900),
                                    radius_m=2500, cell_m=100, max_grid_points=2500)
    assert result.root.ok
    assert result.root.data.quality.grid_cell_m == 100
    assert result.root.data.quality.routing_sample_count == 1961


def test_group_recompute_assembles_a_valid_report_atomically_without_changing_source():
    from copy import deepcopy
    from tools.recompute_representative_case import subset_report
    from tests.report_fixtures import build_report
    from app.schemas.checkup import CheckupReport
    old=build_report('source')
    source=dict(id='source',status='completed',stage='completed',progress=100,result=old.model_dump(mode='json'),error=None)
    before=deepcopy(source)
    report=subset_report(source,old.isochrone,'education_children','new')
    CheckupReport.model_validate(report.model_dump(mode='json'))
    assert source==before
    assert len(report.categories)==6 and len(report.groups)==1
    assert report.selected_facility_ids==[c.facility_id for c in report.categories]
    assert all(p.facility_id in report.selected_facility_ids for p in report.pois)


def test_matching_cross_report_samples_are_reused_without_paid_routes():
    rows=[category('a',[poi('one',0,0)])]
    domain=box(-100,-100,100,100)
    route=Oracle(lambda *_:('success',100,1))
    _,layers,_,_=analyze(rows,route,domain=domain)
    origins={f['properties']['representative_id']:dict(location=f['geometry']['coordinates'],samples={})
             for f in layers['representative_points']['features']}
    for f in layers['route_samples']['features']:
        xy=tuple(round(v,6) for v in to_m(*f['geometry']['coordinates']))
        origins[f['properties']['representative_id']]['samples'][xy]=f['properties']['distance_m']
    def forbidden(_): raise AssertionError('matching sampled routes must be reused')
    ratios,_,quality=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        rows,{'facilities':[spec('a')]},forbidden,[],sample_cache=origins,max_route_pairs=0)
    assert ratios['a']['covered_ratio']==1
    assert quality['coverage_route_pairs']==0
    assert quality['coverage_reused_distance_samples']==13
    for item in origins.values(): item['location'][0]+=.001
    other=Oracle(lambda *_:('success',100,1))
    _,_,quality=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        rows,{'facilities':[spec('a')]},other,[],sample_cache=origins)
    assert quality['coverage_route_pairs']>0 and quality['coverage_reused_distance_samples']==0


def test_tight_budget_reaches_each_origin_on_coarse_layer_before_refinement():
    domain=box(-600,-300,1100,300)
    route=Oracle(lambda *_: ('success',999,1))
    _,_,quality,_=analyze([category('a',[poi('one',0,0)]),category('b',[poi('two',500,0)])],
        route,domain=domain,budget=50)
    assert len(route.requests)==2
    assert route.requests[0].origin != route.requests[1].origin
    assert [len(request.targets) for request in route.requests]==[25,25]
    # 200 m tile corners and centres use multiples of 100 m; refinement would
    # introduce a 50 m offset before every origin had received its coarse turn.
    for request in route.requests:
        for target in request.targets:
            assert all(abs(value/100-round(value/100)) < 1e-6
                       for value in to_m(target.location.lng,target.location.lat))
    assert quality['coverage_route_pairs']==50
    assert quality['coverage_sampling_schedule']=='all_origins_coarse_then_round_robin_refinement'


def test_provider_stop_blocks_new_requests_but_fine_cache_remains_usable():
    domain=box(-100,-100,600,100)
    rows=[category('b',[poi('cached',500,0)])]
    field=Oracle(lambda origin,target: ('success',round(1000+to_m(target.location.lng,target.location.lat)[0]),1))
    ratios,layers,_,_=analyze(rows,field,domain=domain)
    cache={f['properties']['representative_id']:dict(location=f['geometry']['coordinates'],samples={})
           for f in layers['representative_points']['features']}
    for feature in layers['route_samples']['features']:
        xy=tuple(round(v,6) for v in to_m(*feature['geometry']['coordinates']))
        cache[feature['properties']['representative_id']]['samples'][xy]=feature['properties']['distance_m']
    calls=[]
    def stop(request):
        calls.append(request)
        raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED','stop')
    combined=[category('a',[poi('uncached',0,0)]),*rows]
    result,_,quality=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        combined,{'facilities':[spec('a'),spec('b')]},stop,[],sample_cache=cache)
    assert len(calls)==1
    assert quality['coverage_stop_reason']=='PROVIDER_BUDGET_EXHAUSTED'
    assert quality['coverage_reused_distance_samples']>0
    assert result['a']['unknown_ratio']==1
    assert result['b']==ratios['b']
