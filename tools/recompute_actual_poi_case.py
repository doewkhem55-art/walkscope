"""Recompute saved facilities' 1000 m walking service areas from their real coordinates."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
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
from app.modules.checkup.coverage_display import report_with_display_policy
from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD, LEGACY_ACTUAL_POI_METHOD, build_actual_poi_origins
from app.modules.checkup.service import _guarded_route, _projection, analyze_coverage
from app.modules.isochrone.geometry import geometry_to_local
from app.schemas.checkup import CheckupJob, CheckupReport
from app.schemas.common import CenterPoint, WarningItem

HTTP_LIMIT = 450
SECONDS_LIMIT = 1200
PAIR_LIMIT = 5000


def category_rows(report):
    return [dict(facility_id=category.facility_id, search_status=category.search_status,
        pois=[poi.model_dump(mode='json') for poi in report.pois if poi.facility_id == category.facility_id])
        for category in report.categories]


def saved_actual_sample_cache(report):
    """Reuse an actual-origin report's measurements without provider access."""
    if report.quality.get('coverage_method') not in {ACTUAL_POI_METHOD, LEGACY_ACTUAL_POI_METHOD}:
        raise ValueError('Saved samples must have actual POI origins')
    to_m, _ = _projection(report.center)
    domain = geometry_to_local(report.isochrone.geometry, to_m)
    definitions = {item['id']: item for item in load_catalog()['facilities']}
    origins, _, _ = build_actual_poi_origins(domain, category_rows(report), definitions, to_m)
    by_id = {origin['id']: origin for origin in origins}
    cache = {}
    for feature in report.blindspot['representative_points']['features']:
        props = feature['properties']
        origin = by_id[props['representative_id']]
        location = origin['location']
        if (props.get('origin_mode') != 'actual_poi' or props.get('poi_id') not in origin['poi_ids']
                or feature['geometry']['coordinates'] != [location.lng, location.lat]):
            raise ValueError('Saved sample origin does not match the real POI')
        cache[origin['id']] = dict(location=[location.lng, location.lat], samples={}, failed_points=[])
    for feature in report.blindspot['route_samples']['features']:
        props = feature['properties']
        if props.get('origin_mode') != 'actual_poi':
            raise ValueError('Refusing representative-point samples')
        entry = cache[props['representative_id']]
        xy = tuple(round(value, 6) for value in to_m(*feature['geometry']['coordinates']))
        value = props.get('distance_m')
        if value is None:
            entry['failed_points'].append(xy)
        elif isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid saved walking distance')
        elif xy in entry['samples'] and entry['samples'][xy] != value:
            raise ValueError('Conflicting saved walking distances')
        else:
            entry['samples'][xy] = float(value)
    return cache, dict(source_report_id=report.report_id, network_access=False,
        available_samples=sum(len(entry['samples']) for entry in cache.values()),
        available_origins=sum(bool(entry['samples']) for entry in cache.values()),
        failed_samples=sum(len(entry['failed_points']) for entry in cache.values()))


