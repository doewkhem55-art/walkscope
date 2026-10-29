"""Resume the registered 45-category report; preserve real data and cache requests.

No keys in code/logs. Existing stage registration validates all reused evidence.
The one live supplement is bounded to 450 HTTP attempts / 20 minutes.
"""
import argparse
from collections import Counter
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
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog, _CATALOG_PATH
from app.modules.checkup.poi import search_catalog
from app.modules.checkup.service import run_checkup
from app.modules.checkup.snapshots import load_stages
from app.modules.location.service import resolve_location
from app.modules.routing.service import RoutingService, _summary
from app.providers.baidu.poi import BaiduPOIProvider, POIProviderError
from app.providers.baidu.routing import BaiduRoutingProvider
from app.providers.baidu.direction import BaiduDirectionProvider
from app.schemas.checkup import CheckupJob, CheckupRequest
from app.schemas.common import Meta, CenterPoint
from app.schemas.location import LocationRequest
from app.schemas.routing import RoutingResult, RoutingSuccess, RoutingData, RouteStatus, RouteResult


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, path)


def cache_identity(url):
    parts = urlsplit(url)
    params = sorted((k, v) for k, v in parse_qsl(parts.query) if k != 'ak')
    identity = parts.netloc + parts.path + '?' + urlencode(params)
    return hashlib.sha256(identity.encode()).hexdigest(), parts.path, dict(params)


def subdivisions(center, radius):
    """Four overlapping disks cover the parent disk, including its cardinal edges."""
    cosine = math.cos(math.radians(center.lat))
    child_radius = math.ceil(radius / math.sqrt(2)) + 5
    return [(CenterPoint(lng=center.lng + math.degrees(x * radius / 2 / (6371008.8 * cosine)),
                         lat=center.lat + math.degrees(y * radius / 2 / 6371008.8), crs='BD09LL'), child_radius)
            for x, y in ((-1,-1), (-1,1), (1,-1), (1,1))]


class SubdividingPOIProvider:
    """Only split a query if the real provider reports its result cap."""
    def __init__(self, provider, *, max_depth=2):
        self.provider, self.max_depth = provider, max_depth

    def search(self, keyword, center, radius, depth=0):
        try:
            return self.provider.search(keyword, center, radius)
        except POIProviderError as error:
            if error.code != 'INCOMPLETE_RESULTS' or depth >= self.max_depth:
                raise
            gathered = list(error.partial_results or [])
        print('结果上限，分区补查：' + keyword + ' depth=' + str(depth+1), flush=True)
        for child, child_radius in subdivisions(center, radius):
            try:
                gathered.extend(self.search(keyword, child, child_radius, depth+1))
            except POIProviderError as error:
                gathered.extend(error.partial_results or [])
                raise POIProviderError(error.code, error.message, error.retryable, partial_results=gathered) from None
        # Discard records outside the original requested disk; subqueries overlap.
        cosine = math.cos(math.radians(center.lat))
        unique = {}
        for p in gathered:
            distance = math.hypot(math.radians(p.lng-center.lng)*6371008.8*cosine,
                                  math.radians(p.lat-center.lat)*6371008.8)
            if distance <= radius + 0.01:
                unique.setdefault(p.uid or (p.name,p.lng,p.lat),p)
        return list(unique.values())


class Transport:
    def __init__(self, out, *, max_http=450, max_seconds=1200, interval=1.8):
        self.out, self.max_http, self.max_seconds = out, max_http, max_seconds
        self.interval, self.calls, self.hits, self.pairs = interval, 0, 0, 0
        self.started, self.last, self.stage = time.monotonic(), 0.0, 'poi'
        self.out.mkdir(parents=True, exist_ok=True)

    def __call__(self, url, timeout):
        identity, endpoint, params = cache_identity(url)
        path = self.out / 'http_cache' / (identity + '.json')
        if path.exists():
            cached = json.loads(path.read_text())
            if cached.get('status') == 0:
                self.hits += 1
                return cached
        if self.calls >= self.max_http or time.monotonic() - self.started >= self.max_seconds:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED', '本轮补齐达到450次请求或20分钟保护，已保存缓存。')
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        self.calls += 1
        pairs = len(params.get('destinations', '').split('|')) if endpoint == '/routematrix/v2/walking' else int(endpoint == '/directionlite/v1/walking')
        self.pairs += pairs
        event = dict(sequence=self.calls, stage=self.stage, endpoint=endpoint, cache_id=identity,
                     route_pairs=pairs, started_at=datetime.now(timezone.utc).isoformat())
        started = time.monotonic()
        try:
            with urlopen(url, timeout=timeout) as response:
                payload = json.load(response)
            event['provider_status'] = payload.get('status')
            if payload.get('status') == 0:
                save_json(path, payload)
            return payload
        except Exception as exc:
            event['exception_type'] = type(exc).__name__
            raise RuntimeError('百度请求未完成：' + type(exc).__name__) from None
        finally:
            event['elapsed_s'] = round(time.monotonic() - started, 3)
            with (self.out / 'provider_events.jsonl').open('a') as f:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')


