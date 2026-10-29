"""Continue a saved real-POI report with durable budgets and publication checks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
import math
from types import SimpleNamespace
from datetime import datetime, timezone

from shapely import make_valid
from shapely.geometry import Point, box, shape
from shapely.ops import transform, unary_union

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from app.schemas.checkup import CheckupJob
from app.schemas.common import CenterPoint
from app.schemas.routing import RoutingRequest
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import _projection
from app.modules.blindspot.service import _measure
from app.errors import ProviderStopError
from app.modules.checkup.representative_service_areas import build_actual_poi_origins
from app.modules.isochrone.geometry import geometry_to_local
from tools.recompute_actual_poi_case import recompute_actual_case, saved_actual_sample_cache, category_rows
from tools.bounded_baidu import BoundedBaiduTransport, _atomic_json
from tools.cached_route_reuse import CacheFirstRouting
from tools.execute_two_locations import credentials, routing_for


def recover_pair_samples(report, samples, folder):
    """Recover completed point pairs even if a previous process stopped mid-batch."""
    to_m,_=_projection(report.center)
    domain=geometry_to_local(report.isochrone.geometry,to_m)
    origins,_,_=build_actual_poi_origins(domain,category_rows(report),
        {f['id']:f for f in load_catalog()['facilities']},to_m)
    by_location={}
    for origin in origins:
        p=origin['location']
        by_location.setdefault((p.lng,p.lat,p.crs),[]).append(origin['id'])
    validator=CacheFirstRouting(None,[],folder)
    added=variants=0
    for path in Path(folder).glob('*.json'):
        raw=json.loads(path.read_text()); identity=raw.get('identity',{})
        location=identity.get('origin',{})
        ids=by_location.get((location.get('lng'),location.get('lat'),location.get('crs')),[])
        if not ids or identity.get('travel_mode')!='walking':
            continue
        origin=CenterPoint.model_validate(location)
        target=CenterPoint.model_validate(identity['destination'])
        request=RoutingRequest(schema_version='1.0',origin=origin,travel_mode='walking',
            targets=[dict(target_id='saved-pair',location=target)])
        validated_path,row=validator._pair(request,request.targets[0])
        if validated_path!=path or row is None:
            raise ValueError('Saved measured pair identity does not match its file')
        xy=tuple(round(v,6) for v in to_m(target.lng,target.lat))
        for oid in ids:
            entry=samples.setdefault(oid,dict(location=[origin.lng,origin.lat],samples={},failed_points=[]))
            if xy in entry['samples']:
                variants+=entry['samples'][xy]!=row.distance_m
            else:
                entry['samples'][xy]=float(row.distance_m);added+=1
    return dict(pair_cache_samples_added=added,pair_cache_variants_source_observation_retained=variants)


def continue_saved_report(source, route, *, report_id, max_route_pairs=5000, progress=lambda _:None,
                          route_pair_cache_dir=None):
    samples, info=saved_actual_sample_cache(source.result)
    if route_pair_cache_dir is not None:
        info.update(recover_pair_samples(source.result,samples,route_pair_cache_dir))
    for entry in samples.values():
        # A past quota/network failure is not evidence that a route is absent.
        # Retry it in a new bounded round, while every success stays reusable.
        entry['failed_points']=[]
    result=recompute_actual_case(source.model_dump(mode='json'),route,report_id=report_id,
        sample_cache=samples,cache_info=info,max_route_pairs=max_route_pairs,progress=progress)
    return finish_continuation(source,result)


def finish_continuation(source,result):
    report=result.result
    audit=report.quality['coverage_validation']
    report.quality.update(coverage_continued_from=source.id,
        coverage_publication_status='verified_estimate' if audit['evidence_complete'] else 'incomplete')
    for group in report.groups:
        record=report.blindspot['group_coverage'][group.id]
        group.summary=(f"真实POI起点1000米实际步行连续服务区估算；覆盖{record['covered_ratio']:.2%}，"
            f"未知{record['unknown_ratio']:.2%}。"+
            ('圈内POI均通过本组覆盖核验；' if audit['groups'][group.id]['poi_check_passed'] else '仍有圈内POI未通过服务区核验；')+
            '未知不能当作确认无服务，数量评分不代表覆盖率。')
    return CheckupJob.model_validate(result.model_dump(mode='json'))


def fill_group_gaps(source,route,*,report_id,max_route_pairs=5000,progress=lambda _:None,
                    route_pair_cache_dir=None,scope='groups'):
    """Measure missing coarse-mesh nodes near the remaining group-union gaps.

    One real origin proving service is sufficient to remove a common blind gap.
    Candidates are tried across saved rounds; a failed candidate never proves
    absence, and all resulting polygons still come from the measured mesh.
    """
    if report_id==source.id:
        raise ValueError('A new report identity is required')
    if scope not in {'groups','categories'}:
        raise ValueError('Gap scope must be groups or categories')
    report=source.result
    samples,info=saved_actual_sample_cache(report)
    if route_pair_cache_dir is not None:
        info.update(recover_pair_samples(report,samples,route_pair_cache_dir))
    to_m,to_center=_projection(report.center)
    domain=geometry_to_local(report.isochrone.geometry,to_m)
    origins,_,_=build_actual_poi_origins(domain,category_rows(report),
        {f['id']:f for f in load_catalog()['facilities']},to_m)
    planned={}
    records=list(report.blindspot.get('group_coverage',{}).values())
    if scope=='categories':
        records=[dict(facility_ids=[category.facility_id], unknown_areas=dict(
            type='FeatureCollection',features=[f for f in report.blindspot['unknown_areas']['features']
                if f['properties']['facility_type']==category.facility_id])) for category in report.categories]
    for record in records:
        gap=unary_union([make_valid(transform(to_m,shape(f['geometry'])))
            for f in record.get('unknown_areas',{}).get('features',[])]).intersection(domain)
        if gap.is_empty or gap.area<1e-6:
            continue
        ids=set(record['facility_ids'])
        choices=[o for o in origins if o['facility_ids'] & ids]
        minx,miny,maxx,maxy=gap.bounds
        for row in range(math.floor(miny/200),math.floor(maxy/200)+1):
            for col in range(math.floor(minx/200),math.floor(maxx/200)+1):
                a,b=col*200,row*200
                part=gap.intersection(box(a,b,a+200,b+200))
                if part.area<1e-6:
                    continue
                p=part.representative_point()
                nodes={(a,b),(a+200,b),(a+200,b+200),(a,b+200),(a+100,b+100)}
                for origin in sorted(choices,key=lambda o:Point(*o['xy']).distance(p)):
                    if Point(*origin['xy']).distance(part)>1000/math.cos(math.pi/512)+1e-6:
                        continue
                    entry=samples.setdefault(origin['id'],dict(location=[origin['location'].lng,
                        origin['location'].lat],samples={},failed_points=[]))
                    missing=nodes-entry['samples'].keys()
                    if missing:
                        planned.setdefault(origin['id'],dict(origin=origin,nodes=set()))['nodes'].update(missing)
                        break
    warnings=[]; submitted=failed=0; stop=None
    progress(.05)
    for oid,item in planned.items():
        remaining=max_route_pairs-submitted
        if remaining<=0 or stop is not None:
            break
        nodes=sorted(item['nodes'])[:remaining]
        for start in range(0,len(nodes),25):
            chosen=nodes[start:start+25]
            targets=[SimpleNamespace(poi_id=f'gap:{x:g}:{y:g}',location=to_center(x,y)) for x,y in chosen]
            try:
                measured=_measure(item['origin']['location'],targets,route,warnings)
            except ProviderStopError as exc:
                stop=exc.code;break
            submitted+=len(chosen)
            for xy,target in zip(chosen,targets):
                result=measured.get(target.poi_id)
                if result is not None and result.status=='success' and result.distance_m is not None:
                    samples[oid]['samples'][xy]=float(result.distance_m)
                    samples[oid]['failed_points']=[p for p in samples[oid]['failed_points'] if tuple(p)!=xy]
                else:
                    samples[oid]['failed_points'].append(xy);failed+=1
        progress(.05+.65*submitted/max(1,sum(len(v['nodes']) for v in planned.values())))
    if route_pair_cache_dir is not None:
        info['last_partial_pairs_recovered']=recover_pair_samples(report,samples,route_pair_cache_dir)
    def forbidden(_):raise AssertionError('Gap reconstruction cannot add provider calls')
    result=recompute_actual_case(source.model_dump(mode='json'),forbidden,report_id=report_id,
        sample_cache=samples,cache_info=info,max_route_pairs=0,progress=progress)
    result.result.warnings.extend(warnings)
    result.result.quality.update(coverage_gap_targeting=dict(target_pairs=submitted,
        planned_pairs=sum(len(v['nodes']) for v in planned.values()),failed_pairs=failed,stop_reason=stop,
        origin_count=len(planned),scope=scope),coverage_stop_reason=stop)
    return finish_continuation(source,result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--id',required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live',action='store_true')
    mode.add_argument('--offline',action='store_true')
    parser.add_argument('--max-pairs',type=int,default=5000)
    parser.add_argument('--gaps',action='store_true',help='target only the remaining group-union unknown regions')
    args=parser.parse_args(argv)
    if (not all(re.fullmatch(r'[A-Za-z0-9_-]{1,160}',x) for x in (args.source,args.id))
            or args.source==args.id or not 0<=args.max_pairs<=5000):
        parser.error('Use fresh safe report IDs and at most 5000 route pairs per round')
    reports=ROOT/'data/reports'; progress_dir=ROOT/'data/progress'
    output=reports/f'{args.id}_report.json'
    ledger=progress_dir/f'{args.id}_requests.json'
    state=progress_dir/f'{args.id}_execution.json'
    if any(p.exists() for p in (output,ledger,state)):
        parser.error('Refusing to replace saved reports or prior-round accounting')
    source=CheckupJob.model_validate_json((reports/f'{args.source}_report.json').read_text())
    transport=wrapper=None

    def publish(**extra):
        record=dict(report_id=args.id,source_report_id=args.source,
            updated_at=datetime.now(timezone.utc).isoformat(),
            http=transport.snapshot() if transport else dict(requests=0),
            route_cache_reuse=wrapper.snapshot() if wrapper else {},**extra)
        _atomic_json(state,record)
        print(json.dumps(record,ensure_ascii=False),flush=True)

    def forbidden(_): raise AssertionError('Offline continuation must not request any provider')
    route=forbidden
    if args.live:
        dirs=[ROOT/'data/cache/actual-poi',ROOT/'data/cache/shared500',ROOT/'data/cache/coverage-20m']
        transport=BoundedBaiduTransport(credentials(),dirs,ledger,min_interval=.36,route_pair_qps=3)
        wrapper=CacheFirstRouting(routing_for(transport).route,dirs,ROOT/'data/cache/tongji-service-route-pairs',
            request_batch_size=3)
        route=wrapper.route
    publish(phase='coverage',percent=0)
    try:
        result=(fill_group_gaps if args.gaps else continue_saved_report)(source,route,report_id=args.id,
            max_route_pairs=args.max_pairs if args.live else 0,
            route_pair_cache_dir=ROOT/'data/cache/tongji-service-route-pairs',
            progress=lambda value:publish(phase='coverage',percent=round(value*100,1)))
        q=result.result.quality
        stats=transport.snapshot() if transport else dict(requests=0,cache_hits=0)
        q.update(new_http_requests=stats['requests'],provider_requests=stats['requests'],
            credential_accounting=stats,route_cache_reuse=wrapper.snapshot() if wrapper else {},
            provider_request_limit=450,new_poi_search_requests=0,new_center_route_requests=0)
        _atomic_json(output,result.model_dump(mode='json'))
        publish(phase='saved',percent=100,saved=output.name,publication_status=q['coverage_publication_status'],
            validation=q['coverage_validation'],
            coverage={gid:{k:r[k] for k in ('covered_ratio','blind_ratio','unknown_ratio')}
                for gid,r in result.result.blindspot['group_coverage'].items()},
            stop_reason=q.get('coverage_stop_reason'))
        return 0
    except Exception as exc:
        publish(phase='failed',error_type=type(exc).__name__,error_code=getattr(exc,'code',None))
        raise


if __name__=='__main__':
    raise SystemExit(main())
