"""Regressions for the real unmeasured-inside-POI coverage failure."""
from shapely.geometry import Point, box
from app.modules.blindspot.service import _grid_cells
from app.modules.checkup.representative_service_areas import analyze_representative_service_areas, build_actual_poi_origins
from test_poi_service_areas import Oracle, category, local_region, poi, spec, to_center, to_m


def test_inside_pois_receive_nearby_measurements_before_rare_outside_categories():
    rows=[category('rare',[poi('outside',400,0)]),category('dense',[poi('inside1',0,0),poi('inside2',50,20)])]
    domain=box(-100,-100,100,100)
    origins,_,_=build_actual_poi_origins(domain,rows,{x['facility_id']:spec(x['facility_id']) for x in rows},to_m)
    assert [o['outside_domain'] for o in origins]==[False,False,True]
    route=Oracle(lambda *_:('success',100,1))
    _,layers,q=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        rows,{'facilities':[spec(r['facility_id']) for r in rows]},route,[],max_route_pairs=10,origin_mode='actual_poi')
    assert len(route.requests)==2
    assert all(domain.covers(Point(*to_m(r.origin.lng,r.origin.lat))) for r in route.requests)
    assert all(len(r.targets)==5 for r in route.requests)
    assert q['coverage_inside_origins_nearby_attempted']==2
    covered=local_region(layers['group_coverage']['education']['covered_areas'])
    assert covered.covers(Point(0,0)) and covered.covers(Point(50,20))


def test_uniform_walk_access_does_not_drop_a_real_poi_for_capacity_based_catalog_method():
    domain=box(-100,-100,100,100)
    row=category('sports',[poi('gym',31,17)])
    route=Oracle(lambda *_:('success',100,1))
    _,layers,q=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        [row],{'facilities':[spec('sports',method='insufficient_evidence')]},route,[],origin_mode='actual_poi')
    assert q['coverage_poi_origin_count']==1 and route.requests
    assert layers['group_coverage']['education']['covered_ratio']==1
    assert q['coverage_access_scope']=='all_real_pois_uniform_1000m_not_capacity_assessment'


def test_origin_evidence_states_zero_budget_honestly_and_never_creates_a_service_disk():
    domain=box(-100,-100,100,100)
    def forbidden(_): raise AssertionError('no paid route budget')
    _,layers,q=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        [category('school',[poi('inside',31,17)])],{'facilities':[spec('school')]},forbidden,[],
        max_route_pairs=0,origin_mode='actual_poi')
    props=layers['representative_points']['features'][0]['properties']
    assert props['successful_distance_samples']==0
    assert props['own_location_in_service_area'] is False
    assert q['coverage_inside_origins_unverified']==1
    assert layers['poi_service_areas']['features']==[]


def test_publication_audit_catches_the_real_three_community_pois_without_changing_geometry():
    import json
    from pathlib import Path
    from copy import deepcopy
    from app.modules.checkup.coverage_display import poi_coverage_audit
    path=Path(__file__).resolve().parents[1]/'data/reports/tongji-all7-walk-contour-20261025_report.json'
    report=json.loads(path.read_text())['result']
    before=deepcopy(report['blindspot'])
    audit=poi_coverage_audit(report['blindspot'],report['pois'])
    row=audit['groups']['community_safety']
    assert row['inside_poi_count']==15 and row['unverified_poi_count']==3
    assert not row['poi_check_passed'] and not audit['evidence_complete']
    assert any(x['name']=='金安居民委员会' for x in row['unverified_pois'])
    assert report['blindspot']==before
    from app.schemas.checkup import CheckupJob
    from app.modules.checkup.coverage_display import report_with_display_policy
    public=report_with_display_policy(CheckupJob.model_validate_json(path.read_text()).result)
    assert public.quality['coverage_publication_status']=='incomplete'


def test_real_origin_is_zero_distance_to_itself_even_if_surrounding_routes_detour_over_1km():
    domain=box(-100,-100,100,100)
    route=Oracle(lambda *_:('success',1500,2000))
    _,layers,q=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        [category('school',[poi('inside',31,17)])],{'facilities':[spec('school')]},route,[],origin_mode='actual_poi')
    known=local_region(layers['poi_service_areas'])
    assert known.covers(Point(31,17))
    assert not known.covers(Point(-100,-100))
    props=layers['representative_points']['features'][0]['properties']
    assert props['self_distance_anchor']['source']=='identity_zero_distance'
    assert props['own_location_in_service_area'] is True
    # Analytic identity is kept separate from actual provider observations.
    assert all(f['properties']['distance_m']==1500 for f in layers['route_samples']['features'])


def test_thin_domain_fragments_are_not_lost_when_no_auxiliary_cell_represents_the_coarse_tile():
    domain=box(1,1,19,19).union(box(8,-2,12,0))
    route=Oracle(lambda *_:('success',100,1))
    _,layers,q=analyze_representative_service_areas(domain,_grid_cells(domain,20),to_m,to_center,
        [category('school',[poi('inside',10,10)])],{'facilities':[spec('school')]},route,[],origin_mode='actual_poi')
    known=local_region(layers['total_coverage']['covered_areas'])
    assert known.symmetric_difference(domain).area<1e-5
    assert layers['total_coverage']['unknown_ratio']<1e-8
    assert any(to_m(t.location.lng,t.location.lat)[1]<0 for r in route.requests for t in r.targets)
