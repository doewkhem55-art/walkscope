"""Offline checks for today's fixed Tongji 45-category report; no provider calls."""
import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.scoring import score_category
from app.modules.isochrone.grid import EARTH_RADIUS_M, GridField
from app.modules.isochrone.tiers import build_tiers
from app.schemas.checkup import CheckupJob, CheckupReport

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('report', type=Path)
parser.add_argument('--output', type=Path)
args = parser.parse_args()
raw_bytes = args.report.read_bytes()
raw = json.loads(raw_bytes)
issues, unmet = [], []
result = {'input': str(args.report.resolve()), 'sha256': hashlib.sha256(raw_bytes).hexdigest(),
          'offline_only': True, 'issues': issues, 'unmet_acceptance_items': unmet}

class NoCompletedReport(Exception):
    pass


def check(condition, message):
    if not condition:
        issues.append(message)


def quantize(value):
    return min(100.0, max(0.0, math.floor(value / 2.5 + 0.5) * 2.5))

try:
    job = CheckupJob.model_validate(raw) if 'status' in raw and 'result' in raw else None
    report = job.result if job is not None else CheckupReport.model_validate(raw)
    if job is not None:
        result['job'] = {'id': job.id, 'status': job.status, 'stage': job.stage, 'progress': job.progress}
        if job.status == 'failed':
            check(job.error is not None and job.result is None and job.stage == 'failed', 'invalid failed terminal state')
            result['failure_code'] = job.error.code if job.error else None
            unmet.append('fresh completed 45-category report unavailable')
        else:
            check(job.status == 'completed' and job.stage == 'completed' and job.progress == 100,
                  'job is not a completed terminal state')
            check(report is not None and report.report_id == job.id and job.error is None, 'job/report identity mismatch')
    if report is None:
        raise NoCompletedReport
    catalog = load_catalog()
    definitions = {item['id']: item for item in catalog['facilities']}
    expected_ids = set(definitions)
    expected_groups = {item['id'] for item in catalog['groups']}
    categories = {item.facility_id: item for item in report.categories}
    check(set(categories) == expected_ids and len(report.categories) == 45, 'report does not contain exact catalog 45 categories')
    check(set(report.selected_facility_ids) == expected_ids and len(report.selected_facility_ids) == 45,
          'selected scope is not the exact catalog 45 categories')
    check({g.id for g in report.groups} == expected_groups and len(report.groups) == 7, 'report does not contain exact 7 catalog groups')
    check(report.center.crs == 'BD09LL' and abs(report.center.lng - 121.50860201413879) < 1e-10
          and abs(report.center.lat - 31.28826762993392) < 1e-10, 'unexpected fixed Tongji center')
    check(report.center == report.isochrone.center and report.isochrone.time_limit_s == 900, 'isochrone identity/time mismatch')
    check(report.quality.get('score_method_version') == 'presence-count-v3', 'unexpected scoring version')
    history_keys = ['historical_replay', 'historical_subset_replay', 'not_live_checkup', 'offline_render_sample']
    markers = {key: report.quality[key] for key in history_keys if report.quality.get(key)}
    if markers or report.quality.get('stage_reuse'):
        unmet.append('historical/sample/staged provenance present; cannot accept as fresh complete run')
    result.update(report_id=report.report_id, created_at=report.created_at, historical_markers=markers,
                  scope_count=len(categories), group_count=len(report.groups), score=report.score,
                  score_provisional=report.score_provisional)
    domain = shape(report.isochrone.geometry.model_dump(mode='json'))
    for poi in report.pois:
        check(poi.facility_id in categories and poi.location.crs == 'BD09LL', 'POI category/CRS differs from report scope')
        check(poi.in_isochrone == domain.covers(Point(poi.location.lng, poi.location.lat)), f'{poi.poi_id}: inside flag differs from contour')
    route_by_id = {r.target_id: r for r in report.routes}
    check(len(route_by_id) == len(report.routes), 'duplicate center route target IDs')
    inside_ids = {p.poi_id for p in report.pois if p.in_isochrone}
    check(set(route_by_id) == inside_ids, 'center route set differs from in-domain unique POIs')
    for category in report.categories:
        points = [p for p in report.pois if p.facility_id == category.facility_id and p.in_isochrone]
        known = len({p.poi_id for p in points})
        check(known == len(points) == category.known_count, f'{category.facility_id}: known count mismatch')
        expected = score_category(category.facility_id, category.search_status, category.count, known)
        check((category.score, category.score_profile, category.score_basis_count, category.score_band, category.next_band,
               category.score_provisional) == (expected.score, expected.profile, expected.basis_count,
               expected.band, expected.next_band, expected.status == 'provisional'),
              f'{category.facility_id}: count-based score mismatch')
        observed = [route_by_id[p.poi_id] for p in points if p.poi_id in route_by_id]
        success = [r for r in observed if r.status == 'success']
        reachable = sum(r.duration_s <= 900 for r in success)
        unresolved = sum(r.status == 'unavailable' for r in observed)
        check((category.reachable_count, category.unresolved_count) == (reachable, unresolved),
              f'{category.facility_id}: route evidence count mismatch')
        if category.facility_id in definitions and definitions[category.facility_id].get('coverage_method') != 'grid_walking_distance':
            check((category.covered_ratio, category.blind_ratio, category.unknown_ratio) == (0, 0, 1),
                  f'{category.facility_id}: unsupported coverage must remain unknown')
    raw_groups = []
    for group in report.groups:
        members = [c for c in report.categories if c.group_id == group.id]
        scores = [c.score for c in members if c.score is not None]
        raw_score = sum(scores) / len(scores) if scores else None
        raw_groups.append(raw_score)
        check(group.score == (quantize(raw_score) if raw_score is not None else None), f'{group.id}: aggregate score mismatch')
        check(group.score_provisional == (len(scores) < len(members) or any(c.score_provisional for c in members)),
              f'{group.id}: provisional marker does not include partial member evidence')
    total = quantize(sum(raw_groups) / len(raw_groups)) if raw_groups and all(v is not None for v in raw_groups) and all(c.score is not None for c in report.categories) else None
    check(report.score == total and report.score_provisional == any(c.score_provisional for c in report.categories), 'total score/provisional mismatch')
    status_counts = dict(Counter(c.search_status for c in report.categories))
    result['category_statuses'] = status_counts
    result['all_45_searches_complete'] = status_counts.get('complete', 0) == 45
    if not result['all_45_searches_complete']:
        unmet.append('not all 45 searches completed; incomplete/failed categories must remain explicit')
    center = report.center
    cosine = math.cos(math.radians(center.lat))
    def local(geojson):
        if hasattr(geojson, 'model_dump'):
            geojson = geojson.model_dump(mode='json')
        return transform(lambda x, y, z=None: (np.deg2rad(np.asarray(x) - center.lng) * EARTH_RADIUS_M * cosine,
                                             np.deg2rad(np.asarray(y) - center.lat) * EARTH_RADIUS_M), shape(geojson))
    outer = local(report.isochrone.geometry)
    check(outer.is_valid and not outer.is_empty and outer.covers(Point(0, 0)), 'invalid/empty outer contour or center excluded')
    for collection_name, classification, ratio_key in [('blind_spots', 'blind', 'blind_ratio'), ('unknown_areas', 'unknown', 'unknown_ratio')]:
        areas = Counter()
        for feature in report.blindspot.get(collection_name, {}).get('features', []):
            props = feature.get('properties', {})
            fid = props.get('facility_type')
            check(fid in categories and props.get('classification') == classification, f'{collection_name}: feature scope/classification mismatch')
            geometry = local(feature['geometry'])
            check(geometry.is_valid and geometry.difference(outer).area <= 0.01, f'{collection_name}/{fid}: invalid or outside report contour')
            areas[fid] += geometry.area
        for fid, category in categories.items():
            ratio = getattr(category, ratio_key)
            if ratio is not None:
                check(abs(areas[fid] - ratio * outer.area) <= max(0.01, outer.area * 1e-8), f'{collection_name}/{fid}: geometry and ratio differ')
    tiers, saved = report.isochrone.tiers, report.isochrone.time_field
    result['three_tiers_available'] = tiers is not None and saved is not None
    if tiers is None or saved is None:
        unmet.append('fresh 5/10/15-minute tiers/time_field unavailable')
    else:
        check(tiers.thresholds_s == [300, 600, 900], 'unexpected tier thresholds')
        cumulative = [local(item.geometry) for item in tiers.cumulative]
        bands = [local(item.geometry) for item in tiers.bands]
        times = np.array([[np.nan if v is None else float(v) for v in row] for row in saved.times_s])
        field = GridField(times, saved.origin_x_m, saved.origin_y_m, saved.cell_m, center, saved.time_limit_s)
        rebuilt = build_tiers(field)
        metrics = {'cumulative_areas_m2': [g.area for g in cumulative], 'band_areas_m2': [g.area for g in bands],
                   'nesting_difference_m2': [cumulative[i].difference(cumulative[i + 1]).area for i in range(2)],
                   'band_overlap_m2': [bands[i].intersection(bands[j]).area for i in range(3) for j in range(i + 1, 3)],
                   'band_partition_difference_m2': unary_union(bands).symmetric_difference(cumulative[-1]).area,
                   'outer_difference_m2': outer.symmetric_difference(cumulative[-1]).area,
                   'replay_cumulative_difference_m2': [g.symmetric_difference(rebuilt.cumulative_at(t)).area for g, t in zip(cumulative, [300, 600, 900])],
                   'replay_band_difference_m2': [g.symmetric_difference(rebuilt.band_for_label(k)).area for g, k in zip(bands, ['0-300', '300-600', '600-900'])],
                   'unknown_nodes_covered': sum(any(g.covers(Point(*field.point_xy(r, c))) for g in cumulative) for r in range(saved.rows) for c in range(saved.cols) if saved.times_s[r][c] is None),
                   'field_shape': [saved.rows, saved.cols], 'cell_m': saved.cell_m, 'collected_at': saved.collected_at,
                   'field_grid_unknown_ratio': tiers.unknown_ratio,
                   'requested_point_unknown_ratio': report.isochrone.quality.unknown_ratio}
        result['tiers'] = metrics
        area_values = metrics['nesting_difference_m2'] + metrics['band_overlap_m2'] + metrics['replay_cumulative_difference_m2'] + metrics['replay_band_difference_m2'] + [metrics['band_partition_difference_m2'], metrics['outer_difference_m2']]
        check(all(g.is_valid and not g.is_empty for g in cumulative + bands), 'invalid/empty tier geometry')
        check(all(v <= 0.01 for v in area_values), 'tier nesting/partition/replay geometry difference exceeds 0.01 m2')
        unknown_cells = {f'{r}:{c}' for r in range(saved.rows) for c in range(saved.cols) if saved.times_s[r][c] is None}
        check(tiers.unknown_mask is not None and set(tiers.unknown_mask.mask_cells) == unknown_cells, 'unknown mask differs from time_field nulls')
        check(metrics['unknown_nodes_covered'] == 0, 'unmeasured nodes filled into reachable tier')
    q = report.quality
    attempts = q.get('provider_requests')
    check(isinstance(attempts, int) and 0 <= attempts <= 450 and q.get('provider_request_limit') == 450, 'provider request counter/limit invalid')
    result['provider'] = {key: q.get(key) for key in ['provider_requests', 'provider_request_limit', 'provider_budget_exhausted', 'route_pairs_attempted', 'routing_single_route_pairs', 'stage_timings_s', 'coverage_stop_reason', 'center_stop_reason']}
    result['metric_naming_notes'] = [{
        'field': 'quality.stage_counts.poi_categories_searched',
        'recorded_value': q.get('stage_counts', {}).get('poi_categories_searched'),
        'implementation': 'len(searches)',
        'meaning': 'Number of category result records processed, including records appended without a query after a daily-quota stop.',
        'does_not_establish': 'That all listed categories began a provider search or completed successfully.',
        'categories_with_observable_search_activity_at_least': sum(c.search_status in {'complete', 'partial'} for c in report.categories),
        'complete': status_counts.get('complete', 0), 'partial': status_counts.get('partial', 0),
        'failed': status_counts.get('failed', 0),
    }]
    if saved is not None and tiers is not None:
        result['unknown_ratio_note'] = {
            'field_grid_nodes': saved.rows * saved.cols,
            'field_null_nodes': sum(v is None for row in saved.times_s for v in row),
            'requested_measurement_nodes': report.isochrone.quality.routing_sample_count,
            'successful_measurement_nodes': report.isochrone.quality.routing_success_count,
            'explanation': 'The tiers ratio uses the full rectangular grid; the quality ratio uses requested nodes. Null nodes outside the circular sampling domain are not failed requested measurements.',
        }
    result['warning_codes'] = sorted({w.code for w in report.warnings})
    result['categories'] = [{'id': c.facility_id, 'name': c.name_zh, 'search_status': c.search_status, 'count': c.count,
                              'known_count': c.known_count, 'score': c.score, 'provisional': c.score_provisional,
                              'reachable': c.reachable_count, 'unresolved': c.unresolved_count,
                              'covered': c.covered_ratio, 'blind': c.blind_ratio, 'unknown': c.unknown_ratio} for c in report.categories]
except NoCompletedReport:
    pass
except Exception as error:
    issues.append(f'audit could not complete: {type(error).__name__}: {error}')
result['internal_consistency_pass'] = not issues
result['acceptance_complete'] = not issues and not unmet
result['limits'] = ['Offline consistency cannot independently establish provider freshness or successful HTTP delivery.',
                    '1200s is a pre-attempt guard; in-flight/throttle completion may pass that boundary.',
                    'Tier geometry is derived from measured grid times; it does not prove an actual road-edge contour.']
text = json.dumps(result, ensure_ascii=False, indent=2)
if args.output:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding='utf-8')
print(text)
raise SystemExit(0 if result['internal_consistency_pass'] else 1)