def cached_center_route(old, live_route):
    stored = {r.target_id: r for r in old.routes if r.status in {RouteStatus.SUCCESS, RouteStatus.NO_ROUTE}}
    reused = set()

    def route(request):
        if request.origin != old.center:
            return live_route(request)
        known = {t.target_id: stored[t.target_id] for t in request.targets
                 if t.target_id in stored and stored[t.target_id].location == t.location}
        cached_ids = set(known)
        missing = [t for t in request.targets if t.target_id not in known]
        warnings = []
        if missing:
            result = live_route(request.model_copy(update={'targets': missing})).root
            warnings = result.warnings
            if result.ok:
                known.update((r.target_id, r) for r in result.data.routes)
            else:
                known.update((t.target_id, RouteResult(target_id=t.target_id, location=t.location, status='unavailable')) for t in missing)
        reused.update(cached_ids)
        routes = [known[t.target_id] for t in request.targets]
        return RoutingResult(root=RoutingSuccess(ok=True, data=RoutingData(
            origin=request.origin, travel_mode=request.travel_mode, routes=routes, summary=_summary(routes)),
            warnings=warnings, meta=Meta(schema_version='1.0', provider='baidu')))

    return route, reused


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace
    out = root / 'outputs/生活圈补齐_2026-10-21'
    prior_file = root / 'outputs/生活圈执行_2026-10-20/reports/ef5f4c75a0f6449786241fd099e743af_report.json'
    old_job = CheckupJob.model_validate_json(prior_file.read_text())
    old = old_job.result
    catalog = load_catalog()
    keys = json.loads((root / '.local/baidu_keys.json').read_text())
    ak = keys['AK_A']
    request = CheckupRequest(location={'type': 'coordinate', **old.center.model_dump(mode='json')})
    transport = Transport(out)
    provider = SubdividingPOIProvider(BaiduPOIProvider(ak=ak, http_get=transport))
    current = out / 'categories_checkpoint.json'
    rows = json.loads(current.read_text()) if current.exists() else {}
    old_pois = {f['id']: [p.model_dump(mode='json') for p in old.pois if p.facility_id == f['id']] for f in catalog['facilities']}
    reused_categories = []
    for category in old.categories:
        if category.search_status == 'complete':
            reused_categories.append(category.facility_id)
            rows.setdefault(category.facility_id, dict(facility_id=category.facility_id, name_zh=category.name_zh,
                group_id=category.group_id, search_status='complete', count=category.count,
                known_count=category.known_count, pois=old_pois[category.facility_id], warnings=[],
                captured_at=None, source_report_id=old.report_id))
    save_json(current, rows)
    for f in catalog['facilities']:
        fid = f['id']
        if fid in rows and rows[fid]['search_status'] == 'complete':
            continue
        print('补查类别：' + f['name_zh'], flush=True)
        fresh = search_catalog(provider, old.center, old.isochrone.geometry, old.search_radius_m, facilities=[f])[0]
        if fresh['search_status'] != 'complete':
            unique = {p['poi_id']: p for p in old_pois[fid]}
            unique.update((p['poi_id'], p) for p in fresh['pois'])
            fresh['pois'] = list(unique.values())
            fresh['known_count'] = sum(p['in_isochrone'] for p in fresh['pois'])
            if unique:
                fresh['search_status'] = 'partial'
        fresh['captured_at'] = datetime.now(timezone.utc).isoformat()
        rows[fid] = fresh
        save_json(current, rows)
        print(json.dumps(dict(category=f['name_zh'], status=fresh['search_status'], known=fresh['known_count'],
                              http=transport.calls, cache_hits=transport.hits), ensure_ascii=False), flush=True)
        if any(w['code'] == 'POI_DAILY_QUOTA_EXHAUSTED' for w in fresh['warnings']):
            break
    for f in catalog['facilities']:
        rows.setdefault(f['id'], dict(facility_id=f['id'], name_zh=f['name_zh'], group_id=f['group_id'],
            search_status='failed', count=None, known_count=0, pois=old_pois[f['id']], warnings=[], captured_at=None))
    stages = out / 'stages'
    location = resolve_location(LocationRequest(schema_version='1.0', input=request.location))
    save_json(stages / 'gate1.json', dict(captured_at=None, source_report_id=old.report_id,
        location=location.model_dump(mode='json'), isochrone=dict(ok=True, data=old.isochrone.model_dump(mode='json'),
        warnings=[], meta=dict(schema_version='1.0', provider='baidu'))))
    gate1_sha = hashlib.sha256((stages / 'gate1.json').read_bytes()).hexdigest()
    save_json(stages / 'gate2.json', dict(center=old.center.model_dump(mode='json'), catalog_version=catalog['version'],
        gate1_file='gate1.json', gate1_sha256=gate1_sha, search_radius_m=old.search_radius_m,
        captured_at=datetime.now(timezone.utc).isoformat(), categories=[rows[f['id']] for f in catalog['facilities']]))
    normalized = request.location.model_dump(mode='json')
    manifest = dict(source='baidu-live-staged', input=normalized,
        input_sha256=hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        catalog_version=catalog['version'], catalog_sha256=hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest())
    for stage in ('gate1', 'gate2'):
        manifest[stage] = dict(path=stage + '.json', sha256=hashlib.sha256((stages / (stage + '.json')).read_bytes()).hexdigest())
    save_json(stages / 'manifest.json', manifest)
    load_stages(stages / 'manifest.json', request.location, catalog)
    print('45类阶段数据身份校验通过，开始补充路线与覆盖。', flush=True)
    routing = RoutingService(BaiduRoutingProvider(ak, transport=transport), batch_size=25, max_retries=1,
        retry_delay_seconds=2, fallback_provider=BaiduDirectionProvider(ak, transport=transport))
    route, reused_center = cached_center_route(old, routing.route)
    report_id = uuid.uuid4().hex
    def progress(stage, value):
        transport.stage = stage
        record = dict(report_id=report_id, stage=stage, progress=value, http=transport.calls,
                      cache_hits=transport.hits, time_utc=datetime.now(timezone.utc).isoformat())
        save_json(out / 'progress.json', record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    report = run_checkup(request, report_id=report_id, resolve_location=None, generate_isochrone=None,
        poi_provider=None, route=route, stage_manifest=stages / 'manifest.json', progress=progress)
    report.quality.update(resume_source_report_id=old.report_id,
        resume_source_sha256=hashlib.sha256(prior_file.read_bytes()).hexdigest(),
        reused_complete_category_ids=reused_categories, reused_center_routes=len(reused_center),
        provider_requests=transport.calls, provider_cache_hits=transport.hits, route_pairs_attempted=transport.pairs,
        provider_request_limit=450, provider_budget_exhausted=report.quality.get('coverage_stop_reason')=='PROVIDER_BUDGET_EXHAUSTED',
        resume_elapsed_s=round(time.monotonic()-transport.started,3), server_key_alias='AK_A')
    job = CheckupJob(id=report_id, status='completed', stage='completed', progress=100, result=report)
    save_json(out / 'reports' / (report_id+'_report.json'), job.model_dump(mode='json'))
    save_json(out / 'latest_result.json', dict(report_id=report_id, statuses=dict(Counter(c.search_status for c in report.categories)),
        score=report.score, provisional=report.score_provisional, provider_requests=transport.calls,
        cache_hits=transport.hits, reused_center_routes=len(reused_center), quality=report.quality))
    print('RESULT ' + json.dumps(dict(report_id=report_id, score=report.score,
        statuses=dict(Counter(c.search_status for c in report.categories)), provider_requests=transport.calls), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('STOPPED ' + type(exc).__name__ + ': ' + (exc.code if isinstance(exc, ProviderStopError) else 'local validation or computation failed; saved cache retained'), flush=True)
        raise SystemExit(1) from None
