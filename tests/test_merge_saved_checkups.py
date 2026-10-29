from pathlib import Path
from copy import deepcopy
import hashlib
import json
import pytest
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from app.schemas.checkup import CheckupJob
from tools.merge_saved_checkups import merge_reports


@pytest.fixture(scope='module')
def continuous_source():
    path = Path(__file__).resolve().parents[1] / 'data/reports/tongji-education-health-walk-contour-20261025_report.json'
    return json.loads(path.read_text())


def continuous_part(source, group_id, facility_ids=None, *, new_group=None):
    """Split real saved evidence, keeping shared origins scoped to this part."""
    report = source['result']
    ids = set(facility_ids or [c['facility_id'] for c in report['categories'] if c['group_id'] == group_id])
    result = {key: deepcopy(value) for key, value in report.items()
              if key not in ('categories', 'groups', 'pois', 'blindspot', 'quality')}
    result.update(categories=[deepcopy(c) for c in report['categories'] if c['facility_id'] in ids],
                  groups=[deepcopy(g) for g in report['groups'] if g['id'] == group_id],
                  pois=[deepcopy(p) for p in report['pois'] if p['facility_id'] in ids],
                  selected_facility_ids=sorted(ids),
                  quality={k: deepcopy(v) for k, v in report['quality'].items() if not k.startswith('source_')})
    result['report_id'] = new_group or group_id
    layers = result['blindspot'] = {}
    for key in ('covered_areas', 'blind_spots', 'unknown_areas'):
        layers[key] = dict(type='FeatureCollection', features=[deepcopy(f) for f in report['blindspot'][key]['features']
                          if f['properties']['facility_type'] in ids])
    for key in ('representative_points', 'poi_service_areas'):
        features = []
        for feature in report['blindspot'][key]['features']:
            members = sorted(ids.intersection(feature['properties']['facility_ids']))
            if members:
                feature = deepcopy(feature)
                feature['properties']['facility_ids'] = members
                if 'facility_type' in feature['properties']:
                    feature['properties']['facility_type'] = members[0]
                features.append(feature)
        layers[key] = dict(type='FeatureCollection', features=features)
    origin_ids = {f['properties']['representative_id'] for f in layers['representative_points']['features']}
    for key in ('route_samples', 'service_distance_contours'):
        layers[key] = dict(type='FeatureCollection', features=[deepcopy(f) for f in report['blindspot'][key]['features']
                          if f['properties']['representative_id'] in origin_ids])
    layers['group_coverage'] = {(new_group or group_id): deepcopy(report['blindspot']['group_coverage'][group_id])}
    layers['total_coverage'] = deepcopy(report['blindspot']['group_coverage'][group_id])
    if new_group:
        for category in result['categories']:
            category['group_id'] = new_group
        result['groups'][0].update(id=new_group, name_zh='额外设施组')
    return CheckupJob.model_validate(dict(id=result['report_id'], status='completed', stage='completed', progress=100, result=result))


def collection_geometry(collection):
    return unary_union([shape(f['geometry']) for f in collection['features']])


