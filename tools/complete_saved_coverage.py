"""Finish a saved area's selected coverage, with durable per-round evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.schemas.checkup import CheckupJob
from tools.bounded_baidu import BoundedBaiduTransport, _atomic_json
from tools.cached_route_reuse import CacheFirstRouting
from tools.continue_tongji_coverage import continue_saved_report, fill_group_gaps
from tools.execute_two_locations import credentials, routing_for


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-http', type=int, default=10000)
    parser.add_argument('--max-seconds', type=int, default=14400)
    parser.add_argument('--initial-pairs', type=int, default=400)
    parser.add_argument('--reuse-http-cache', action='append', default=[])
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('--live is required to use the configured map credentials')
    if (not all(re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value)
                for value in (args.source,args.id,*args.reuse_http_cache))
            or args.source==args.id or args.max_http<1 or args.max_seconds<1 or not 0<=args.initial_pairs<=50000):
        parser.error('Use fresh safe identities and positive execution limits')
    reports=ROOT/'data/reports'
    state=ROOT/'data/progress'/f'{args.id}_execution.json'
    ledger=ROOT/'data/progress'/f'{args.id}_requests.json'
    destination=reports/f'{args.id}_report.json'
    if any(path.exists() for path in (state,ledger,destination)):
        parser.error('Refusing to replace earlier reports or request accounting')
    source=CheckupJob.model_validate_json((reports/f'{args.source}_report.json').read_text())
    dirs=[ROOT/'data/cache'/args.id, *[ROOT/'data/cache'/value for value in args.reuse_http_cache],
          ROOT/'data/cache/actual-poi',
          ROOT/'data/cache/shared500', ROOT/'data/cache/coverage-20m']
    pairs=ROOT/'data/cache'/f'{args.id}-pairs'
    transport=BoundedBaiduTransport(credentials(),dirs,ledger,max_requests=args.max_http,
        max_seconds=args.max_seconds,min_interval=.36,route_pair_qps=3)
    wrapper=CacheFirstRouting(routing_for(transport).route,dirs,pairs,request_batch_size=3)

    def publish(**extra):
        value=dict(report_id=args.id,updated_at=datetime.now(timezone.utc).isoformat(),
            http=transport.snapshot(),route_cache_reuse=wrapper.snapshot(),**extra)
        _atomic_json(state,value)
        print(json.dumps(value,ensure_ascii=False),flush=True)

    def save(job,phase):
        report=job.result
        stats=transport.snapshot()
        report.quality.update(provider_requests=stats['requests'],new_http_requests=stats['requests'],
            provider_request_limit=args.max_http,provider_active_time_limit_s=args.max_seconds,
            credential_accounting=stats,route_cache_reuse=wrapper.snapshot(),
            acquisition_source_report_id=args.source,new_poi_search_requests=0,new_center_route_requests=0)
        path=reports/f'{job.id}_report.json'
        if path.exists():raise FileExistsError('Refusing to replace an earlier coverage round')
        _atomic_json(path,job.model_dump(mode='json'))
        publish(phase=phase,saved_report_id=job.id,
            groups={gid:{k:row[k] for k in ('covered_ratio','blind_ratio','unknown_ratio')}
                for gid,row in report.blindspot['group_coverage'].items()},
            categories={row.facility_id:{k:getattr(row,k) for k in ('covered_ratio','blind_ratio','unknown_ratio')}
                for row in report.categories},
            validation={k:v for k,v in report.quality['coverage_validation'].items() if k!='groups'})

    publish(phase='inside_poi_initial_samples')
    current=continue_saved_report(source,wrapper.route,report_id=args.id+'-initial',max_route_pairs=args.initial_pairs,
        route_pair_cache_dir=pairs,progress=lambda p:publish(phase='inside_poi_initial_samples',fraction=round(p,3)))
    save(current,'initial_saved')
    for scope in ('groups','categories'):
        for number in range(1,201):
            report=current.result
            incomplete=(not report.quality['coverage_validation']['evidence_complete'] if scope=='groups'
                else any(row.unknown_ratio>1e-8 for row in report.categories))
            if not incomplete:break
            publish(phase=scope+'_gap_samples',round=number)
            next_job=fill_group_gaps(current,wrapper.route,report_id=f'{args.id}-{scope}-{number}',
                max_route_pairs=2000,route_pair_cache_dir=pairs,scope=scope,
                progress=lambda p:publish(phase=scope+'_gap_samples',round=number,fraction=round(p,3)))
            save(next_job,scope+'_saved')
            current=next_job
            targeting=current.result.quality['coverage_gap_targeting']
            if targeting['stop_reason'] or targeting['planned_pairs']==0:
                break
    report=current.result
    if (not report.quality['coverage_validation']['evidence_complete']
            or any(row.unknown_ratio>1e-8 for row in report.categories)):
        publish(phase='incomplete',saved_report_id=current.id)
        return 2
    final=CheckupJob(id=args.id,status='completed',stage='completed',progress=100,
        result=report.model_copy(deep=True,update={'report_id':args.id}))
    final.result.quality.update(coverage_completed_selected_categories=True,
        coverage_completed_from=current.id,coverage_publication_status='verified_estimate')
    save(final,'completed')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
