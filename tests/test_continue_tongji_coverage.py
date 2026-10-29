import importlib
import pytest
from tests.test_recompute_actual_poi_case import saved_case
from tools.recompute_actual_poi_case import recompute_actual_case


def test_continuation_reuses_saved_samples_and_keeps_unverified_publication_honest():
    def forbidden(_): raise AssertionError('no network in zero-budget continuation')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='old-actual',max_route_pairs=0)
    before=source.model_dump_json()
    tool=importlib.import_module('tools.continue_tongji_coverage')
    result=tool.continue_saved_report(source,forbidden,report_id='new-actual',max_route_pairs=0)
    assert source.model_dump_json()==before
    assert result.result.isochrone==source.result.isochrone
    assert result.result.pois==source.result.pois and result.result.score==source.result.score
    assert result.result.quality['coverage_validation']['poi_check_passed'] is False
    assert result.result.quality['coverage_publication_status']=='incomplete'
    assert result.result.quality['coverage_continued_from']=='old-actual'


def test_continuation_requires_a_new_output_identity():
    def forbidden(_): raise AssertionError('no network')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='old-actual',max_route_pairs=0)
    tool=importlib.import_module('tools.continue_tongji_coverage')
    with pytest.raises(ValueError):
        tool.continue_saved_report(source,forbidden,report_id=source.id,max_route_pairs=0)


def test_partial_round_pair_files_are_recovered_without_resubmitting_measured_points(tmp_path):
    from test_poi_service_areas import Oracle
    from tools.cached_route_reuse import CacheFirstRouting
    from app.schemas.routing import RoutingRequest
    from app.schemas.common import CenterPoint
    def forbidden(_): raise AssertionError('offline recovery must not use a provider')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='old-actual',max_route_pairs=0)
    report=source.result; origin=report.pois[0].location
    router=CacheFirstRouting(Oracle(lambda *_:('success',100,1)),[],tmp_path/'pairs')
    router.route(RoutingRequest(schema_version='1.0',origin=origin,travel_mode='walking',
        targets=[dict(target_id='real',location=CenterPoint(lng=origin.lng+.0001,lat=origin.lat,crs='BD09LL'))]))
    tool=importlib.import_module('tools.continue_tongji_coverage')
    result=tool.continue_saved_report(source,forbidden,report_id='new-actual',max_route_pairs=0,
        route_pair_cache_dir=tmp_path/'pairs')
    assert result.result.quality['coverage_cache_source']['pair_cache_samples_added']==1
    assert result.result.quality['new_http_requests']==0


def test_gap_targeting_measures_only_missing_mesh_nodes_and_reuses_them_on_a_second_pass():
    from test_poi_service_areas import Oracle
    tool=importlib.import_module('tools.continue_tongji_coverage')
    def forbidden(_):raise AssertionError('no provider request expected')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='old-actual',max_route_pairs=0)
    oracle=Oracle(lambda *_:('success',100,1))
    result=tool.fill_group_gaps(source,oracle,report_id='gap-first',max_route_pairs=100)
    assert result.result.blindspot['group_coverage']['education_children']['covered_ratio']==pytest.approx(1)
    assert 0<sum(len(r.targets) for r in oracle.requests)<40
    identities=[(r.origin.lng,r.origin.lat,t.location.lng,t.location.lat) for r in oracle.requests for t in r.targets]
    assert len(identities)==len(set(identities))
    second=tool.fill_group_gaps(result,forbidden,report_id='gap-second',max_route_pairs=100)
    assert second.result.quality['coverage_gap_targeting']['target_pairs']==0


def test_gap_targeting_zero_budget_cannot_turn_a_poi_or_straight_line_bound_into_coverage():
    tool=importlib.import_module('tools.continue_tongji_coverage')
    def forbidden(_):raise AssertionError('zero budget')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='old-actual',max_route_pairs=0)
    result=tool.fill_group_gaps(source,forbidden,report_id='gap-zero',max_route_pairs=0)
    assert result.result.blindspot['total_coverage']['covered_ratio']==0
    assert not result.result.quality['coverage_validation']['poi_check_passed']


def test_category_gap_scope_finishes_missing_subcategory_even_when_group_is_covered():
    from test_poi_service_areas import Oracle
    tool=importlib.import_module('tools.continue_tongji_coverage')
    def forbidden(_):raise AssertionError('zero budget')
    source=recompute_actual_case(saved_case().model_dump(mode='json'),forbidden,
        report_id='subcategory-source',max_route_pairs=0)
    # The group is already covered by another subcategory, while the individual
    # category evidence still needs real walking-distance observations.
    for record in source.result.blindspot['group_coverage'].values():
        record['unknown_areas']={'type':'FeatureCollection','features':[]}
        record['unknown_ratio']=0
    oracle=Oracle(lambda *_:('success',100,1))
    result=tool.fill_group_gaps(source,oracle,report_id='subcategory-filled',max_route_pairs=100,
        scope='categories')
    assert oracle.requests
    measured_ids={poi.facility_id for poi in source.result.pois}
    assert all(row.unknown_ratio==pytest.approx(0) for row in result.result.categories
        if row.facility_id in measured_ids)


def test_gap_targeting_considers_a_small_component_even_if_the_representative_is_farther_than_1km():
    from shapely.geometry import box,Point
    from shapely.ops import transform,unary_union
    from app.modules.checkup.service import _projection
    from app.modules.blindspot.service import _geojson
    from test_poi_service_areas import Oracle
    from shapely.geometry import shape
    tool=importlib.import_module('tools.continue_tongji_coverage')
    case=saved_case();p,to_center=_projection(case.result.center)
    domain=box(0,0,20,20).union(box(150,0,195,50))
    case.result.isochrone=type(case.result.isochrone).model_validate(
        case.result.isochrone.model_dump(mode='json')|{'geometry':_geojson(domain,to_center)})
    case.result.pois[0].location=to_center(-980,10);case.result.pois[0].in_isochrone=False
    case.result.pois[1].location=to_center(5000,5000);case.result.pois[1].in_isochrone=False
    def forbidden(_):raise AssertionError('zero budget')
    source=recompute_actual_case(case.model_dump(mode='json'),forbidden,report_id='disjoint-source',max_route_pairs=0)
    oracle=Oracle(lambda *_:('success',100,1))
    result=tool.fill_group_gaps(source,oracle,report_id='disjoint-filled',max_route_pairs=100)
    assert oracle.requests
    covered=unary_union([transform(p,shape(f['geometry'])) for f in
        result.result.blindspot['group_coverage']['education_children']['covered_areas']['features']])
    assert covered.covers(Point(10,10))