def load_actual_sample_cache(path, report):
    """Bind historical distances to an exact real POI identity and location."""
    if path is None:
        return {}, dict(available_samples=0, available_origins=0)
    path = Path(path)
    raw = json.loads(path.read_text())
    to_m, _ = _projection(report.center)
    domain = geometry_to_local(report.isochrone.geometry, to_m)
    definitions = {item['id']: item for item in load_catalog()['facilities']}
    origins, _, _ = build_actual_poi_origins(domain, category_rows(report), definitions, to_m)
    by_identity = {(next(iter(origin['poi_ids'])), origin['location'].lng, origin['location'].lat,
                    origin['location'].crs): origin for origin in origins}
    cache = {}
    serialized_match = raw.get('meta', {}).get('provider_origin_precision') == 6
    for item in raw['records']:
        distance = item.get('distance_m')
        if (item.get('status') != 'success' or isinstance(distance, bool)
                or not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance < 0):
            continue
        location = CenterPoint.model_validate(item['origin_location'])
        origin = by_identity.get((item['poi_id'], location.lng, location.lat, location.crs))
        if origin is None:
            continue
        source = CenterPoint.model_validate(item.get('source_origin_location', item['origin_location']))
        if source != location and (not serialized_match or
                f'{source.lat:.6f},{source.lng:.6f}' != f'{location.lat:.6f},{location.lng:.6f}'):
            continue
        target = CenterPoint.model_validate(item['target_location'])
        xy = tuple(round(value, 6) for value in to_m(target.lng, target.lat))
        entry = cache.setdefault(origin['id'], dict(location=[location.lng, location.lat], samples={}))
        if xy in entry['samples'] and entry['samples'][xy] != float(distance):
            raise ValueError('Conflicting saved distances for the same real origin and target')
        entry['samples'][xy] = float(distance)
    return cache, dict(source=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        provider_origin_precision=raw.get('meta', {}).get('provider_origin_precision'),
        available_samples=sum(len(entry['samples']) for entry in cache.values()), available_origins=len(cache))