def test_continuous_merge_preserves_real_origins_contours_and_source_evidence(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    before = [job.model_dump_json() for job in jobs]
    merged = merge_reports(jobs, 'continuous-two-groups').result
    original = continuous_source['result']
    origins = merged.blindspot['representative_points']['features']
    assert len(origins) == 302
    assert {f['properties']['representative_id']: f['geometry'] for f in origins} == {
        f['properties']['representative_id']: f['geometry'] for f in original['blindspot']['representative_points']['features']}
    for key, count in (('route_samples', 5756), ('service_distance_contours', 1062)):
        features = merged.blindspot[key]['features']
        assert len(features) == count
        memberships = {f['properties']['representative_id']: set(f['properties']['facility_ids']) for f in origins}
        assert all(set(f['properties']['facility_ids']) <= memberships[f['properties']['representative_id']]
                   and f['properties']['facility_ids'] for f in features)
    assert merged.quality['coverage_geometry_method'] == 'measured_distance_triangles_1000m_isoline_v1'
    assert merged.quality['coverage_geometry_grid_m'] is None
    assert merged.quality['coverage_origin_grid_m'] is None
    assert merged.quality['coverage_poi_origin_count'] == merged.quality['coverage_source_poi_count'] == 302
    assert merged.quality['coverage_distance_sample_count'] == 5705
    assert merged.quality['coverage_failed_sample_count'] == 51
    assert merged.quality['coverage_contour_segment_count'] == 1062
    assert merged.quality['coverage_outside_poi_origins_included'] == 174
    assert merged.quality['coverage_shared_category_uses'] == 328
    assert merged.quality['provider_requests'] == 0
    assert merged.score == 57.5
    for job in jobs:
        group = job.result.groups[0].id
        assert merged.blindspot['group_coverage'][group]['unknown_ratio'] == job.result.blindspot['group_coverage'][group]['unknown_ratio']
        for key in ('covered_areas', 'blind_spots', 'unknown_areas'):
            assert merged.blindspot['group_coverage'][group][key] == job.result.blindspot['group_coverage'][group][key]
            assert all(feature in merged.blindspot[key]['features'] for feature in job.result.blindspot[key]['features'])
    assert [job.model_dump_json() for job in jobs] == before


def test_continuous_three_groups_recompute_score_and_location_text(continuous_source):
    education = ['childcare_0_3', 'kindergarten', 'primary_school']
    jobs = [continuous_part(continuous_source, 'education_children', education),
            continuous_part(continuous_source, 'education_children', ['secondary_school', 'after_school_care', 'education_training'],
                            new_group='elderly_disability'),
            continuous_part(continuous_source, 'health_medical')]
    for job, score in zip(jobs, (0.0, 50.0, 100.0)):
        job.result.location_label = '回归测试地点 · 原分组'
        for category in job.result.categories:
            category.score = score
    merged = merge_reports(jobs, 'continuous-three-groups').result
    assert len(merged.groups) == 3 and len(merged.categories) == 13
    assert merged.score == 50
    assert '回归测试地点' in merged.location_label and '额外设施组' in merged.location_label
    assert '3组' in merged.summary and '13个小项' in merged.summary
    assert '同济' not in merged.summary


def test_continuous_merge_replaces_pending_group_summary_without_changing_source_evidence(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    for job in jobs:
        job.result.groups[0].summary = '内部来源快照；服务区待测。'
    before = [job.model_dump_json() for job in jobs]
    merged = merge_reports(jobs, 'completed-group-summaries').result
    for group in merged.groups:
        record = merged.blindspot['group_coverage'][group.id]
        assert '待测' not in group.summary and '内部来源' not in group.summary
        assert '真实POI' in group.summary and '1000米' in group.summary and '连续服务区' in group.summary
        assert f"覆盖{record['covered_ratio']:.1%}" in group.summary
        assert f"未知{record['unknown_ratio']:.1%}" in group.summary
        assert '检索完成不代表覆盖范围已全部核验' in group.summary
        source = next(job.result for job in jobs if job.result.groups[0].id == group.id)
        assert group.model_dump(exclude={'summary'}) == source.groups[0].model_dump(exclude={'summary'})
        for key in ('covered_areas', 'blind_spots', 'unknown_areas'):
            assert record[key] == source.blindspot['group_coverage'][group.id][key]
    assert merged.score == 57.5
    assert [job.model_dump_json() for job in jobs] == before


@pytest.mark.parametrize('key,value', [
    ('coverage_method', 'shared_500m_origins_adaptive_1km_service_area_v1'),
    ('coverage_geometry_method', 'grid_cell_union'),
    ('coverage_geometry_grid_m', 20),
    ('coverage_route_direction', 'sample_to_poi'),
    ('coverage_service_distance_m', 500),
    ('coverage_origin_rule', 'rounded_coordinates'),
])
def test_continuous_merge_rejects_incompatible_coverage_contract(continuous_source, key, value):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    jobs[1].result.quality[key] = value
    before = [job.model_dump_json() for job in jobs]
    with pytest.raises(ValueError, match='method|contract'):
        merge_reports(jobs, 'bad-coverage-contract')
    assert [job.model_dump_json() for job in jobs] == before


@pytest.mark.parametrize('key', ['center', 'isochrone', 'catalog_version', 'score_method'])
def test_continuous_merge_rejects_different_report_context(continuous_source, key):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    report = jobs[1].result
    if key == 'center':
        report.center = report.center.model_copy(update={'lng': 120.0})
    elif key == 'isochrone':
        report.isochrone = report.isochrone.model_copy(update={'geometry': jobs[0].result.isochrone.geometry.model_copy(
            update={'coordinates': []})})
    else:
        setattr(report, key, 'different')
    with pytest.raises(ValueError, match='centre|isochrone|catalog|scoring'):
        merge_reports(jobs, 'bad-report-context')


def test_continuous_merge_rejects_conflicting_samples_and_origin_identity(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    report = jobs[0].result
    sample = deepcopy(next(f for f in report.blindspot['route_samples']['features'] if f['properties']['distance_m'] is not None))
    sample['properties']['distance_m'] += 1
    report.blindspot['route_samples']['features'].append(sample)
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'bad-samples')
    report.blindspot['route_samples']['features'].pop()
    report.blindspot['representative_points']['features'][0]['geometry']['coordinates'][0] += .001
    with pytest.raises(ValueError, match='origin|identity'):
        merge_reports(jobs, 'bad-origin')


def test_continuous_shared_origin_deduplicates_evidence_without_transferring_service_polygons(continuous_source):
    jobs = [continuous_part(continuous_source, 'education_children', ['childcare_0_3']),
            continuous_part(continuous_source, 'education_children', ['education_training'], new_group='elderly_disability')]
    first_samples = {(f['properties']['representative_id'], tuple(f['geometry']['coordinates'])): f
                     for f in jobs[0].result.blindspot['route_samples']['features']}
    common_sample = next(f for f in jobs[1].result.blindspot['route_samples']['features']
                         if (f['properties']['representative_id'], tuple(f['geometry']['coordinates'])) in first_samples
                         and f['properties']['distance_m'] is not None)
    oid = common_sample['properties']['representative_id']
    coordinates = common_sample['geometry']['coordinates']
    expected_distance = common_sample['properties']['distance_m']
    # A failed duplicate must not erase a successful saved measurement.
    first_samples[(oid, tuple(coordinates))]['properties']['distance_m'] = None
    before = [job.model_dump_json() for job in jobs]
    merged = merge_reports(jobs, 'shared-real-origin').result
    merged_sample = next(f for f in merged.blindspot['route_samples']['features']
                         if f['properties']['representative_id'] == oid and f['geometry']['coordinates'] == coordinates)
    assert merged_sample['properties']['distance_m'] == expected_distance
    assert merged_sample['properties']['facility_ids'] == ['childcare_0_3', 'education_training']
    assert merged_sample['properties']['source_report_ids'] == ['education_children', 'elderly_disability']
    for feature in merged.blindspot['poi_service_areas']['features']:
        expected = ['childcare_0_3'] if feature['properties']['source_report_id'] == 'education_children' else ['education_training']
        assert feature['properties']['facility_ids'] == expected
    assert len(merged.blindspot['route_samples']['features']) == len({
        (f['properties']['representative_id'], tuple(f['geometry']['coordinates']))
        for job in jobs for f in job.result.blindspot['route_samples']['features']})
    assert [job.model_dump_json() for job in jobs] == before
    first_samples[(oid, tuple(coordinates))]['properties']['distance_m'] = expected_distance + 1
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'conflicting-shared-origin')


def variant_jobs(source, distances):
    jobs = [continuous_part(source, 'education_children', ['childcare_0_3']),
            continuous_part(source, 'education_children', ['education_training'], new_group='elderly_disability')]
    by_key = [{(f['properties']['representative_id'], tuple(f['geometry']['coordinates'])): f
               for f in job.result.blindspot['route_samples']['features']} for job in jobs]
    key = next(key for key in by_key[0].keys() & by_key[1].keys()
               if by_key[0][key]['properties']['distance_m'] is not None)
    for rows, distance in zip(by_key, distances):
        rows[key]['properties']['distance_m'] = distance
    return jobs, key


@pytest.mark.parametrize('distances', [(527.0, 519.0), (1258.0, 1272.0), (999.0, 1000.0)])
def test_explicit_variants_keep_each_distance_and_its_source_categories(continuous_source, distances):
    jobs, key = variant_jobs(continuous_source, distances)
    before = [job.model_dump_json() for job in jobs]
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'strict-default')
    merged = merge_reports(jobs, 'same-side-variants', preserve_measurement_variants=True).result
    observations = [f['properties'] for f in merged.blindspot['route_samples']['features']
                    if (f['properties']['representative_id'], tuple(f['geometry']['coordinates'])) == key]
    assert {(p['distance_m'], tuple(p['facility_ids']), tuple(p['source_report_ids'])) for p in observations} == {
        (distances[0], ('childcare_0_3',), ('education_children',)),
        (distances[1], ('education_training',), ('elderly_disability',))}
    assert merged.quality['coverage_conflicting_sample_pair_count'] == 1
    assert merged.quality['coverage_measurement_variant_pair_count'] == 1
    assert merged.quality['coverage_measurement_variant_observation_count'] == 2
    assert merged.quality['coverage_sample_observation_count'] == len(merged.blindspot['route_samples']['features'])
    assert merged.quality['coverage_sample_observation_count'] == merged.quality['coverage_unique_sample_pair_count'] + 1
    assert merged.quality['cache_compatible'] is False
    assert merged.quality['coverage_measurement_variant_policy'] == 'preserve_cross_source_disjoint_categories_same_threshold_side_v1'
    assert any(w.code == 'MEASUREMENT_VARIANTS_PRESERVED' for w in merged.warnings)
    for job in jobs:
        for gid, group in job.result.blindspot['group_coverage'].items():
            for name in ('covered_areas', 'blind_spots', 'unknown_areas'):
                assert merged.blindspot['group_coverage'][gid][name] == group[name]
    assert [job.model_dump_json() for job in jobs] == before


