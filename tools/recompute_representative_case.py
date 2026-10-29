"""Rebuild a 100 m centre isochrone and one saved group's shared-origin coverage."""
from __future__ import annotations
import argparse
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
sys.path.insert(0,str(ROOT))

from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.scoring import score_category
from app.modules.checkup.service import analyze_coverage, _guarded_route, _projection, _quantize
from app.modules.isochrone.geometry import geometry_to_local
from app.modules.isochrone.grid import generate_grid_isochrone
from app.modules.checkup.representative_service_areas import METHOD
from app.schemas.checkup import CheckupJob, CategoryAssessment, GroupAssessment
from app.schemas.common import CenterPoint, WarningItem
from app.schemas.isochrone import IsochroneData, IsochroneRequest
from app.schemas.routing import RouteStatus
from shapely.geometry import Point


def load_isochrone_source(path, center):
    """Reuse only the same centre's 900-second, 100-metre isochrone."""
    raw = json.loads(Path(path).read_text())
    value = raw.get('result', raw)
    iso = IsochroneData.model_validate(value.get('isochrone', value))
    if iso.center != CenterPoint.model_validate(center):
        raise ValueError('isochrone source centre differs from the saved case')
    if iso.time_limit_s != 900 or iso.quality.grid_cell_m != 100:
        raise ValueError('isochrone source must use 900 seconds and a 100 m grid')
    return iso


def load_coverage_samples_source(path, center, isochrone):
    """Reuse finite measurements, with matching field geometry and method."""
    report = CheckupJob.model_validate_json(Path(path).read_text()).result
    if report is None or report.center != CenterPoint.model_validate(center) or report.isochrone.center != isochrone.center:
        raise ValueError('coverage samples source centre differs from the saved case')
    if report.isochrone.geometry != isochrone.geometry:
        raise ValueError('coverage samples source isochrone geometry differs')
    if report.quality.get('coverage_method') != METHOD:
        raise ValueError('coverage samples source calculation method differs')
    to_m, _ = _projection(report.center)
    cache = {}
    layers = report.blindspot or {}
    for feature in layers.get('representative_points', {}).get('features', []):
        origin_id = feature.get('properties', {}).get('representative_id')
        geometry = feature.get('geometry', {})
        coordinates = geometry.get('coordinates', [])
        if not isinstance(origin_id, str) or geometry.get('type') != 'Point' or len(coordinates) != 2:
            continue
        try:
            location = CenterPoint(lng=coordinates[0], lat=coordinates[1], crs='BD09LL')
        except (TypeError, ValueError):
            continue
        position = [location.lng, location.lat]
        if origin_id in cache and cache[origin_id]['location'] != position:
            raise ValueError('coverage samples source has conflicting representative locations')
        cache.setdefault(origin_id, dict(location=position, samples={}))
    for feature in layers.get('route_samples', {}).get('features', []):
        props = feature.get('properties', {})
        origin = cache.get(props.get('representative_id'))
        distance = props.get('distance_m')
        geometry = feature.get('geometry', {})
        coordinates = geometry.get('coordinates', [])
        if (origin is None or isinstance(distance, bool) or not isinstance(distance, (int, float))
                or not math.isfinite(distance) or distance < 0
                or geometry.get('type') != 'Point' or len(coordinates) != 2):
            continue
        try:
            location = CenterPoint(lng=coordinates[0], lat=coordinates[1], crs='BD09LL')
        except (TypeError, ValueError):
            continue
        xy = tuple(round(value, 6) for value in to_m(location.lng, location.lat))
        if xy in origin['samples'] and origin['samples'][xy] != float(distance):
            raise ValueError('coverage samples source has conflicting distances')
        origin['samples'][xy] = float(distance)
    return cache


