"""Bounded A->B evidence experiment. Dry-run by default; never changes a contour."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union
from app.modules.isochrone.grid import EARTH_RADIUS_M
from app.modules.isochrone.route_edges import EdgeThresholds, bd09ll_to_local, validate_candidate_edge
from app.schemas.checkup import CheckupJob

WORKSPACE = Path(__file__).resolve().parents[1]
ENDPOINT = 'https://api.map.baidu.com/directionlite/v1/walking'
TIERS = (300, 600, 900)
MAX_ATTEMPTS = 40


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


http_open = build_opener(NoRedirect()).open
LIMITATIONS = [
    'Only exact measured grid nodes are reused; no interpolated contour vertex is a measured endpoint.',
    'Direct measured center-to-B and estimated center-to-A plus A-to-B are distinct evidence.',
    'Tier holes and unknown masks are computed constraints, not independently verified water or road barriers.',
    'Independent water/obstacle information is unknown; full interior route arrival times are not measured.',
    'Validator thresholds are uncalibrated experimental defaults; every result remains ineligible for production.',
]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


def lnglat(center, x, y):
    return [center.lng + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(center.lat)))),
            center.lat + math.degrees(y / EARTH_RADIUS_M)]


def select_candidates(report, maximum=10):
    field = report.isochrone.time_field
    settings = EdgeThresholds()
    pools = {tier: [] for tier in TIERS}
    for row in range(field.rows):
        for col in range(field.cols):
            for dr, dc in ((0, 1), (1, 0)):
                rr, cc = row + dr, col + dc
                if rr >= field.rows or cc >= field.cols:
                    continue
                first, second = field.times_s[row][col], field.times_s[rr][cc]
                if first is None or second is None:
                    continue
                nodes = [(row, col, float(first)), (rr, cc, float(second))]
                nodes.sort(key=lambda item: (item[2], item[0], item[1]))
                a, b = nodes
                xy = [[field.origin_x_m + c * field.cell_m, field.origin_y_m + r * field.cell_m]
                      for r, c, _ in nodes]
                midpoint = [(xy[0][i] + xy[1][i]) / 2 for i in (0, 1)]
                sector = int((math.atan2(midpoint[1], midpoint[0]) + math.pi) / (math.pi / 4)) % 8
                for tier in TIERS:
                    band = settings.time_band_s(tier)
                    if all(tier - band <= item[2] <= tier + band for item in nodes):
                        pools[tier].append({
                            'tier_s': tier, 'a_grid': list(a[:2]), 'b_grid': list(b[:2]),
                            'a_grid_id': f'grid:r{a[0]:04d}:c{a[1]:04d}',
                            'b_grid_id': f'grid:r{b[0]:04d}:c{b[1]:04d}',
                            'a': [round(v, 6) for v in lnglat(report.center, *xy[0])],
                            'b': [round(v, 6) for v in lnglat(report.center, *xy[1])],
                            'center_to_a_duration_s': a[2], 'measured_center_to_b_duration_s': b[2],
                            'midpoint_local_m': midpoint, 'sector': sector,
                            'endpoint_max_deviation_s': max(abs(a[2] - tier), abs(b[2] - tier)),
                        })
    selected, used_pairs = [], set()
    while len(selected) < maximum:
        progress = False
        for tier in TIERS:
            prior = [item for item in selected if item['tier_s'] == tier]
            sectors = {item['sector'] for item in prior}
            eligible = []
            for item in pools[tier]:
                pair = tuple(sorted((tuple(item['a_grid']), tuple(item['b_grid']))))
                if pair in used_pairs:
                    continue
                if any(math.dist(item['midpoint_local_m'], other['midpoint_local_m']) < 2 * field.cell_m
                       for other in prior):
                    continue
                eligible.append(item)
            if not eligible:
                continue
            item = min(eligible, key=lambda value: (value['sector'] in sectors,
                       value['endpoint_max_deviation_s'], value['a_grid'], value['b_grid']))
            selected.append({**item, 'candidate_id': f'edge-{len(selected) + 1:02d}'})
            used_pairs.add(tuple(sorted((tuple(item['a_grid']), tuple(item['b_grid'])))))
            progress = True
            if len(selected) >= maximum:
                break
        if not progress:
            break
    return selected, {str(tier): len(pool) for tier, pool in pools.items()}


def normalized_direction(payload, center):
    routes = payload.get('result', {}).get('routes')
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise ValueError('ROUTE_MISSING')
    route = routes[0]
    for name in ('distance', 'duration'):
        value = route.get(name)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError('ROUTE_METRIC_INVALID')
    steps = route.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ValueError('STEP_PATH_MISSING')
    parts, flat, max_gap = [], [], 0.0
    for step in steps:
        path = step.get('path') if isinstance(step, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise ValueError('STEP_PATH_MISSING')
        points = []
        for pair in path.split(';'):
            if not pair.strip():
                continue
            try:
                lng, lat = [float(value) for value in pair.split(',')]
            except (ValueError, TypeError):
                raise ValueError('STEP_PATH_INVALID') from None
            if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
                raise ValueError('STEP_PATH_INVALID')
            point = [lng, lat]
            if not points or point != points[-1]:
                points.append(point)
        if not points:
            raise ValueError('STEP_PATH_MISSING')
        if flat:
            gap = math.dist(bd09ll_to_local(flat[-1], center), bd09ll_to_local(points[0], center))
            max_gap = max(max_gap, gap)
        parts.append(points)
        flat.extend(points if not flat or flat[-1] != points[0] else points[1:])
    if len(flat) < 2:
        raise ValueError('STEP_PATH_TOO_SHORT')
    return {'distance_m': route['distance'], 'duration_s': route['duration'], 'crs': 'BD09LL',
            'path_parts': parts, 'polyline': flat if max_gap <= 1 else None,
            'max_step_junction_gap_m': max_gap, 'continuous_within_1m': max_gap <= 1}


def constraints(report, tier):
    cumulative = next(item for item in report.isochrone.tiers.cumulative if item.threshold_s == tier)
    geometry = shape(cumulative.geometry.model_dump(mode='json'))
    polygons = [geometry] if geometry.geom_type == 'Polygon' else geometry.geoms
    holes = [Polygon(ring) for polygon in polygons for ring in polygon.interiors]
    mask = report.isochrone.tiers.unknown_mask
    unknown = shape(mask.mask_geometry.model_dump(mode='json')) if mask and mask.mask_geometry else None
    return unary_union(holes) if holes else None, unknown


def write_table(directory, record):
    rows = ['# A→B 相邻格点候选边实验', '',
            f"模式：{record['mode']}；报告：{record['report_id']}；HTTP 尝试：{record['attempts_used']}/{MAX_ATTEMPTS}。",
            '', '独立水体/障碍信息：未知。全部结果仅供候选边研究，不接入生产。', '',
            '|候选|档位|A格点 / 实测秒|B格点 / 直接实测秒|A→B秒|判定|原因|',
            '|---|---:|---|---|---:|---|---|']
    for item in record['results']:
        candidate = item['candidate']
        rows.append('|{}|{}|{} / {}|{} / {}|{}|{}|{}|'.format(
            candidate['candidate_id'], candidate['tier_s'], candidate['a_grid'],
            candidate['center_to_a_duration_s'], candidate['b_grid'],
            candidate['measured_center_to_b_duration_s'], item.get('route', {}).get('duration_s', '—'),
            item['outcome'], ', '.join(item.get('decision', {}).get('reason_codes', item.get('reason_codes', [])))))
    rows.extend(['', '限制：', *['- ' + item for item in LIMITATIONS]])
    (directory / 'decisions.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')


def execute(record, output, key, report):
    ledger = output / 'execution.json'
    if ledger.exists():
        raise ValueError('Existing execution ledger: inspect it; this script never automatically resumes or repeats requests.')
    atomic_json(ledger, record)
    last_start = 0.0
    for item in record['results']:
        if record['attempts_used'] >= MAX_ATTEMPTS:
            record['stopped_reason'] = 'ATTEMPT_LIMIT'
            break
        candidate = item['candidate']
        time.sleep(max(0, 1.5 - (time.monotonic() - last_start)))
        record['attempts_used'] += 1
        item.update(outcome='attempting', attempt=record['attempts_used'])
        atomic_json(ledger, record)  # Persist uncertain attempts before the HTTP call.
        last_start = time.monotonic()
        params = {'origin': f"{candidate['a'][1]:.6f},{candidate['a'][0]:.6f}",
                  'destination': f"{candidate['b'][1]:.6f},{candidate['b'][0]:.6f}",
                  'coord_type': 'bd09ll', 'output': 'json', 'ak': key}
        try:
            with http_open(ENDPOINT + '?' + urlencode(params), timeout=10) as response:
                payload = json.load(response)
            status = payload.get('status') if isinstance(payload, dict) else None
            item['provider_status'] = int(status) if isinstance(status, (int, str)) and str(status).isdigit() else None
            if status not in (0, '0'):
                item['outcome'] = 'provider_denied'
                record['stopped_reason'] = 'PROVIDER_NONZERO_STOP_NO_RETRY'
                break
            try:
                item['route'] = normalized_direction(payload, record['center'])
                if item['route']['polyline'] is None:
                    item.update(outcome='unverified', reason_codes=['STEP_PATH_DISCONTINUOUS'])
                    continue
                holes, unknown = constraints(report, candidate['tier_s'])
                decision = validate_candidate_edge(
                    center=record['center'], a=candidate['a'], b=candidate['b'],
                    polyline=item['route']['polyline'], tier_s=candidate['tier_s'], crs='BD09LL',
                    center_to_a_duration_s=candidate['center_to_a_duration_s'],
                    route_duration_s=item['route']['duration_s'], holes=holes, unknown=unknown,
                )
                item.update(outcome=decision.status, decision=decision.as_dict(),
                            independent_obstacles_status='unknown', production_eligible=False,
                            measured_vs_via_a_b_gap_s=(candidate['center_to_a_duration_s'] +
                                item['route']['duration_s'] - candidate['measured_center_to_b_duration_s']))
            except ValueError as error:
                item.update(outcome='unverified', reason_codes=[str(error)])
        except HTTPError as error:
            item.update(outcome='http_error', http_status=error.code)
            record['stopped_reason'] = 'HTTP_ERROR_STOP_NO_RETRY'
            break
        except URLError as error:
            item.update(outcome='network_error', reason_type=type(error.reason).__name__)
            record['stopped_reason'] = 'NETWORK_ERROR_STOP_NO_RETRY'
            break
        except Exception as error:
            item.update(outcome='unavailable', exception_type=type(error).__name__)
            record['stopped_reason'] = 'UNAVAILABLE_STOP_NO_RETRY'
            break
        finally:
            item['elapsed_s'] = round(time.monotonic() - last_start, 3)
            atomic_json(ledger, record)
            write_table(output, record)
    record['outcome_counts'] = dict(Counter(item['outcome'] for item in record['results']))
    atomic_json(ledger, record)
    write_table(output, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--max-candidates', type=int, default=10, choices=range(1, 11))
    parser.add_argument('--execute', action='store_true', help='Explicitly enable at most 40 real HTTP attempts; no retry.')
    args = parser.parse_args()
    raw = args.report.read_bytes()
    job = CheckupJob.model_validate_json(raw)
    report = job.result
    if job.status != 'completed' or report is None or job.id != report.report_id:
        parser.error('A completed CheckupJob with matching report ID is required.')
    markers = ('historical_replay', 'historical_subset_replay', 'not_live_checkup', 'offline_render_sample', 'stage_reuse')
    if any(report.quality.get(key) for key in markers) or any(w.code == 'OFFLINE_RENDER_SAMPLE' for w in report.warnings):
        parser.error('Historical, staged or rendering-sample reports cannot be used as fresh route evidence.')
    field = report.isochrone.time_field
    if report.center != report.isochrone.center or field is None or report.isochrone.tiers is None:
        parser.error('The fresh report must contain matching center, tiers and measured time_field.')
    selected, eligible = select_candidates(report, args.max_candidates)
    output = args.output_dir or args.report.parent / 'neighbor-routes'
    output.mkdir(parents=True, exist_ok=True)
    record = {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'execute' if args.execute else 'dry-run',
              'report_id': report.report_id, 'input_sha256': hashlib.sha256(raw).hexdigest(),
              'center': [report.center.lng, report.center.lat], 'crs': 'BD09LL',
              'field_source': field.source, 'field_collected_at': field.collected_at,
              'request_coordinate_precision_decimals': 6, 'retries': 0, 'http_interval_s': 1.5,
              'endpoint_provenance': 'exact saved measured time_field row/column; provider endpoint per node not retained',
              'routing_single_route_pairs_in_report': report.quality.get('routing_single_route_pairs'),
              'eligible_adjacent_pairs_per_tier': eligible, 'selected_count': len(selected),
              'max_attempts': MAX_ATTEMPTS, 'attempts_used': 0,
              'independent_obstacles_status': 'unknown', 'production_eligible': False, 'limitations': LIMITATIONS,
              'results': [{'candidate': item, 'outcome': 'not_requested'} for item in selected]}
    atomic_json(output / 'plan.json', record)
    write_table(output, record)
    if args.execute and not selected:
        record['stopped_reason'] = 'NO_ELIGIBLE_MEASURED_NEIGHBORS'
        atomic_json(output / 'plan.json', record)
    elif args.execute:
        key = ''
        for line in (WORKSPACE / '.env').read_text().splitlines():
            if line.strip().startswith('BAIDU_MAP_AK='):
                key = line.split('=', 1)[1].strip().strip('\"\'')
                break
        if not key:
            parser.error('Local BAIDU_MAP_AK missing; no request sent.')
        execute(record, output, key, report)
    print(json.dumps({'mode': record['mode'], 'selected_count': len(selected), 'attempts_used': record['attempts_used'],
                      'output_dir': str(output.resolve()), 'stopped_reason': record.get('stopped_reason')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