@pytest.mark.parametrize('distances', [(995.0, 1001.0), (1000.0, 1001.0)])
def test_explicit_variants_still_reject_threshold_disagreement(continuous_source, distances):
    jobs, _ = variant_jobs(continuous_source, distances)
    before = [job.model_dump_json() for job in jobs]
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'cross-threshold', preserve_measurement_variants=True)
    assert [job.model_dump_json() for job in jobs] == before


def test_explicit_variants_still_reject_same_source_category_conflicts(continuous_source):
    jobs, key = variant_jobs(continuous_source, (527.0, 519.0))
    sample = deepcopy(next(f for f in jobs[0].result.blindspot['route_samples']['features']
                           if (f['properties']['representative_id'], tuple(f['geometry']['coordinates'])) == key))
    sample['properties']['distance_m'] = 520.0
    jobs[0].result.blindspot['route_samples']['features'].append(sample)
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'same-source-category-conflict', preserve_measurement_variants=True)


def test_explicit_variants_use_containing_report_when_saved_source_ids_are_empty(continuous_source):
    jobs, key = variant_jobs(continuous_source, (527.0, 519.0))
    for job in jobs:
        for sample in job.result.blindspot['route_samples']['features']:
            sample['properties']['source_report_ids'] = []
    merged = merge_reports(jobs, 'empty-source-id-fallback', preserve_measurement_variants=True).result
    assert {tuple(f['properties']['source_report_ids']) for f in merged.blindspot['route_samples']['features']
            if (f['properties']['representative_id'], tuple(f['geometry']['coordinates'])) == key} == {
        ('education_children',), ('elderly_disability',)}


