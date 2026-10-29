"""Execute the approved two-location plan with durable, bounded Baidu calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import CheckupError, run_checkup
from app.modules.isochrone.grid import generate_grid_isochrone
from app.modules.location.service import resolve_location
from app.modules.routing.service import RoutingService
from app.providers.baidu.direction import BaiduDirectionProvider
from app.providers.baidu.poi import BaiduPOIProvider
from app.providers.baidu.routing import BaiduRoutingProvider
from app.schemas.checkup import CheckupJob, CheckupRequest
from app.schemas.common import CenterPoint
from app.schemas.isochrone import IsochroneResult
from tools.bounded_baidu import BoundedBaiduTransport
from tools.cached_route_reuse import CacheFirstRouting
from tools.prepare_actual_group import prepare_group
from tools.recompute_actual_poi_case import load_actual_sample_cache, recompute_actual_case

CURRENT = 'tongji-education-health-walk-contour-20261025'
ORIGINAL = '3de7777d550f4562afc38d96140f0118'
GROUPS = [
    ('elderly_disability', 'elderly'), ('daily_commerce', 'commerce'),
    ('culture_sports_green', 'culture'), ('transport_municipal', 'transport'),
    ('community_safety', 'community'),
]
HARBIN = 'harbin-nangang-earthquake-education-walk-contour-20261025'
REPORTS = ROOT / 'data/reports'
PROGRESS = ROOT / 'data/progress'


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def read_job(report_id):
    return CheckupJob.model_validate_json((REPORTS / f'{report_id}_report.json').read_text())


def credentials():
    rows = json.loads((ROOT / '.local/baidu-keys.json').read_text())
    probes = json.loads((ROOT / '.local/key-probes.json').read_text())
    allowed = {p['alias'] for p in probes if p.get('status') == 0}
    result = [row for row in rows if row['alias'] in allowed]
    if not result:
        raise ValueError('No validated server credential is configured')
    return result


def transport_for(report_id):
    ledger = PROGRESS / f'{report_id}_requests.json'
    if ledger.exists():
        raise FileExistsError('Prior run accounting exists; preserve it and use a fresh report id')
    # The authenticated quota page confirms 3 QPS for the Harbin-preferred
    # account. Keep requests sequential and below 2 QPS in those rounds.
    interval = 0.55 if report_id.startswith(HARBIN) else 1.5
    return BoundedBaiduTransport(credentials(), [ROOT / 'data/cache/actual-poi',
        ROOT / 'data/cache/shared500', ROOT / 'data/cache/coverage-20m'], ledger,
        min_interval=interval)


def routing_for(transport):
    key = credentials()[0]['value']
    return RoutingService(BaiduRoutingProvider(key, transport=transport), batch_size=25,
        max_retries=1, retry_delay_seconds=2,
        fallback_provider=BaiduDirectionProvider(key, transport=transport))


def publish(report_id, **data):
    state = dict(report_id=report_id, updated_at=datetime.now(timezone.utc).isoformat(), **data)
    atomic(PROGRESS / f'{report_id}_execution.json', state)
    print(json.dumps(state, ensure_ascii=False), flush=True)


def finish(job, transport, *, accounting_id=None, **quality):
    report = job.result
    if report is None:
        raise ValueError('No final report to persist')
    stats = transport.snapshot()
    report.quality.update(provider_requests=stats['requests'], new_http_requests=stats['requests'],
        provider_cache_hits=stats['cache_hits'], provider_request_limit=450,
        elapsed_seconds=stats['elapsed'], credential_accounting=stats,
        provider_budget_exhausted=stats['requests'] >= 450 or stats['elapsed'] >= 1200,
        **quality)
    validated = CheckupJob.model_validate(job.model_dump(mode='json'))
    output = REPORTS / f'{validated.id}_report.json'
    if output.exists():
        raise FileExistsError('Refusing to replace a saved report')
    atomic(output, validated.model_dump(mode='json'))
    total = report.blindspot['total_coverage']
    publish(accounting_id or job.id, final_report_id=job.id,
        phase='completed', score=report.score, http=stats,
        phase_http_requests=quality.get('phase_http_requests', {}),
        coverage={k: total.get(k) for k in ('covered_ratio', 'blind_ratio', 'unknown_ratio', 'display_blind_ratio')},
        stop_reason=report.quality.get('coverage_stop_reason'), saved=output.name,
        **({'route_cache_reuse': quality['route_cache_reuse']} if 'route_cache_reuse' in quality else {}))


def dry_run():
    original, current = read_job(ORIGINAL), read_job(CURRENT)
    for group, slug in GROUPS:
        prepared = prepare_group(original, current.result.isochrone, group, f'dry-{slug}')
        report = prepared.result
        print(json.dumps(dict(group=group, categories=len(report.categories),
            poi_records=len(report.pois), score=report.score,
            required_radius_m=report.quality['required_search_radius_m'],
            inherited_radius_m=report.search_radius_m), ensure_ascii=False))


def run_tongji():
    original, current = read_job(ORIGINAL), read_job(CURRENT)
    samples = ROOT.parent / 'evidence/真实POI服务区_2026-10-25/真实起点缓存恢复.json'
    for group, slug in GROUPS:
        report_id = f'tongji-{slug}-walk-contour-20261025'
        if (REPORTS / f'{report_id}_report.json').exists():
            saved = read_job(report_id)
            if saved.result.center != current.result.center:
                raise ValueError('Saved output centre differs')
            print(json.dumps(dict(report_id=report_id, already_saved=True)), flush=True)
            continue
        prepared = prepare_group(original, current.result.isochrone, group, report_id + '-source')
        atomic(ROOT / '.local' / f'{prepared.id}.json', prepared.model_dump(mode='json'))
        sample_cache, cache_info = load_actual_sample_cache(samples, prepared.result)
        transport = transport_for(report_id)
        routing = routing_for(transport)
        publish(report_id, phase='coverage', group=group,
            categories=len(prepared.result.categories), reused_samples=cache_info.get('available_samples'))
        result = recompute_actual_case(prepared.model_dump(mode='json'), routing.route,
            report_id=report_id, sample_cache=sample_cache, cache_info=cache_info,
            progress=lambda value: publish(report_id, phase='coverage', fraction=round(value, 3),
                                          http=transport.snapshot()))
        result.result.location_label = f'同济大学（四平路校区）· {prepared.result.groups[0].name_zh}'
        result.result.quality.update(coverage_computed=True, coverage_status='measured_estimate')
        finish(result, transport, source_historical_poi_report=ORIGINAL,
               source_center_isochrone_report=CURRENT, new_poi_search_requests=0,
               new_center_route_requests=0)


def harbin_match():
    probes = json.loads((ROOT / '.local/key-probes.json').read_text())
    matches = [p for p in probes if p.get('status') == 0 and p.get('poi_infos')]
    if not matches:
        raise ValueError('No validated Harbin address match')
    poi = matches[0]['poi_infos'][0]
    if (len(matches[0]['poi_infos']) != 1 or poi.get('city') != '哈尔滨市'
            or poi.get('district') != '南岗区' or poi.get('name') != '地震局家属楼'
            or matches[0]['result'].get('precise') != 1):
        raise ValueError('Harbin address requires disambiguation before routing')
    return poi


def safe_error(exc):
    data = dict(error_type=type(exc).__name__)
    if isinstance(exc, CheckupError) and isinstance(exc.code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', exc.code):
        data['error_code'] = exc.code
    return data


def prior_harbin_accounting(accounting_id):
    paths = sorted({PROGRESS / f'{HARBIN}_execution.json',
                    *PROGRESS.glob(f'{HARBIN}-wave*_execution.json')})
    prior = []
    for path in paths:
        previous_id = path.name.removesuffix('_execution.json')
        if previous_id == accounting_id or not path.exists():
            continue
        state = json.loads(path.read_text())
        prior.append(dict(accounting_id=previous_id,
            **{key: state[key] for key in ('phase', 'failed', 'error_type', 'error_code',
                'http', 'phase_http_requests', 'route_cache_reuse') if key in state}))
    return prior


def run_harbin(accounting_id=None):
    accounting_id = HARBIN if accounting_id is None else accounting_id
    if (not isinstance(accounting_id, str) or not re.fullmatch(
            re.escape(HARBIN) + r'(?:-wave[A-Za-z0-9][A-Za-z0-9_.-]{0,39})?', accounting_id)):
        raise ValueError('Use the Harbin report id or a safe Harbin wave id')
    if (REPORTS / f'{HARBIN}_report.json').exists():
        print(json.dumps(dict(report_id=HARBIN, already_saved=True)), flush=True)
        return
    if any((PROGRESS / f'{accounting_id}_{suffix}.json').exists()
           for suffix in ('requests', 'execution', 'partial_evidence')):
        raise FileExistsError('Prior run evidence exists; preserve it and use a fresh accounting id')
    transport = None
    isochrone_routing = service_routing = None

    def route_cache_snapshot():
        return dict(isochrone=isochrone_routing.snapshot() if isochrone_routing is not None else None,
                    service=service_routing.snapshot() if service_routing is not None else None)

    try:
        prior = prior_harbin_accounting(accounting_id)
        poi = harbin_match()
        center = CenterPoint(**poi['location'], crs='BD09LL')
        facility_ids = [f['id'] for f in load_catalog()['facilities'] if f['group_id'] == 'education_children']
        request = CheckupRequest(location=dict(type='coordinate', **center.model_dump()), facility_ids=facility_ids)
        transport = transport_for(accounting_id)
        routing = routing_for(transport)
        cache_dirs = [ROOT / 'data/cache/actual-poi', ROOT / 'data/cache/shared500', ROOT / 'data/cache/coverage-20m']
        isochrone_routing = CacheFirstRouting(routing.route, cache_dirs,
            ROOT / 'data/cache/harbin-isochrone-route-pairs', observation_policy='isochrone_conservative')
        service_routing = CacheFirstRouting(routing.route, cache_dirs,
            ROOT / 'data/cache/harbin-service-route-pairs', observation_policy='strict')
        key = credentials()[0]['value']
    except Exception as exc:
        publish(accounting_id, final_report_id=HARBIN, failed=True, phase='initializing',
            http=transport.snapshot() if transport is not None else None,
            phase_http_requests={'initializing': transport.requests if transport is not None else 0},
            route_cache_reuse=route_cache_snapshot(),
            **safe_error(exc))
        raise
    phase = ''; phase_started = 0; phase_requests = {}

    def phase_counts():
        counts = dict(phase_requests)
        if phase:
            counts[phase] = counts.get(phase, 0) + transport.requests - phase_started
        return counts

    def progress(stage, value):
        nonlocal phase, phase_started
        if stage != phase:
            if phase:
                phase_requests[phase] = phase_requests.get(phase, 0) + transport.requests - phase_started
            phase, phase_started = stage, transport.requests
        publish(accounting_id, final_report_id=HARBIN, phase=stage, percent=value,
            http=transport.snapshot(), phase_http_requests=phase_counts(), route_cache_reuse=route_cache_snapshot())

    def isochrone(iso_request):
        saved_path = ROOT / 'data/cache/harbin-center-100m-20261025.json'
        if saved_path.exists():
            saved = IsochroneResult.model_validate_json(saved_path.read_text())
            if (not saved.root.ok or saved.root.data.center != center
                    or saved.root.data.time_limit_s != 900 or saved.root.data.quality.grid_cell_m != 100):
                raise ValueError('Saved Harbin circle is incompatible')
            return saved
        result = generate_grid_isochrone(isochrone_routing.route, iso_request, cell_m=100,
            max_grid_points=2500, include_tiers=True)
        if result.root.ok:
            atomic(saved_path, result.model_dump(mode='json'))
        return result

    try:
        result = run_checkup(request, report_id=HARBIN, resolve_location=resolve_location,
            generate_isochrone=isochrone, poi_provider=BaiduPOIProvider(ak=key, http_get=transport),
            route=service_routing.route, progress=progress,
            on_update=lambda snapshot: atomic(PROGRESS / f'{accounting_id}_partial_evidence.json', snapshot))
        counts = phase_counts()
        cumulative = dict(counts)
        for attempt in prior:
            for stage, count in attempt.get('phase_http_requests', {}).items():
                cumulative[stage] = cumulative.get(stage, 0) + count
        result.location_label = '哈尔滨市南岗区地震局家属楼（学府路副29-13）'
        job = CheckupJob(id=HARBIN, status='completed', stage='completed', progress=100, result=result)
        finish(job, transport, accounting_id=accounting_id, location_verified=poi,
            phase_http_requests=counts, prior_attempt_accounting=prior,
            route_cache_reuse=route_cache_snapshot(),
            cumulative_new_http_requests=transport.requests + sum(
                (attempt.get('http') or {}).get('requests', 0) for attempt in prior),
            cumulative_phase_http_requests=cumulative,
            new_poi_search_requests=counts.get('poi', 0),
            new_isochrone_requests=counts.get('isochrone', 0),
            new_center_route_requests=counts.get('routing', 0),
            new_coverage_requests=counts.get('coverage', 0),
            isochrone_grid_cell_m=100)
    except Exception as exc:
        publish(accounting_id, final_report_id=HARBIN, failed=True, phase=phase,
            http=transport.snapshot(), phase_http_requests=phase_counts(),
            route_cache_reuse=route_cache_snapshot(), **safe_error(exc))
        raise


def merge_tongji():
    from tools.merge_saved_checkups import merge_reports
    report_id = 'tongji-all7-walk-contour-20261025'
    if (REPORTS / f'{report_id}_report.json').exists():
        raise FileExistsError('Combined output exists')
    jobs = [read_job(CURRENT)] + [read_job(f'tongji-{slug}-walk-contour-20261025') for _, slug in GROUPS]
    merged = merge_reports(jobs, report_id, preserve_measurement_variants=True)
    if len(merged.result.groups) != 7 or len(merged.result.categories) != 45:
        raise ValueError('Incomplete seven-group merge')
    atomic(REPORTS / f'{report_id}_report.json', merged.model_dump(mode='json'))
    print(json.dumps(dict(report_id=report_id, score=merged.result.score, groups=7, categories=45), ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', choices=('dry-run', 'tongji', 'harbin', 'merge'))
    parser.add_argument('--accounting-id', help='Fresh Harbin wave id; the final report id stays fixed')
    args = parser.parse_args(argv)
    if args.accounting_id is not None and args.task != 'harbin':
        parser.error('--accounting-id is only supported for harbin')
    try:
        if args.task == 'harbin':
            run_harbin(accounting_id=args.accounting_id)
        else:
            {'dry-run': dry_run, 'tongji': run_tongji, 'merge': merge_tongji}[args.task]()
    except Exception as exc:
        # Provider exceptions can hold a credential-bearing URL; never print their repr/traceback.
        print(json.dumps(dict(task=args.task, failed=True, **safe_error(exc)), ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