def recompute_actual_case(source, route, *, report_id, sample_cache=None, cache_info=None,
                          max_route_pairs=PAIR_LIMIT, progress=lambda value: None):
    job = CheckupJob.model_validate(source)
    if job.status != 'completed' or job.result is None or report_id == job.id:
        raise ValueError('A completed source report and a new report id are required')
    old = job.result
    if old.center != old.isochrone.center or old.isochrone.time_limit_s != 900 or old.isochrone.quality.grid_cell_m != 100:
        raise ValueError('The source must retain its matching-centre 900 s / 100 m isochrone')
    report = old.model_copy(deep=True)
    warnings = [WarningItem(code='HISTORICAL_POIS_REUSED', message='设施和圈外候选沿用保存数据，本轮未重新检索设施或计算中心生活圈。'),
        WarningItem(code='SELECTED_SCOPE', message=f'仅计算本报告选择的{len(old.groups)}组、{len(old.categories)}个小项，不代表45类完整报告。')]
    ratios, layers, quality = analyze_coverage(old.center, old.isochrone.geometry, category_rows(old),
        load_catalog(), _guarded_route(route), warnings, max_route_pairs=max_route_pairs,
        sample_cache=sample_cache, origin_mode='actual_poi', progress=progress)
    if quality.get('coverage_method') != ACTUAL_POI_METHOD:
        raise ValueError('Expected actual POI origins; refusing to publish representative-point coverage')
    report.report_id = report_id
    report.created_at = datetime.now(timezone.utc).isoformat()
    report.categories = [category.model_copy(update=ratios[category.facility_id]) for category in report.categories]
    report.blindspot = layers
    report.quality = dict(quality, provider_requests=0, new_http_requests=0,
        coverage_route_budget=max_route_pairs, coverage_cache_source=cache_info or {},
        source_report_id=old.report_id, source_report_created_at=old.created_at,
        source_report_sha256=hashlib.sha256(old.model_dump_json().encode()).hexdigest(),
        source_report_quality=deepcopy(old.quality), source_report_warnings=[w.model_dump(mode='json') for w in old.warnings],
        reused_poi_search=True, reused_center_isochrone=True, isochrone_grid_cell_m=100,
        new_poi_search_requests=0, new_center_route_requests=0, score_preserved=True,
        selected_categories=len(report.categories), total_categories=len(report.categories),
        assessed_categories=sum(category.score is not None for category in report.categories),
        confidence='low' if quality['max_supported_category_unknown_ratio'] > .1 else 'medium')
    warnings.append(WarningItem(code='ACTUAL_POI_COVERAGE_RECOMPUTED', message=(
        '本轮以每个设施的真实坐标为起点，按1000米实际步行距离估算服务区；纳入距生活圈不超过1000米的历史圈外候选。'
        '200米粗采样后加密至50米，用实际步行距离插值得到1000米等距边界，服务区并集从生活圈扣除；未核验区域仍保留未知。')))
    report.warnings = warnings
    report.summary = (f'{old.location_label}：{len(old.groups)}组、{len(old.categories)}个小项的真实POI起点1000米步行服务区重算。'
        '设施、中心100米等时圈及数量评分沿用保存报告；包含圈外可服务候选，未完成的区域保留未知。')
    report = report_with_display_policy(CheckupReport.model_validate(report.model_dump(mode='json')))
    return CheckupJob(id=report_id, status='completed', stage='completed', progress=100, result=report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--id', default='tongji-education-health-actual-poi-20261025')
    parser.add_argument('--samples', type=Path, default=ROOT.parents[1] / 'outputs/真实POI服务区_2026-10-25/真实起点缓存恢复.json')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='explicitly allow at most 450 paid route HTTP requests / 20 active minutes')
    mode.add_argument('--offline', action='store_true', help='rebuild the saved actual-origin distance contour with no provider calls')
    args = parser.parse_args(argv)
    args.source = args.source or ('tongji-education-health-actual-poi-20261025' if args.offline
                                 else 'tongji-education-health-shared500-20261024')
    if any(not value or not all(c.isalnum() or c in '-_' for c in value) for value in (args.id, args.source)):
        parser.error('invalid report id')
    reports = ROOT / 'data/reports'
    destination = reports / f'{args.id}_report.json'
    if destination.exists():
        parser.error('destination exists; choose a fresh report id')
    source_path = reports / f'{args.source}_report.json'
    source = CheckupJob.model_validate_json(source_path.read_text())
    if source.result is None:
        parser.error('source has no completed report')
    if args.offline:
        sample_cache, cache_info = saved_actual_sample_cache(source.result)
        cache_info['sha256'] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        def forbidden(_):
            raise AssertionError('Offline contour rebuild cannot call a provider')
        result = recompute_actual_case(source.model_dump(mode='json'), forbidden, report_id=args.id,
            sample_cache=sample_cache, cache_info=cache_info, max_route_pairs=0)
        result.result.quality.update(coverage_replay_only=True, coverage_budget_exhausted=False,
            coverage_stop_reason='SAVED_SAMPLES_ONLY', provider_budget_exhausted=False,
            coverage_http_budget_exhausted=False, coverage_pair_budget_exhausted=False,
            source_actual_http_requests=source.result.quality.get('new_http_requests', 0))
        result.result.warnings = [warning for warning in result.result.warnings if warning.code != 'ROUTING_BUDGET_EXCEEDED']
        result.result.warnings.append(WarningItem(code='SAVED_ROUTE_SAMPLES_ONLY', message=(
            '仅复用已保存的真实步行测距生成1000米等距服务区，没有新增地图请求；'
            '未测范围保持未知，采样范围外沿不代表已经测得的1000米边界。')))
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(result.model_dump_json(indent=2)+'\n')
        temporary.replace(destination)
        print(json.dumps(dict(saved=str(destination), new_http_requests=0,
            quality={k:v for k,v in result.result.quality.items() if not isinstance(v,(dict,list))}),ensure_ascii=False))
        return 0
    from tools.dev import load_env
    from app.modules.routing.service import RoutingService
    from app.providers.baidu.routing import BaiduRoutingProvider
    from app.providers.baidu.direction import BaiduDirectionProvider
    load_env(ROOT / '.env')
    key = os.environ.get('BAIDU_MAP_AK', '').strip()
    if not key:
        parser.error('server-side map key is not configured')
    progress_path = ROOT / 'data/progress' / f'{args.id}_progress.json'
    request_path = progress_path.with_suffix('.requests.json')
    log_path = progress_path.with_suffix('.log')
    if destination.exists() or progress_path.exists() or request_path.exists() or log_path.exists():
        parser.error('destination or prior run accounting exists; choose a fresh report id')
    source = CheckupJob.model_validate_json((reports / f'{args.source}_report.json').read_text())
    if source.result is None:
        parser.error('source has no completed report')
    sample_cache, cache_info = load_actual_sample_cache(args.samples, source.result)
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    cache = ROOT / 'data/cache/actual-poi'
    cache.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    calls = hits = 0
    last_call = 0.
    phase = 'coverage'
    percent = 0

    def atomic_json(path, value):
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(path)

    def publish(**extra):
        state = dict(report_id=args.id, phase=phase, percent=percent, provider_requests=calls,
            new_http_requests=calls, provider_cache_hits=hits, elapsed_seconds=round(time.monotonic()-started, 1), **extra)
        atomic_json(progress_path, state)
        line = json.dumps(state, ensure_ascii=False)
        with log_path.open('a') as log:
            log.write(line + '\n')
        print(line, flush=True)

    def transport(url, timeout):
        nonlocal calls, hits, last_call
        parts = urlsplit(url)
        public = parts.path + '?' + urlencode([(name, value) for name, value in parse_qsl(parts.query) if name != 'ak'])
        filename = hashlib.sha256(public.encode()).hexdigest() + '.json'
        for folder in (cache, ROOT / 'data/cache/shared500', ROOT / 'data/cache/coverage-20m'):
            path = folder / filename
            if path.exists():
                hits += 1
                return json.loads(path.read_text())
        if calls >= HTTP_LIMIT or time.monotonic()-started >= SECONDS_LIMIT:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', '450 HTTP requests / 20 active minutes reached')
        time.sleep(max(0, 1.5-(time.monotonic()-last_call)))
        remaining = SECONDS_LIMIT-(time.monotonic()-started)
        if remaining <= 0:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', '20 active minutes reached')
        last_call = time.monotonic()
        calls += 1
        atomic_json(request_path, dict(provider_requests=calls, provider_request_limit=HTTP_LIMIT,
            elapsed_seconds=round(time.monotonic()-started, 1)))
        with urlopen(url, timeout=min(timeout, remaining)) as response:
            data = json.load(response)
        if data.get('status') == 0:
            atomic_json(cache / filename, data)
        if calls % 10 == 0:
            publish()
        return data

    routing = RoutingService(BaiduRoutingProvider(key, transport=transport), batch_size=25,
        max_retries=1, retry_delay_seconds=2, fallback_provider=BaiduDirectionProvider(key, transport=transport))
    atomic_json(request_path, dict(provider_requests=0, provider_request_limit=HTTP_LIMIT, elapsed_seconds=0))
    publish(source_report_id=source.id, sample_cache=cache_info, center_route_requests=0, poi_search_requests=0)

    def progress(value):
        nonlocal percent
        value = int(value*100)
        if value != percent:
            percent = value
            publish()
    try:
        result = recompute_actual_case(source.model_dump(mode='json'), routing.route, report_id=args.id,
            sample_cache=sample_cache, cache_info=cache_info, progress=progress)
        elapsed = time.monotonic() - started
        http_budget_exhausted = calls >= HTTP_LIMIT or elapsed >= SECONDS_LIMIT
        result.result.quality.update(provider_requests=calls, new_http_requests=calls, provider_cache_hits=hits,
            provider_request_limit=HTTP_LIMIT, provider_budget_exhausted=http_budget_exhausted,
            coverage_http_budget_exhausted=http_budget_exhausted,
            coverage_pair_budget_exhausted=bool(result.result.quality.get('coverage_budget_exhausted')),
            elapsed_seconds=round(elapsed, 1))
        result = CheckupJob.model_validate(result.model_dump(mode='json'))
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(result.model_dump_json(indent=2) + '\n')
        temporary.replace(destination)
        phase, percent = 'completed', 100
        publish(saved=str(destination), score=result.result.score,
            aggregate={name:value for name,value in result.result.blindspot['total_coverage'].items()
                       if value is None or isinstance(value, (str, int, float, bool))})
        return 0
    except Exception as exc:
        phase = 'failed'
        publish(error_type=type(exc).__name__, error='Coverage recomputation failed; source reports remain unchanged')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
