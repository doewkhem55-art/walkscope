"""Aggregate no-service requires every selected category's spatial evidence."""
from copy import deepcopy

import pytest
from shapely.geometry import box, shape

from app.modules.checkup import service
from app.modules.blindspot.service import _geojson
from app.modules.isochrone.geometry import geometry_to_local
from app.schemas.isochrone import GeoJSONPolygon
from test_checkup_service import CENTER


def aggregate(categories, blind, unknown):
    assert hasattr(service, 'aggregate_saved_coverage'), 'saved coverage needs all-category aggregation'
    _, to_center = service._projection(CENTER)
    domain = GeoJSONPolygon.model_validate(_geojson(box(0, 0, 100, 100), to_center))
    def collection(regions, state):
        return {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'geometry': _geojson(region, to_center),
             'properties': {'facility_type': fid, 'classification': state}}
            for fid, region in regions.items() if not region.is_empty]}
    layers = {'blind_spots': collection(blind, 'blind'), 'unknown_areas': collection(unknown, 'unknown')}
    original = deepcopy((categories, layers))
    result = service.aggregate_saved_coverage(CENTER, domain, categories, layers)
    assert (categories, layers) == original, 'aggregation must preserve all source facts'
    for record in [*result['group_coverage'].values(), result['total_coverage']]:
        assert record['definition'] == 'all_categories_confirmed_blind'
        assert sum(record[key] for key in ('covered_ratio', 'blind_ratio', 'unknown_ratio')) == pytest.approx(1)
    return result


def category(fid, blind, unknown=0, group='education_children'):
    return dict(facility_id=fid, group_id=group, covered_ratio=1-blind-unknown,
                blind_ratio=blind, unknown_ratio=unknown)


def test_overlapping_blind_regions_intersect_instead_of_union():
    result = aggregate([category('a', .6), category('b', .6)],
                       {'a': box(0, 0, 60, 100), 'b': box(40, 0, 100, 100)}, {})
    record = result['total_coverage']
    assert record['facility_ids'] == ['a', 'b']
    assert record['blind_ratio'] == pytest.approx(.2)
    assert record['covered_ratio'] == pytest.approx(.8)
    feature = record['blind_spots']['features'][0]
    assert feature['properties']['facility_type'] == 'total'
    to_m, _ = service._projection(CENTER)
    geometry = GeoJSONPolygon.model_validate(feature['geometry'])
    assert geometry_to_local(geometry, to_m).bounds == pytest.approx((40, 0, 60, 100), abs=1e-6)
    group_feature = result['group_coverage']['education_children']['blind_spots']['features'][0]
    assert group_feature['properties']['facility_type'] == 'education_children'


def test_disjoint_blind_regions_have_no_aggregate_blind():
    record = aggregate([category('a', .4), category('b', .4)],
                       {'a': box(0, 0, 40, 100), 'b': box(60, 0, 100, 100)}, {})['total_coverage']
    assert record['blind_ratio'] == 0
    assert record['unknown_ratio'] == 0
    assert record['blind_spots']['features'] == []


def test_unknown_only_survives_where_no_category_is_known_covered():
    record = aggregate([category('a', 1), category('b', .2, .3)],
                       {'a': box(0, 0, 100, 100), 'b': box(0, 0, 20, 100)},
                       {'b': box(20, 0, 50, 100)})['total_coverage']
    assert record['blind_ratio'] == pytest.approx(.2)
    assert record['unknown_ratio'] == pytest.approx(.3)
    assert record['covered_ratio'] == pytest.approx(.5)
    blind = shape(record['blind_spots']['features'][0]['geometry'])
    unknown = shape(record['unknown_areas']['features'][0]['geometry'])
    assert blind.intersection(unknown).area == 0


