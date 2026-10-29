"""Offline checks for real facility origins in the adaptive walking engine."""
import pytest
from shapely.geometry import box

from app.modules.blindspot.service import _grid_cells
from app.modules.checkup.representative_service_areas import (
    ACTUAL_POI_METHOD, analyze_representative_service_areas, build_actual_poi_origins,
)
from test_poi_service_areas import Oracle, category, poi, spec, to_center, to_m


def analyze(rows, route=None, *, domain=None, budget=5000, cache=None):
    domain = box(-100, -100, 100, 100) if domain is None else domain
    warnings = []
    result = analyze_representative_service_areas(domain, _grid_cells(domain, 20), to_m, to_center,
        rows, {'facilities': [spec(row['facility_id']) for row in rows]}, route or Oracle(), warnings,
        max_route_pairs=budget, sample_cache=cache, origin_mode='actual_poi')
    return (*result, warnings)


def test_actual_origins_keep_exact_facility_coordinates_and_explicit_provenance():
    point = poi('school', 31, 17)
    route = Oracle(lambda *_: ('success', 100, 90000))
    ratios, layers, quality, warnings = analyze([category('a', [point])], route)
    assert all(request.origin == to_center(31, 17) for request in route.requests)
    assert quality['coverage_origin_mode'] == 'actual_poi'
    assert quality['coverage_method'] == ACTUAL_POI_METHOD
    assert quality['coverage_origin_grid_m'] is None
    assert quality['coverage_service_distance_m'] == 1000
    assert quality['coverage_route_direction'] == 'poi_to_sample'
    assert ratios['a']['covered_ratio'] == 1
    origin = layers['representative_points']['features'][0]
    assert origin['geometry']['coordinates'] == [point['location']['lng'], point['location']['lat']]
    service = layers['poi_service_areas']['features'][0]['properties']
    assert service['poi_id'] == 'school'
    assert service['origin_location'] == point['location']
    assert service['calculation_method'] == ACTUAL_POI_METHOD
    assert service['origin_mode'] == 'actual_poi'
    assert 'ACTUAL_POI_SERVICE_AREA_ESTIMATE' in {warning.code for warning in warnings}
    assert 'REPRESENTATIVE_SERVICE_AREA_ESTIMATE' not in {warning.code for warning in warnings}


def test_only_identical_poi_id_and_location_share_across_categories():
    shared = poi('same', 31, 17)
    route = Oracle(lambda *_: ('success', 100, 1))
    _, _, shared_quality, _ = analyze([category('a', [shared, shared]), category('b', [shared])], route)
    assert shared_quality['coverage_poi_origin_count'] == 1
    assert shared_quality['coverage_shared_category_uses'] == 2
    assert len({(request.origin.lng, request.origin.lat) for request in route.requests}) == 1
    # Both facilities are in one old 500 m cell, but use distinct true origins.
    distinct = Oracle(lambda *_: ('success', 100, 1))
    _, _, distinct_quality, _ = analyze([
        category('a', [shared]), category('b', [poi('same', 32, 17)])], distinct)
    assert distinct_quality['coverage_poi_origin_count'] == 2
    assert distinct_quality['coverage_route_pairs'] == 2 * shared_quality['coverage_route_pairs']
    assert len({(request.origin.lng, request.origin.lat) for request in distinct.requests}) == 2
    # Matching coordinates alone do not alias another facility identity.
    _, _, quality, _ = analyze([category('a', [shared]), category('b', [poi('other', 31, 17)])])
    assert quality['coverage_poi_origin_count'] == 2


@pytest.mark.parametrize('distance,covered', [(1000, True), (1001, False)])
def test_actual_walking_service_uses_inclusive_1000m_distance_not_duration(distance, covered):
    ratios, layers, _, _ = analyze([category('a', [poi('one', 31, 17)])],
        Oracle(lambda *_: ('success', distance, 90000 if covered else 1)))
    if covered:
        assert ratios['a']['covered_ratio'] == 1
    else:
        # Every measured surrounding node is >1000m, while the actual origin
        # is zero distance to itself. Neither fact can erase the other.
        from shapely.geometry import Point
        from test_poi_service_areas import local_region
        known=local_region(layers['covered_areas'])
        assert 0 < ratios['a']['covered_ratio'] < 1
        assert not known.covers(Point(0,0)) and known.covers(Point(31,17))


def test_no_budget_never_turns_the_straight_line_prefilter_into_service_coverage():
    def forbidden(_):
        raise AssertionError('No routing budget remains')
    ratios, layers, quality, _ = analyze([category('a', [poi('one', 31, 17)])], forbidden, budget=0)
    assert ratios['a']['covered_ratio'] == 0
    assert ratios['a']['unknown_ratio'] == 1
    assert layers['poi_service_areas']['features'] == []
    assert quality['coverage_route_pairs'] == 0


