"""Recompute saved coverage with the current grid; never rewrite the source report."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import COVERAGE_METHOD, analyze_coverage, aggregate_saved_coverage, _guarded_route
from app.schemas.checkup import CheckupJob, CheckupReport
from app.schemas.common import WarningItem


def normalize_coverage_metadata(report, source):
    """Keep historical acquisition accounting separate from this coverage-only run."""
    current = report.quality
    if current.get('coverage_method') == COVERAGE_METHOD:
        # Preserve the engine's explicit service-area unions/differences.
        records = list(report.blindspot.get('group_coverage', {}).values())
        records.append(report.blindspot.get('total_coverage', {}))
        if (current.get('coverage_route_direction') != 'poi_to_sample'
                or 'poi_service_areas' not in report.blindspot
                or any(record.get('calculation_method') != COVERAGE_METHOD
                       or 'covered_areas' not in record for record in records)):
            raise ValueError('Missing POI-origin service-area provenance or geometry.')
    else:
        report.blindspot.update(aggregate_saved_coverage(
            report.center, report.isochrone.geometry, report.categories, report.blindspot))
    quality = {k: v for k, v in current.items() if k.startswith('coverage_') or k in {
        'grid_cell_count', 'grid_size_m', 'max_supported_category_unknown_ratio',
        'source_coverage_grid_size_m', 'source_report_created_at', 'confidence'}}
    quality.update(source_report_quality=deepcopy(source.quality),
                   source_report_warnings=[w.model_dump(mode='json') for w in source.warnings],
                   reused_center_route_count=len(source.routes))
    for key in ('assessed_categories', 'total_categories', 'selected_categories', 'score_method_version',
                'unverified_categories', 'provisional_categories', 'unavailable_categories', 'not_found_scored_zero'):
        if key in source.quality:
            quality[key] = deepcopy(source.quality[key])
    if 'coverage_provider_requests' in current:
        quality.update(provider_requests=current['coverage_provider_requests'],
                       provider_cache_hits=current.get('coverage_cached_requests', 0),
                       provider_request_limit=450,
                       provider_budget_exhausted=current.get('coverage_stop_reason') == 'PROVIDER_BUDGET_EXHAUSTED')
    report.quality = quality
    retained = [w for w in report.warnings if w.code not in {'APPROXIMATE_BOUNDARIES', 'STAGED_DATA_REUSED'}]
    retained.append(WarningItem(code='APPROXIMATE_BOUNDARIES', message=(
        '生活圈沿用历史步行测时边界；设施服务区从真实POI起点按1000米步行距离、200米粗采样至50米边界加密，插值生成1000米等距边界，'
        '不是逐户覆盖或真实建筑边界。')))
    report.warnings = list({(w.code, w.message): w for w in retained}.values())
    return CheckupReport.model_validate(report.model_dump(mode='json'))


def recompute_saved_case(source, route, *, report_id, max_route_pairs=5000, progress=lambda _: None):
    job = CheckupJob.model_validate(source)
    if job.status != 'completed' or job.result is None or report_id == job.id:
        raise ValueError('A completed source and a different new report ID are required.')
    report = job.result.model_copy(deep=True)
    categories = [dict(facility_id=c.facility_id, search_status=c.search_status,
                       pois=[p.model_dump(mode='json') for p in report.pois if p.facility_id == c.facility_id])
                  for c in report.categories]
    warnings = []
    ratios, blindspot, quality = analyze_coverage(
        report.center, report.isochrone.geometry, categories, load_catalog(),
        _guarded_route(route), warnings, max_route_pairs=max_route_pairs, progress=progress,
    )
    report.report_id = report_id
    report.created_at = datetime.now(timezone.utc).isoformat()
    report.categories = [c.model_copy(update=ratios[c.facility_id]) for c in report.categories]
    report.blindspot = blindspot
    # Prior run counters are historical evidence, never this run's cost/budget.
    # normalize_coverage_metadata retains their full copy in source_report_quality.
    report.quality = {k: v for k, v in report.quality.items() if not k.startswith('coverage_')}
    report.quality.update(quality, coverage_route_budget=max_route_pairs,
                          coverage_recomputed_from=job.id,
                          coverage_recomputed_at=report.created_at,
                          source_coverage_grid_size_m=job.result.quality.get('grid_size_m'),
                          source_report_created_at=job.result.created_at)
    if quality['max_supported_category_unknown_ratio'] > 0.1:
        report.quality['confidence'] = 'low'
    report.warnings.extend(warnings)
    report.warnings.append(WarningItem(code='COVERAGE_RECOMPUTED', message=(
        '复用原报告的历史设施、数量分和生活圈边界；本次从真实POI坐标出发按1000米实际步行距离、自适应采样估算服务区，合并后从生活圈扣除；'
        '未验证的区域保留未知。设施并非本次重新检索。')))
    report.summary += ' 覆盖已另存为真实POI起点1000米步行服务区重算版本；设施和数量分沿用原报告，未知区不等于盲区。'
    # Validate the combined report before it can be saved or displayed.
    report = normalize_coverage_metadata(report, job.result)
    return job.model_copy(update=dict(id=report_id, result=report)).model_dump(mode='json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='3de7777d550f4562afc38d96140f0118')
    parser.add_argument('--id', default='tongji-20m-20261024')
    parser.add_argument('--live', action='store_true', help='Explicitly allow bounded Baidu walking requests')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this operation may consume routing quota')
    if not all(c.isalnum() or c in '-_' for c in args.id):
        parser.error('invalid report ID')
    from tools.dev import load_env
    from app.modules.routing.service import RoutingService
    from app.providers.baidu.routing import BaiduRoutingProvider
    from app.providers.baidu.direction import BaiduDirectionProvider

    load_env(ROOT / '.env')
    key = os.environ.get('BAIDU_MAP_AK', '').strip()
    if not key:
        parser.error('configure the server-side map key locally first')
    reports = ROOT / 'data/reports'
    destination = reports / f'{args.id}_report.json'
    if destination.exists():
        parser.error('destination already exists; use a new ID')
    source = json.loads((reports / f'{args.source}_report.json').read_text())
    cache = ROOT / 'data/cache/coverage-20m'
    cache.mkdir(parents=True, exist_ok=True)
    progress_path = ROOT / 'data/progress' / f'{args.id}_coverage.json'
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    started, last_call, calls, reused = time.monotonic(), 0.0, 0, 0
    last_percent = -1

    def transport(url, timeout):
        nonlocal last_call, calls, reused
        parts = urlsplit(url)
        cache_key = parts.path + '?' + urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != 'ak'])
        path = cache / (hashlib.sha256(cache_key.encode()).hexdigest() + '.json')
        if path.exists():
            reused += 1
            return json.loads(path.read_text())
        if calls >= 450 or time.monotonic() - started >= 1200:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', '本次覆盖重算已达到450次HTTP或20分钟上限。')
        time.sleep(max(0, 1.5 - (time.monotonic() - last_call)))
        remaining = 1200 - (time.monotonic() - started)
        if remaining <= 0:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', '覆盖重算已达到20分钟上限。')
        last_call = time.monotonic()
        calls += 1
        with urlopen(url, timeout=min(timeout, remaining)) as response:
            data = json.load(response)
        if data.get('status') == 0:
            path.write_text(json.dumps(data, ensure_ascii=False))
        return data

    routing = RoutingService(BaiduRoutingProvider(key, transport=transport),
        batch_size=int(os.environ.get('BAIDU_ROUTING_BATCH_SIZE', '3')),
        max_retries=0, fallback_provider=BaiduDirectionProvider(key, transport=transport))

    def progress(fraction):
        nonlocal last_percent
        percent = int(fraction * 100)
        if percent != last_percent:
            last_percent = percent
            state = dict(percent=percent, provider_requests=calls, cached_requests=reused,
                         elapsed_seconds=round(time.monotonic() - started, 1))
            progress_path.write_text(json.dumps(state))
            print(json.dumps(state), flush=True)

    result = recompute_saved_case(source, routing.route, report_id=args.id, progress=progress)
    result['result']['quality'].update(coverage_provider_requests=calls, coverage_cached_requests=reused,
        coverage_request_limit=450, coverage_elapsed_seconds=round(time.monotonic() - started, 1))
    report = normalize_coverage_metadata(CheckupReport.model_validate(result['result']), CheckupReport.model_validate(source['result']))
    result['result'] = report.model_dump(mode='json')
    result = CheckupJob.model_validate(result).model_dump(mode='json')
    temp = destination.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    temp.replace(destination)
    print(json.dumps(dict(saved=str(destination), quality=result['result']['quality']), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
