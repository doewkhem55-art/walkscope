"""Combine compatible saved category reports without any provider requests."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shapely import make_valid
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform, unary_union
from app.modules.blindspot.service import _geojson, _polygon_parts
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.coverage_display import apply_display_policy, report_with_display_policy
from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD, METHOD, build_actual_poi_origins, build_origins
from app.modules.checkup.service import aggregate_saved_coverage, _projection, _quantize
from app.modules.isochrone.geometry import geometry_to_local
from app.schemas.checkup import CheckupJob, CheckupReport
from app.schemas.common import WarningItem


_COVERAGE_CONTRACT = ('coverage_method', 'coverage_geometry_method', 'coverage_geometry_grid_m',
    'grid_size_m', 'grid_cell_count', 'coverage_origin_grid_m', 'coverage_origin_mode', 'coverage_origin_rule',
    'coverage_service_distance_m', 'coverage_service_distance_policy', 'coverage_sampling_coarse_m',
    'coverage_sampling_fine_m', 'coverage_sampling_schedule', 'coverage_refinement_rule',
    'coverage_route_direction', 'coverage_precision', 'isochrone_grid_cell_m')
_VARIANT_POLICY = 'preserve_cross_source_disjoint_categories_same_threshold_side_v1'


def _valid_distance(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _continuous_total(categories, layers, domain, to_m, to_center, method):
    # Saved contour coordinates can contain collapsed rings after JSON round trips.
    # Repair only temporary calculation geometries; source evidence stays untouched.
    def local(raw):
        geometry = transform(to_m, shape(raw))
        return unary_union(_polygon_parts(make_valid(geometry)))

    covered = unary_union([local(f['geometry']) for f in layers['poi_service_areas']['features']]).intersection(domain)
    blind = domain
    for category in categories:
        parts = [local(f['geometry']) for f in layers['blind_spots']['features']
                 if f['properties'].get('facility_type') == category.facility_id]
        confirmed = unary_union(parts).intersection(domain)
        ratio = category.blind_ratio
        if ratio is None or abs(confirmed.area - ratio * domain.area) > domain.area * 1e-6 or confirmed.area <= domain.area * 1e-12:
            confirmed = Polygon()
        if confirmed.is_empty:
            blind = Polygon()
            break
        blind = unary_union(_polygon_parts(blind.intersection(confirmed)))
    blind = blind.difference(covered)
    unknown = domain.difference(covered).difference(blind)

    def collection(region, classification):
        geometry = unary_union(_polygon_parts(make_valid(shape(_geojson(region, to_center))))) if not region.is_empty else Polygon()
        return dict(type='FeatureCollection', features=[] if geometry.is_empty else [dict(type='Feature',
            geometry=mapping(geometry), properties=dict(facility_type='total', classification=classification,
                calculation_method=method, estimated=True))])

    covered_ratio = min(1.0, max(0.0, covered.area / domain.area))
    blind_ratio = min(1.0 - covered_ratio, max(0.0, blind.area / domain.area))
    return dict(definition='all_categories_confirmed_blind', facility_ids=[c.facility_id for c in categories],
        covered_areas=collection(covered, 'covered'), blind_spots=collection(blind, 'blind'),
        unknown_areas=collection(unknown, 'unknown'), covered_ratio=covered_ratio, blind_ratio=blind_ratio,
        unknown_ratio=max(0.0, 1.0 - covered_ratio - blind_ratio), calculation_method=method, estimated=True)


def _merge_continuous_evidence(reports, origins, layers, *, preserve_measurement_variants=False):
    expected = {origin['id']: origin for origin in origins}
    points, samples, contours = {}, {}, {}
    layers['poi_service_areas'] = dict(type='FeatureCollection', features=[])
    for report in reports:
        scope = {c.facility_id for c in report.categories}
        source_origins = {}
        for raw in report.blindspot['representative_points']['features']:
            feature = deepcopy(raw)
            props = feature['properties']
            oid = props['representative_id']
            origin = expected.get(oid)
            members = set(props['facility_ids'])
            if origin is None or not members or not members <= scope or not members <= origin['facility_ids']:
                raise ValueError('saved origin identity or category membership differs')
            location = origin['location'].model_dump(mode='json')
            if (props.get('poi_id') not in origin['poi_ids'] or feature['geometry']['type'] != 'Point'
                    or list(feature['geometry']['coordinates']) != [location['lng'], location['lat']]
                    or props.get('origin_mode') != 'actual_poi'
                    or ('origin_location' in props and props['origin_location'] != location)):
                raise ValueError('saved origin identity and exact POI location differ')
            source_origins.setdefault(oid, set()).update(members)
            if oid not in points:
                props.update(facility_ids=sorted(members), origin_location=location, source_report_ids=[report.report_id])
                points[oid] = feature
            else:
                points[oid]['properties']['facility_ids'] = sorted(set(points[oid]['properties']['facility_ids']) | members)
                points[oid]['properties']['source_report_ids'] = sorted(set(points[oid]['properties']['source_report_ids']) | {report.report_id})

        def attributed(raw):
            feature = deepcopy(raw)
            props = feature['properties']
            oid = props['representative_id']
            members = set(props.get('facility_ids', source_origins.get(oid, set())))
            if oid not in source_origins or not members or not members <= source_origins[oid]:
                raise ValueError('saved evidence has unknown origin or category membership')
            location = expected[oid]['location'].model_dump(mode='json')
            if (('poi_id' in props and props['poi_id'] not in expected[oid]['poi_ids'])
                    or ('origin_location' in props and props['origin_location'] != location)):
                raise ValueError('saved evidence origin identity differs')
            sources = (props.get('source_report_ids') or [report.report_id]) if preserve_measurement_variants else [report.report_id]
            props.update(facility_ids=sorted(members), source_report_ids=sorted(set(sources)))
            return feature

        for raw in report.blindspot['poi_service_areas']['features']:
            feature = attributed(raw)
            feature['properties']['source_report_id'] = report.report_id
            layers['poi_service_areas']['features'].append(feature)
        for raw in report.blindspot['route_samples']['features']:
            feature = attributed(raw)
            props = feature['properties']
            key = (props['representative_id'], tuple(feature['geometry']['coordinates']))
            if not _valid_distance(props.get('distance_m')):
                props['distance_m'] = None
            samples.setdefault(key, []).append(feature)
        for raw in report.blindspot['service_distance_contours']['features']:
            feature = attributed(raw)
            props = feature['properties']
            key = (props['representative_id'], json.dumps(feature['geometry'], sort_keys=True),
                   props.get('distance_m'), props.get('boundary_source'))
            if key in contours:
                props['facility_ids'] = sorted(set(props['facility_ids']) | set(contours[key]['properties']['facility_ids']))
                props['source_report_ids'] = sorted(set(props['source_report_ids']) | set(contours[key]['properties']['source_report_ids']))
            contours[key] = feature
    if set(points) != set(expected):
        raise ValueError('saved origin identity evidence is incomplete')
    observations = []
    conflicting_pairs = variant_observations = 0
    for features in samples.values():
        measured = [f for f in features if _valid_distance(f['properties'].get('distance_m'))]
        distances = {f['properties']['distance_m'] for f in measured}
        if len(distances) > 1:
            conflicting_pairs += 1
            for index, first in enumerate(measured):
                a = first['properties']
                for second in measured[index + 1:]:
                    b = second['properties']
                    if a['distance_m'] == b['distance_m']:
                        continue
                    if (not preserve_measurement_variants
                            or set(a['source_report_ids']) & set(b['source_report_ids'])
                            or set(a['facility_ids']) & set(b['facility_ids'])
                            or (a['distance_m'] <= 1000) != (b['distance_m'] <= 1000)):
                        raise ValueError('conflicting saved distance samples')
            # A variant keeps its original source and category scope. Equal
            # observations from another source also stay separate at this pair.
            variants = {}
            for feature in features:
                props = feature['properties']
                props['measurement_variant'] = True
                key = (props['distance_m'], tuple(props['facility_ids']), tuple(props['source_report_ids']))
                variants[key] = feature
            observations.extend(variants.values())
            variant_observations += len(variants)
        else:
            feature = deepcopy(measured[0] if measured else features[0])
            feature['properties']['facility_ids'] = sorted({fid for f in features for fid in f['properties']['facility_ids']})
            feature['properties']['source_report_ids'] = sorted({sid for f in features for sid in f['properties']['source_report_ids']})
            observations.append(feature)
    layers['route_samples'] = dict(type='FeatureCollection', features=observations)
    for key, features in (('representative_points', points), ('service_distance_contours', contours)):
        layers[key] = dict(type='FeatureCollection', features=list(features.values()))
    return dict(coverage_conflicting_sample_pair_count=conflicting_pairs,
        coverage_measurement_variant_pair_count=conflicting_pairs,
        coverage_measurement_variant_observation_count=variant_observations,
        coverage_unique_sample_pair_count=len(samples))


def merge_reports(jobs, report_id, *, preserve_measurement_variants=False):
    reports = [j.result for j in jobs]
    if len(reports) < 2 or any(j.status != 'completed' or j.result is None for j in jobs):
        raise ValueError('need at least two completed reports')
    first = reports[0]
    for report in reports[1:]:
        if any(getattr(report, key) != getattr(first, key) for key in
               ('center', 'isochrone', 'catalog_version', 'score_method')):
            raise ValueError('reports must share centre, isochrone, catalog and scoring method')
        if any(report.quality.get(key) != first.quality.get(key) for key in _COVERAGE_CONTRACT):
            raise ValueError('coverage methods or contract differ')
    method = first.quality.get('coverage_method')
    if method not in (METHOD, ACTUAL_POI_METHOD):
        raise ValueError('unsupported coverage method')
    continuous = method == ACTUAL_POI_METHOD
    if preserve_measurement_variants and not continuous:
        raise ValueError('measurement variants require the current continuous coverage method')
    if continuous:
        required = dict(coverage_geometry_method='measured_distance_triangles_1000m_isoline_v1',
            coverage_geometry_grid_m=None, coverage_origin_grid_m=None, coverage_origin_mode='actual_poi',
            coverage_origin_rule='exact_poi_id_location_crs', coverage_route_direction='poi_to_sample',
            coverage_service_distance_m=1000)
        if any(key not in report.quality or report.quality[key] != value
               for report in reports for key, value in required.items()):
            raise ValueError('current continuous coverage contract is missing or invalid')
    categories = [c.model_copy(deep=True) for r in reports for c in r.categories]
    groups = [g.model_copy(deep=True) for r in reports for g in r.groups]
    if len({c.facility_id for c in categories}) != len(categories) or len({g.id for g in groups}) != len(groups):
        raise ValueError('reports must have disjoint categories and groups')
    pois = {}
    for report in reports:
        for poi in report.pois:
            key = (poi.poi_id, poi.facility_id, poi.location.lng, poi.location.lat, poi.location.crs)
            if key in pois and pois[key] != poi:
                raise ValueError('conflicting saved POI identities')
            pois[key] = poi.model_copy(deep=True)
    routes = {}
    for r in reports:
        for route in r.routes:
            if route.target_id in routes and routes[route.target_id] != route:
                raise ValueError('conflicting saved centre routes')
            routes[route.target_id] = route.model_copy(deep=True)
    layers = {k: dict(type='FeatureCollection', features=[deepcopy(f) for r in reports
              for f in r.blindspot[k]['features']]) for k in ('covered_areas', 'blind_spots', 'unknown_areas')}
    layers['group_coverage'] = {gid: deepcopy(value) for r in reports for gid, value in r.blindspot['group_coverage'].items()}
    to_m, to_center = _projection(first.center)
    domain = geometry_to_local(first.isochrone.geometry, to_m)
    if not domain.is_valid or domain.is_empty or domain.area <= 0:
        raise ValueError('invalid isochrone geometry')
    rows = [dict(facility_id=c.facility_id, search_status=c.search_status,
                 pois=[p.model_dump(mode='json') for p in pois.values() if p.facility_id == c.facility_id]) for c in categories]
    definitions = {f['id']: f for f in load_catalog()['facilities']}
    origin_rows = rows
    if continuous:
        # Historical v2 reports excluded capacity-based catalogue classes.
        # Preserve their evidence contract while newer reports include every
        # real POI for walking access, independently of capacity scoring.
        access_ids = set()
        for report in reports:
            access_ids.update(report.quality.get('coverage_access_facility_ids', [c.facility_id
                for c in report.categories if report.quality.get('coverage_access_scope') ==
                'all_real_pois_uniform_1000m_not_capacity_assessment' or
                definitions[c.facility_id].get('coverage_method') == 'grid_walking_distance']))
        origin_rows = [r for r in rows if r['facility_id'] in access_ids]
    origins, count, outside = (build_actual_poi_origins if continuous else build_origins)(domain, origin_rows, definitions, to_m)
    sample_quality = dict(coverage_conflicting_sample_pair_count=0, coverage_measurement_variant_pair_count=0,
                          coverage_measurement_variant_observation_count=0)
    if continuous:
        sample_quality.update(_merge_continuous_evidence(reports, origins, layers,
            preserve_measurement_variants=preserve_measurement_variants))
        layers['total_coverage'] = _continuous_total(categories, layers, domain, to_m, to_center, method)
        for group in groups:
            saved = layers['group_coverage'][group.id]
            group.summary = ('已保存真实POI起点1000米实际步行连续服务区估算结果：'
                f"估算覆盖{saved['covered_ratio']:.1%}、未知{saved['unknown_ratio']:.1%}；检索完成不代表覆盖范围已全部核验。")
    else:
        total = aggregate_saved_coverage(first.center, first.isochrone.geometry, categories, layers)['total_coverage']
        excluded = [geometry_to_local(SimpleNamespace(**f['geometry']), to_m)
                    for k in ('blind_spots', 'unknown_areas') for f in total[k]['features']]
        covered = domain.difference(unary_union(excluded))
        total.update(calculation_method=method, estimated=True,
            covered_areas=dict(type='FeatureCollection', features=[] if covered.is_empty else [dict(type='Feature',
                geometry=_geojson(covered, to_center), properties=dict(facility_type='total', classification='covered'))]))
        layers['total_coverage'] = total
    # Keep source-specific category assignments: combining service polygons would transfer evidence to other categories.
    if not continuous:
        layers['poi_service_areas'] = dict(type='FeatureCollection', features=[])
        samples = {}
        for r in reports:
            for f in r.blindspot['poi_service_areas']['features']:
                feature = deepcopy(f)
                feature['properties']['source_report_id'] = r.report_id
                layers['poi_service_areas']['features'].append(feature)
            for f in r.blindspot['route_samples']['features']:
                key = (f['properties']['representative_id'], tuple(f['geometry']['coordinates']))
                old = samples.get(key)
                distance = f['properties'].get('distance_m')
                valid = _valid_distance(distance)
                if old and _valid_distance(old['properties'].get('distance_m')) and valid and old['properties']['distance_m'] != distance:
                    raise ValueError('conflicting saved distance samples')
                if old is None or valid:
                    samples[key] = deepcopy(f)
        layers['route_samples'] = dict(type='FeatureCollection', features=list(samples.values()))
        features = []
        for origin in origins:
            point = to_center(*origin['xy'])
            features.append(dict(type='Feature', geometry=dict(type='Point', coordinates=[point.lng, point.lat]),
                properties=dict(representative_id=origin['id'], facility_ids=sorted(origin['facility_ids']),
                    poi_count=len(origin['poi_ids']), max_poi_offset_m=round(origin['max_poi_offset_m'], 2), origin_grid_m=500)))
        layers['representative_points'] = dict(type='FeatureCollection', features=features)
    raw_scores = [sum(c.score for c in categories if c.group_id == g.id and c.score is not None)
                  / sum(c.score is not None for c in categories if c.group_id == g.id)
                  for g in groups if any(c.score is not None for c in categories if c.group_id == g.id)]
    score = _quantize(sum(raw_scores) / len(groups)) if len(raw_scores) == len(groups) and all(c.score is not None for c in categories) else None
    warnings = {(w.code, w.message): w for r in reports for w in r.warnings if w.code not in ('SELECTED_SCOPE', 'UNKNOWN_INCLUDED_IN_DISPLAY_BLIND')}
    scope_note = '已覆盖完整设施目录。' if len(categories) == len(definitions) else '未计算其余设施类别；不能与45类完整总分直接比较。'
    warnings[('SELECTED_SCOPE', '')] = WarningItem(code='SELECTED_SCOPE', message=f'本报告合并{len(groups)}组、{len(categories)}个小项，{scope_note}')
    variant_pairs = sample_quality['coverage_measurement_variant_pair_count']
    if variant_pairs:
        warnings[('MEASUREMENT_VARIANTS_PRESERVED', '')] = WarningItem(code='MEASUREMENT_VARIANTS_PRESERVED', message=(
            f'{variant_pairs}对真实起点与目标存在不同来源的步行距离观测差异；'
            '仅在分类不交叠且均处于1000米阈值同一侧时保留各原值、来源和分类归属。'
            '各组服务区保持原证据；样本数量按观测计，含变体的报告不可作为单值测距缓存。'))
    quality = {k: deepcopy(first.quality[k]) for k in _COVERAGE_CONTRACT if k in first.quality}
    for key in ('coverage_access_scope','coverage_origin_zero_anchor','coverage_boundary_fragment_policy',
                'coverage_triangulation_scope','coverage_origin_priority'):
        if key in first.quality and all(r.quality.get(key)==first.quality[key] for r in reports):
            quality[key]=deepcopy(first.quality[key])
    if continuous:
        quality['coverage_access_facility_ids'] = sorted(access_ids)
    quality.update(selected_categories=len(categories), total_categories=len(categories), assessed_categories=sum(c.score is not None for c in categories),
        coverage_poi_origin_count=len(origins), coverage_source_poi_count=count, coverage_outside_poi_memberships_excluded=outside,
        coverage_estimated=True, coverage_budget_exhausted=any(r.quality.get('coverage_budget_exhausted') for r in reports),
        provider_requests=0, merge_only=True, reused_poi_search=True, confidence='low',
        source_provider_requests_total=sum(r.quality.get('provider_requests', 0) for r in reports),
        source_reports=[dict(report_id=r.report_id, quality=deepcopy(r.quality),
            report_sha256=hashlib.sha256(r.model_dump_json().encode()).hexdigest()) for r in reports])
    quality.update(coverage_distance_sample_count=sum(_valid_distance(f['properties'].get('distance_m'))
                       for f in layers['route_samples']['features']),
        coverage_failed_sample_count=sum(not _valid_distance(f['properties'].get('distance_m'))
                       for f in layers['route_samples']['features']),
        coverage_contour_segment_count=len(layers.get('service_distance_contours', {}).get('features', [])),
        coverage_shared_category_uses=sum(len(o['facility_ids']) for o in origins),
        coverage_outside_poi_origins_included=sum(o.get('outside_domain', False) for o in origins),
        coverage_reused_distance_samples=sum(_valid_distance(f['properties'].get('distance_m'))
                       for f in layers['route_samples']['features']),
        new_http_requests=0, new_poi_search_requests=0, new_center_route_requests=0,
        coverage_route_pairs=0, coverage_route_batches=0, coverage_route_succeeded=0,
        coverage_route_no_route=0, coverage_route_unavailable=0)
    quality.update(sample_quality,
        coverage_sample_observation_count=len(layers['route_samples']['features']),
        coverage_sample_count_semantics='saved_measurement_observations',
        coverage_measurement_variant_policy=_VARIANT_POLICY if preserve_measurement_variants else 'reject_conflicting_distances',
        cache_compatible=not variant_pairs and all(r.quality.get('cache_compatible', True) for r in reports))
    quality.setdefault('coverage_unique_sample_pair_count', len(layers['route_samples']['features']))
    for flag in ('provider_budget_exhausted', 'coverage_http_budget_exhausted', 'coverage_pair_budget_exhausted'):
        quality[flag] = any(r.quality.get(flag, False) for r in reports)
    quality['max_supported_category_unknown_ratio'] = max((c.unknown_ratio for c in categories
        if c.unknown_ratio is not None and definitions[c.facility_id].get('coverage_method') == 'grid_walking_distance'), default=1)
    location = first.location_label.split('·')[0].strip()
    group_names = '、'.join(g.name_zh for g in groups)
    report = first.model_dump(mode='json')
    report.update(report_id=report_id, created_at=datetime.now(timezone.utc).isoformat(),
        location_label=f'{location} · {group_names}', categories=categories, groups=groups,
        selected_facility_ids=[c.facility_id for c in categories], score=score,
        score_provisional=any(c.score_provisional for c in categories), pois=list(pois.values()), routes=list(routes.values()),
        blindspot=apply_display_policy(layers), warnings=list(warnings.values()), quality=quality,
        summary=f'{location}：{group_names}共{len(groups)}组、{len(categories)}个小项合并展示。复用已保存的同一生活圈和独立核验结果，无新增地图请求；盲区包含未核验区域。')
    return CheckupJob(id=report_id, status='completed', stage='completed', progress=100,
                      result=report_with_display_policy(CheckupReport.model_validate(report)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path, nargs='+')
    parser.add_argument('--id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preserve-measurement-variants', action='store_true',
        help='Keep cross-source, disjoint-category distance observations only when they agree on the 1000m threshold side')
    args = parser.parse_args()
    result = merge_reports([CheckupJob.model_validate_json(p.read_text()) for p in args.sources], args.id,
        preserve_measurement_variants=args.preserve_measurement_variants)
    if args.output.exists():
        raise FileExistsError('refusing to overwrite a saved report')
    args.output.write_text(result.model_dump_json(indent=2))
    print(json.dumps(dict(report_id=result.id, score=result.result.score,
        groups=[g.id for g in result.result.groups], total_blind_ratio=result.result.blindspot['total_coverage']['display_blind_ratio']), ensure_ascii=False))