def subset_report(source, isochrone, group_id, report_id):
    """Recount historical saved POIs in the new domain; never change the source."""
    old = CheckupJob.model_validate(source).result
    report = old.model_copy(deep=True)
    catalog = load_catalog()
    definitions = [f for f in catalog['facilities'] if f['group_id']==group_id]
    if not definitions:
        raise ValueError('unknown group')
    selected = {f['id'] for f in definitions}
    report.pois = [p for p in report.pois if p.facility_id in selected]
    to_m,_ = _projection(report.center)
    domain = geometry_to_local(isochrone.geometry,to_m)
    for p in report.pois:
        p.in_isochrone = domain.covers(Point(*to_m(p.location.lng,p.location.lat)))
    route_by_id = {r.target_id:r for r in old.routes}
    report.routes = [r for r in old.routes if r.target_id in {p.poi_id for p in report.pois}]
    prior = {c.facility_id:c for c in old.categories}
    rows = []
    for f in definitions:
        fid=f['id']; old_category=prior[fid]
        observed={p.poi_id:p for p in report.pois if p.facility_id==fid and p.in_isochrone}
        resolved=[route_by_id[k] for k in observed if k in route_by_id]
        good=[r for r in resolved if r.status==RouteStatus.SUCCESS]
        nearest=min(good,key=lambda r:r.duration_s,default=None)
        reachable=sum(r.duration_s<=900 for r in good)
        unresolved=len(observed)-sum(r.status!=RouteStatus.UNAVAILABLE for r in resolved)
        count=len(observed) if old_category.search_status=='complete' else None
        scored=score_category(fid,old_category.search_status,count,len(observed))
        rows.append(CategoryAssessment(facility_id=fid,name_zh=f['name_zh'],group_id=group_id,
            search_status=old_category.search_status,count=count,known_count=len(observed),
            reachable_count=reachable,unresolved_count=unresolved,
            nearest_duration_s=nearest.duration_s if nearest else None,nearest_distance_m=nearest.distance_m if nearest else None,
            access_status='reachable' if reachable else 'unverified' if unresolved or count is None else 'not_found' if not observed else 'unreachable',
            score=scored.score,score_provisional=scored.status=='provisional',score_profile=scored.profile,
            score_basis_count=scored.basis_count,score_band=scored.band,next_band=scored.next_band,data_notes=f.get('data_notes',''),
            covered_ratio=0,blind_ratio=0,unknown_ratio=1))
    values=[c.score for c in rows if c.score is not None]
    score=_quantize(sum(values)/len(values)) if len(values)==len(rows) else None
    label=next(g['name_zh'] for g in catalog['groups'] if g['id']==group_id)
    report=report.model_copy(update=dict(categories=rows,
        groups=[GroupAssessment(id=group_id,name_zh=label,score=score,score_provisional=any(c.score_provisional for c in rows),
            assessed_categories=len(values),total_categories=len(rows),summary='复用历史设施，按新100米等时圈重新统计数量；中心到设施路线沿用历史证据。')],
        score=score,score_provisional=any(c.score_provisional for c in rows),selected_facility_ids=[f['id'] for f in definitions]))
    report.isochrone=isochrone
    report.report_id=report_id
    report.created_at=datetime.now(timezone.utc).isoformat()
    report.summary=f'同济大学 · {label}单组重算：100米中心等时圈、500米共享代表点、1000米步行服务区与自适应边界采样。设施来自历史保存数据。'
    report.warnings=[WarningItem(code='HISTORICAL_POIS_REUSED',message='本次未重新检索设施；沿用历史POI及召回完整性记录，数量按新生活圈重计。'),
        WarningItem(code='SELECTED_SCOPE',message=f'本次仅计算{label}{len(rows)}个小项，数量分不能与原45类52.5分直接比较。')]
    report.quality=dict(source_report_id=old.report_id,source_report_created_at=old.created_at,
        source_report_quality=old.quality,source_report_sha256=hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        selected_categories=len(rows),total_categories=len(rows),assessed_categories=len(values),
        isochrone_recomputed=True,isochrone_grid_cell_m=100,reused_poi_search=True,
        reused_center_route_count=len(report.routes),source_isochrone_grid_cell_m=old.isochrone.quality.grid_cell_m)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='3de7777d550f4562afc38d96140f0118')
    parser.add_argument('--id',default='tongji-education-shared500-20261024')
    parser.add_argument('--group',default='education_children')
    parser.add_argument('--isochrone-source',type=Path,help='reuse a saved 900 s / 100 m isochrone without centre route calls')
    parser.add_argument('--coverage-samples-source',type=Path,help='reuse matching shared-500 m report measurements across categories')
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live: parser.error('--live explicitly permits bounded paid route calls')
    if not all(c.isalnum() or c in '-_' for c in args.id): parser.error('invalid id')
    from tools.dev import load_env
    from app.modules.routing.service import RoutingService
    from app.providers.baidu.routing import BaiduRoutingProvider
    from app.providers.baidu.direction import BaiduDirectionProvider
    load_env(ROOT/'.env')
    key=os.environ.get('BAIDU_MAP_AK','').strip()
    if not key: parser.error('server key not configured')
    reports=ROOT/'data/reports'; path=reports/f'{args.id}_report.json'
    if path.exists(): parser.error('destination exists; choose a new id')
    source=json.loads((reports/f'{args.source}_report.json').read_text())
    reused_iso = load_isochrone_source(args.isochrone_source, source['result']['center']) if args.isochrone_source else None
    cache=ROOT/'data/cache/shared500';cache.mkdir(parents=True,exist_ok=True)
    progress_path=ROOT/'data/progress'/f'{args.id}_progress.json';progress_path.parent.mkdir(parents=True,exist_ok=True)
    iso_path=ROOT/'data/progress'/f'{args.id}_isochrone.json'
    started=time.monotonic();last_call=0.;calls=hits=0;phase='isochrone'
    cost_path=iso_path.with_suffix('.cost.json')
    prior_cost=json.loads(cost_path.read_text()) if cost_path.exists() else {'provider_requests':0,'elapsed_seconds':0}
    calls=prior_cost['provider_requests']
    phase_calls={'isochrone':calls,'coverage':0}
    def publish(**extra):
        state=dict(phase=phase,provider_requests=calls,cache_hits=hits,phase_requests=phase_calls,
            elapsed_seconds=round(time.monotonic()-started,1),**extra)
        progress_path.write_text(json.dumps(state,ensure_ascii=False))
        print(json.dumps(state,ensure_ascii=False),flush=True)
    def transport(url,timeout):
        nonlocal calls,hits,last_call
        parts=urlsplit(url)
        public=parts.path+'?'+urlencode([(k,v) for k,v in parse_qsl(parts.query) if k!='ak'])
        target=cache/(hashlib.sha256(public.encode()).hexdigest()+'.json')
        if target.exists():
            hits+=1;return json.loads(target.read_text())
        if calls>=450 or time.monotonic()-started+prior_cost.get('elapsed_seconds',0)>=1200:
            raise ProviderStopError('PROVIDER_BUDGET_EXHAUSTED','450 requests / 20 active minutes reached')
        time.sleep(max(0,1.5-(time.monotonic()-last_call)))
        last_call=time.monotonic();calls+=1;phase_calls[phase]+=1
        (progress_path.with_suffix('.requests.json')).write_text(json.dumps(dict(provider_requests=calls,phase_provider_requests=phase_calls)))
        with urlopen(url,timeout=min(timeout,max(.1,1200-(time.monotonic()-started+prior_cost.get('elapsed_seconds',0))))) as response:
            data=json.load(response)
        if data.get('status')==0: target.write_text(json.dumps(data,ensure_ascii=False))
        if calls%10==0: publish()
        return data
    routing=RoutingService(BaiduRoutingProvider(key,transport=transport),batch_size=25,max_retries=1,retry_delay_seconds=2,
        fallback_provider=BaiduDirectionProvider(key,transport=transport))
    route=_guarded_route(routing.route)
    publish()
    if reused_iso is not None:
        iso = reused_iso
        if iso_path.resolve() != args.isochrone_source.resolve():
            iso_path.write_text(iso.model_dump_json(indent=2))
    elif iso_path.exists():
        iso=IsochroneData.model_validate_json(iso_path.read_text())
    else:
        generated=generate_grid_isochrone(route,IsochroneRequest(schema_version='1.0',center=source['result']['center'],time_limit_s=900),
            cell_m=100,radius_m=2500,max_grid_points=2500,include_tiers=True)
        if not generated.root.ok: raise RuntimeError('100m isochrone failed; no report written')
        iso=generated.root.data;iso_path.write_text(iso.model_dump_json(indent=2))
        cost_path.write_text(json.dumps(dict(provider_requests=calls,elapsed_seconds=round(time.monotonic()-started+prior_cost.get('elapsed_seconds',0),1))))
    report=subset_report(source,iso,args.group,args.id)
    sample_cache = (load_coverage_samples_source(args.coverage_samples_source, report.center, iso)
                    if args.coverage_samples_source else None)
    report.quality.update(isochrone_recomputed=reused_iso is None, isochrone_reused=reused_iso is not None,
        isochrone_reused_from=str(args.isochrone_source) if args.isochrone_source else None,
        coverage_samples_reused_from=str(args.coverage_samples_source) if args.coverage_samples_source else None,
        coverage_samples_available=sum(len(origin['samples']) for origin in (sample_cache or {}).values()))
    phase='coverage';publish(isochrone_samples=iso.quality.routing_sample_count,isochrone_success=iso.quality.routing_success_count)
    rows=[dict(facility_id=c.facility_id,search_status=c.search_status,
        pois=[p.model_dump(mode='json') for p in report.pois if p.facility_id==c.facility_id]) for c in report.categories]
    last_percent=-1
    def progress(value):
        nonlocal last_percent
        percent=int(value*100)
        if percent!=last_percent:
            last_percent=percent;publish(percent=percent)
    kwargs = dict(progress=progress, origin_mode='shared_500m')
    if sample_cache is not None:
        kwargs['sample_cache'] = sample_cache
    ratios,layers,quality=analyze_coverage(report.center,iso.geometry,rows,load_catalog(),route,report.warnings,**kwargs)
    report.categories=[c.model_copy(update=ratios[c.facility_id]) for c in report.categories]
    report.blindspot=layers
    report.quality.update(quality,provider_requests=calls,provider_cache_hits=hits,provider_request_limit=450,
        provider_budget_exhausted=bool(quality['coverage_stop_reason']),phase_provider_requests=phase_calls,
        elapsed_seconds=round(time.monotonic()-started+prior_cost.get('elapsed_seconds',0),1),confidence='low' if quality['max_supported_category_unknown_ratio']>.1 else 'medium')
    result=CheckupJob(id=args.id,status='completed',stage='completed',progress=100,result=report)
    temp=path.with_suffix('.tmp');temp.write_text(result.model_dump_json(indent=2));temp.replace(path)
    phase='completed';publish(saved=str(path),aggregate={key:value for key,value in layers['total_coverage'].items()
        if value is None or isinstance(value,(str,int,float,bool))})

if __name__=='__main__':main()