def test_real_education_culture_17_variant_pairs_preserve_source_observations_and_geometry():
    folder = Path(__file__).resolve().parents[1] / 'data/reports'
    paths = [folder / f'tongji-{name}-walk-contour-20261025_report.json' for name in ('education-health', 'culture')]
    jobs = [CheckupJob.model_validate_json(path.read_text()) for path in paths]
    before = [job.model_dump_json() for job in jobs]
    file_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    with pytest.raises(ValueError, match='conflicting saved distance'):
        merge_reports(jobs, 'real-strict-default')
    merged = merge_reports(jobs, 'real-17-variant-pairs', preserve_measurement_variants=True).result
    source_pairs = {}
    for job in jobs:
        memberships = {f['properties']['representative_id']: tuple(sorted(f['properties']['facility_ids']))
                       for f in job.result.blindspot['representative_points']['features']}
        for feature in job.result.blindspot['route_samples']['features']:
            props = feature['properties']
            if props['distance_m'] is None:
                continue
            key = (props['representative_id'], tuple(feature['geometry']['coordinates']))
            source_pairs.setdefault(key, []).append((props['distance_m'], memberships[key[0]], (job.result.report_id,)))
    conflicts = {key: values for key, values in source_pairs.items() if len({value[0] for value in values}) > 1}
    assert len(conflicts) == 17
    for key, expected in conflicts.items():
        observed = [(f['properties']['distance_m'], tuple(f['properties']['facility_ids']), tuple(f['properties']['source_report_ids']))
                    for f in merged.blindspot['route_samples']['features']
                    if (f['properties']['representative_id'], tuple(f['geometry']['coordinates'])) == key]
        assert sorted(observed) == sorted(expected)
    assert merged.quality['coverage_conflicting_sample_pair_count'] == 17
    assert merged.quality['coverage_measurement_variant_pair_count'] == 17
    assert merged.quality['coverage_measurement_variant_observation_count'] == 34
    assert merged.quality['cache_compatible'] is False
    assert merged.quality['coverage_sample_observation_count'] == merged.quality['coverage_unique_sample_pair_count'] + 17
    for job in jobs:
        for gid, group in job.result.blindspot['group_coverage'].items():
            for key in ('covered_areas', 'blind_spots', 'unknown_areas'):
                assert merged.blindspot['group_coverage'][gid][key] == group[key]
    assert [job.model_dump_json() for job in jobs] == before
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == file_hashes