def test_outside_facilities_within_1km_can_serve_domain_without_shifting_their_origin():
    domain = box(-100, -100, 100, 100)
    rows = [category('a', [poi('near-outside', -150, 0), poi('far-outside', -1200, 0)])]
    route = Oracle(lambda *_: ('success', 100, 1))
    ratios, _, quality, _ = analyze(rows, route, domain=domain)
    assert quality['coverage_poi_origin_count'] == 1
    assert quality['coverage_outside_poi_origins_included'] == 1
    assert quality['coverage_outside_poi_memberships_excluded'] == 1
    assert all(request.origin == to_center(-150, 0) for request in route.requests)
    assert ratios['a']['covered_ratio'] == 1


def test_actual_origin_pending_samples_give_small_nearby_turns_before_25_pair_batches():
    domain = box(-600, -300, 1100, 300)
    route = Oracle(lambda *_: ('success', 999, 1))
    _, _, quality, _ = analyze([category('a', [poi('one', 13, 17)]),
        category('b', [poi('two', 513, 17)])], route, domain=domain, budget=50)
    assert len(route.requests) == 4
    assert route.requests[0].origin != route.requests[1].origin
    assert [len(r.targets) for r in route.requests[:2]] == [5,5]
    for request in route.requests:
        assert 0 < len(request.targets) <= 25
        x, y = to_m(request.origin.lng, request.origin.lat)
        distances = [sum((value - origin)**2 for value, origin in
            zip(to_m(target.location.lng, target.location.lat), (x, y))) for target in request.targets]
        assert distances == pytest.approx(sorted(distances), abs=1e-4)
    assert quality['coverage_route_pairs'] == 50


def test_actual_cache_requires_poi_identity_and_matching_real_origin_location():
    rows = [category('a', [poi('one', 31, 17)])]
    domain = box(-100, -100, 100, 100)
    _, layers, _, _ = analyze(rows, Oracle(lambda *_: ('success', 100, 1)))
    cache = {feature['properties']['representative_id']: {
        'location': feature['geometry']['coordinates'], 'samples': {}}
        for feature in layers['representative_points']['features']}
    for feature in layers['route_samples']['features']:
        xy = tuple(round(value, 6) for value in to_m(*feature['geometry']['coordinates']))
        cache[feature['properties']['representative_id']]['samples'][xy] = feature['properties']['distance_m']
    def forbidden(_):
        raise AssertionError('A valid actual-POI cache should avoid requests')
    ratios, _, quality, _ = analyze(rows, forbidden, budget=0, cache=cache)
    assert ratios['a']['covered_ratio'] == 1
    assert quality['coverage_reused_distance_samples'] > 0
    origin = next(iter(cache.values()))
    old_grid_cache = {'grid:r0000:c0000': origin}
    ratios, _, quality, _ = analyze(rows, forbidden, budget=0, cache=old_grid_cache)
    assert ratios['a']['unknown_ratio'] == 1
    assert quality['coverage_reused_distance_samples'] == 0
    origin['location'][0] += .001
    ratios, _, quality, _ = analyze(rows, forbidden, budget=0, cache=cache)
    assert ratios['a']['unknown_ratio'] == 1
    assert quality['coverage_reused_distance_samples'] == 0


def test_actual_origin_window_keeps_exact_1000m_outside_boundary():
    domain = box(-100, -100, 100, 100)
    rows = [category('a', [poi('on-boundary', -1100, 0), poi('beyond', -1101, 0)])]
    origins, count, excluded = build_actual_poi_origins(domain, rows, {'a': spec('a')}, to_m)
    assert count == 1 and excluded == 1
    assert origins[0]['poi_ids'] == {'on-boundary'}


@pytest.mark.parametrize('offset,covered,unknown', [(0, 1, 0), (2000, 0, 0)])
def test_nonlinear_measured_interior_is_not_lost_to_unfinished_refinement(offset, covered, unknown):
    # Saved walking samples surrounding the rehabilitation POI: every measured
    # distance is well inside 1 km, despite a large centre/corner residual.
    domain = box(-200, 400, 0, 600)
    point = poi('rehabilitation', -51 if offset==0 else -600, 486)
    rows = [category('a', [point])]
    origins, _, _ = build_actual_poi_origins(domain, rows, {'a': spec('a')}, to_m)
    coords = [(-200, 400), (0, 400), (0, 600), (-200, 600), (-100, 500)]
    cache = {origins[0]['id']: dict(location=[point['location']['lng'], point['location']['lat']],
        samples={xy: value + offset for xy, value in zip(coords, [372, 140, 452, 292, 82])})}
    def forbidden(_):
        raise AssertionError('Saved measurements must suffice; no new route budget')
    ratios, _, quality, _ = analyze(rows, forbidden, domain=domain, budget=0, cache=cache)
    assert ratios['a']['covered_ratio'] == covered
    assert ratios['a']['unknown_ratio'] == unknown
    assert quality['coverage_route_pairs'] == 0