def test_group_and_total_use_every_selected_id_including_absent_features():
    result = aggregate([category('a', 1), category('b', 0), category('c', 1, group='health_medical')],
                       {'a': box(0, 0, 100, 100), 'c': box(0, 0, 100, 100)}, {})
    assert result['group_coverage']['education_children']['facility_ids'] == ['a', 'b']
    assert result['group_coverage']['education_children']['blind_ratio'] == 0
    assert result['group_coverage']['health_medical']['blind_ratio'] == pytest.approx(1)
    assert result['total_coverage']['facility_ids'] == ['a', 'b', 'c']
    assert result['total_coverage']['covered_ratio'] == pytest.approx(1)


@pytest.mark.parametrize('bad_category', [
    {'facility_id': 'b', 'group_id': 'education_children'},
    category('b', 1),  # ratio claims blind, but no blind geometry exists
    category('b', 1e-8),  # missing geometry stays unknown even below area tolerance
])
def test_missing_evidence_is_unknown_and_cannot_shrink_selected_ids(bad_category):
    record = aggregate([category('a', 1), bad_category], {'a': box(0, 0, 100, 100)}, {})['total_coverage']
    assert record['facility_ids'] == ['a', 'b']
    assert record['blind_ratio'] == 0
    assert record['unknown_ratio'] == pytest.approx(1)


def test_contradictory_overlapping_blind_and_unknown_is_conservative():
    record = aggregate([category('a', .5, .5)], {'a': box(0, 0, 50, 100)},
                       {'a': box(0, 0, 50, 100)})['total_coverage']
    assert record['blind_ratio'] == 0
    assert record['unknown_ratio'] == pytest.approx(1)


def test_missing_category_cannot_override_another_known_covered_category():
    record = aggregate([category('a', 0), {'facility_id': 'b', 'group_id': 'education_children'}], {}, {})['total_coverage']
    assert record['covered_ratio'] == pytest.approx(1)
    assert record['unknown_ratio'] == 0


def test_geometry_outside_domain_is_clipped_before_area_calculation():
    record = aggregate([category('a', .5)], {'a': box(-100, -100, 50, 200)}, {})['total_coverage']
    assert record['blind_ratio'] == pytest.approx(.5)
    assert record['covered_ratio'] == pytest.approx(.5)


def test_future_coverage_results_include_aggregates_for_all_selected_categories():
    from app.modules.checkup.catalog import load_catalog
    _, to_center = service._projection(CENTER)
    geometry = GeoJSONPolygon.model_validate(_geojson(box(0, 0, 30, 30), to_center))
    selected = [dict(facility_id=fid, search_status='complete', pois=[])
                for fid in ('childcare_0_3', 'kindergarten')]
    def no_routes(_):
        pytest.fail('complete empty categories require no routing')
    ratios, layers, _ = service.analyze_coverage(CENTER, geometry, selected, load_catalog(), no_routes, [])
    assert layers['total_coverage']['facility_ids'] == ['childcare_0_3', 'kindergarten']
    assert layers['group_coverage']['education_children']['blind_ratio'] == pytest.approx(1)
    assert all(r['blind_ratio'] == pytest.approx(1) for r in ratios.values())


def test_saved_report_normalization_regenerates_stale_aggregates_without_changing_source():
    from tests.report_fixtures import build_report
    from tools.recompute_case import normalize_coverage_metadata
    source = build_report('original')
    report = source.model_copy(deep=True)
    report.blindspot['total_coverage'] = {'blind_ratio': 1, 'facility_ids': ['wrong']}
    original = source.model_dump(mode='json')
    normalized = normalize_coverage_metadata(report, source)
    assert source.model_dump(mode='json') == original
    assert normalized.categories == source.categories
    assert normalized.pois == source.pois
    assert len(normalized.blindspot['group_coverage']) == 7
    total = normalized.blindspot['total_coverage']
    assert total['facility_ids'] == [c.facility_id for c in source.categories]
    assert total['blind_ratio'] == 0
    assert total['unknown_ratio'] == pytest.approx(1)