@pytest.mark.parametrize('field,value', [('poi_id', 'different-poi'), ('origin_location', {'lng': 121.0, 'lat': 31.0, 'crs': 'WGS84'})])
def test_continuous_merge_binds_origin_to_exact_poi_identity_and_crs(continuous_source, field, value):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    jobs[0].result.blindspot['representative_points']['features'][0]['properties'][field] = value
    with pytest.raises(ValueError, match='origin identity'):
        merge_reports(jobs, 'wrong-identity')


def test_continuous_merge_requires_current_geometry_contract_even_if_all_sources_omit_it(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    for job in jobs:
        del job.result.quality['coverage_geometry_method']
    with pytest.raises(ValueError, match='contract'):
        merge_reports(jobs, 'missing-geometry-contract')


def test_continuous_merge_keeps_same_poi_id_at_distinct_exact_locations_separate(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    report = jobs[0].result
    old = report.blindspot['representative_points']['features'][0]
    poi = next(p for p in report.pois if p.poi_id == old['properties']['poi_id']
               and p.facility_id in old['properties']['facility_ids'])
    location = poi.location.model_copy(update={'lng': poi.location.lng + .0001})
    report.pois.append(poi.model_copy(update={'location': location}))
    identity = [poi.poi_id, location.lng, location.lat, location.crs]
    oid = 'poi:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    point = deepcopy(old)
    point['geometry']['coordinates'] = [location.lng, location.lat]
    point['properties'].update(representative_id=oid, facility_ids=[poi.facility_id])
    report.blindspot['representative_points']['features'].append(point)
    merged = merge_reports(jobs, 'distinct-exact-origins').result
    assert merged.quality['coverage_poi_origin_count'] == 303
    assert {f['properties']['representative_id'] for f in merged.blindspot['representative_points']['features']} >= {
        old['properties']['representative_id'], oid}
    assert len([p for p in merged.pois if p.poi_id == poi.poi_id and p.facility_id == poi.facility_id]) == 2


def test_continuous_merge_total_is_service_union_complement_with_unknown_separate(continuous_source):
    jobs = [continuous_part(continuous_source, group) for group in ('education_children', 'health_medical')]
    # Put hand-checked, disjoint strips inside the real domain. Each source
    # covers a strip; confirmed blind needs both sources to rule out service.
    domain = shape(continuous_source['result']['isochrone']['geometry'])
    xmin, ymin, xmax, ymax = domain.bounds
    x1, x2, x3 = [xmin + (xmax - xmin) * fraction for fraction in (.2, .4, .6)]
    strips = [domain.intersection(box(left, ymin, right, ymax))
              for left, right in ((xmin, x1), (x1, x2), (x2, x3), (x3, xmax))]
    for job, covered, blind, unknown in ((jobs[0], strips[0], strips[2], unary_union([strips[1], strips[3]])),
                                         (jobs[1], strips[1], unary_union([strips[2], strips[3]]), strips[0])):
        layers = job.result.blindspot
        for key, geometry, classification in (('covered_areas', covered, 'covered'), ('blind_spots', blind, 'blind'),
                                               ('unknown_areas', unknown, 'unknown')):
            layers[key]['features'] = [dict(type='Feature', geometry=mapping(geometry),
                properties=dict(facility_type=category.facility_id, classification=classification))
                for category in job.result.categories]
        for feature in layers['poi_service_areas']['features']:
            feature['geometry'] = mapping(covered)
        job.result.categories = [category.model_copy(update=dict(covered_ratio=covered.area / domain.area,
            blind_ratio=blind.area / domain.area, unknown_ratio=unknown.area / domain.area))
            for category in job.result.categories]
    total = merge_reports(jobs, 'geometry-strips').result.blindspot['total_coverage']
    expected_covered = unary_union(strips[:2])
    for key, expected in (('covered_areas', expected_covered), ('blind_spots', strips[2]),
                          ('unknown_areas', strips[3]), ('display_blind_spots', domain.difference(expected_covered))):
        assert collection_geometry(total[key]).symmetric_difference(expected).area < 1e-12
    assert total['covered_ratio'] + total['blind_ratio'] + total['unknown_ratio'] == pytest.approx(1)
    assert total['blind_ratio'] > 0 and total['unknown_ratio'] > 0


def test_saved_education_and_health_merge_without_changing_evidence():
    folder = Path(__file__).resolve().parents[1] / 'data/reports'
    jobs = [CheckupJob.model_validate_json((folder / f'tongji-{name}-shared500-20261024_report.json').read_text())
            for name in ('education', 'health')]
    before = [j.model_dump_json() for j in jobs]
    combined = merge_reports(jobs, 'combined-test').result
    assert len(combined.groups) == 2 and len(combined.categories) == 13
    assert combined.score == 57.5
    assert len({p.poi_id for p in combined.pois if p.in_isochrone}) == 128
    assert combined.quality['provider_requests'] == 0
    assert len(combined.blindspot['representative_points']['features']) == 10
    for source in jobs:
        for key, value in source.result.blindspot['group_coverage'].items():
            assert combined.blindspot['group_coverage'][key]['unknown_ratio'] == value['unknown_ratio']
        assert all(c in combined.categories for c in source.result.categories)
        assert all(p in combined.pois for p in source.result.pois)
    total = combined.blindspot['total_coverage']
    assert total['unknown_ratio'] == pytest.approx(.196571897, abs=1e-8)
    assert total['display_blind_ratio'] == pytest.approx(total['unknown_ratio'])
    assert total['covered_ratio'] + total['display_blind_ratio'] == pytest.approx(1)
    merged_covered = unary_union([shape(f['geometry']) for f in total['covered_areas']['features']])
    sources_covered = unary_union([shape(f['geometry']) for j in jobs
        for f in j.result.blindspot['total_coverage']['covered_areas']['features']])
    assert merged_covered.symmetric_difference(sources_covered).area < 1e-12
    assert [j.model_dump_json() for j in jobs] == before
    changed = jobs[1].model_copy(deep=True)
    changed.result.center = changed.result.center.model_copy(update={'lng': 120})
    with pytest.raises(ValueError, match='centre'):
        merge_reports([jobs[0], changed], 'bad-centre')
    with pytest.raises(ValueError, match='disjoint'):
        merge_reports([jobs[0], jobs[0]], 'duplicate-category')
