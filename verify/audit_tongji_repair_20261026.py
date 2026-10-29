"""Offline release audit: preserved inputs, geometry partitions and POI checks."""
from pathlib import Path
import hashlib
import json
import math
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from shapely import make_valid,set_precision
from shapely.geometry import Point,shape
from shapely.ops import transform,unary_union
from app.schemas.checkup import CheckupJob
from app.modules.checkup.service import _projection
from app.modules.blindspot.service import _polygon_parts
from tools.bounded_baidu import _atomic_json


def main():
    path=ROOT/'data/reports/tongji-all7-repaired-20261026_report.json'
    raw=path.read_bytes();r=CheckupJob.model_validate_json(raw).result
    old=CheckupJob.model_validate_json((ROOT/'data/reports/tongji-all7-walk-contour-20261025_report.json').read_bytes()).result
    assert r.isochrone==old.isochrone and r.center==old.center and r.score==old.score==55
    canon=lambda pois:sorted([p.model_dump(mode='json') for p in pois],key=lambda p:
        (p['facility_id'],p['poi_id'],p['location']['lng'],p['location']['lat']))
    assert canon(r.pois)==canon(old.pois)
    assert {g.id:g.score for g in r.groups}=={g.id:g.score for g in old.groups}
    projection,_=_projection(r.center)
    # Only the audit overlay uses micrometre precision to discard floating-point
    # slivers. Source report polygons and recorded ratios are never rewritten.
    def clean(geometry):
        return unary_union(_polygon_parts(set_precision(make_valid(geometry),1e-6)))
    domain=clean(transform(projection,shape(r.isochrone.geometry.model_dump())))
    observed={}
    for feature in r.blindspot['route_samples']['features']:
        props=feature['properties'];distance=props.get('distance_m')
        if distance is not None:
            assert math.isfinite(distance) and distance>=0
            for fid in props['facility_ids']:
                key=(props['representative_id'],fid);observed[key]=observed.get(key,0)+1
    groups={}
    for gid,record in r.blindspot['group_coverage'].items():
        def region(name):
            return clean(unary_union([clean(transform(projection,shape(f['geometry'])))
                for f in record[name]['features']]))
        covered,blind,unknown=map(region,['covered_areas','blind_spots','unknown_areas'])
        partition_error=domain.symmetric_difference(unary_union([covered,blind,unknown])).area
        overlap=max(covered.intersection(blind).area,covered.intersection(unknown).area,blind.intersection(unknown).area)
        assert partition_error<.01 and overlap<.01 and unknown.area/domain.area<1e-8,(gid,partition_error,overlap)
        ids=set(record['facility_ids']);seen=set()
        for poi in r.pois:
            if poi.in_isochrone is True and poi.facility_id in ids:
                identity=(poi.poi_id,poi.location.lng,poi.location.lat)
                if identity in seen:continue
                seen.add(identity)
                point=Point(*projection(poi.location.lng,poi.location.lat))
                assert covered.distance(point)<=1e-5 and not blind.contains(point),(gid,poi.poi_id)
        unmeasured_overlap=0
        for feature in r.blindspot['representative_points']['features']:
            props=feature['properties'];members=set(props['facility_ids']) & ids
            if members and not any(observed.get((props['representative_id'],fid),0) for fid in members):
                window=Point(*projection(*feature['geometry']['coordinates'])).buffer(
                    1000/math.cos(math.pi/512),quad_segs=128)
                unmeasured_overlap=max(unmeasured_overlap,window.intersection(blind).area)
        assert unmeasured_overlap<.01,(gid,unmeasured_overlap)
        groups[gid]=dict(inside_poi_count=len(seen),unverified_poi_count=0,
            **{k:record[k] for k in ('covered_ratio','blind_ratio','unknown_ratio')},
            partition_error_m2=partition_error,max_overlap_m2=overlap,
            unmeasured_origin_blind_overlap_m2=unmeasured_overlap)
    original_manifest=json.loads((ROOT.parent/'manifest.json').read_text())
    originals=[row for row in original_manifest['files'] if row['path'].startswith('project/data/reports/')]
    assert len(originals)==19
    assert all(hashlib.sha256((ROOT.parent/row['path']).read_bytes()).hexdigest()==row['sha256'] for row in originals)
    private=ROOT/'.local/baidu-keys.json'
    if private.exists():
        assert not any(row['value'].encode() in raw for row in json.loads(private.read_text()))
    assert (ROOT/'frontend/public/cases/tongji.json').read_bytes()==raw
    result=dict(ok=True,report_id=r.report_id,score=r.score,groups=groups,
        category_count=len(r.categories),poi_category_records=len(r.pois),original_reports_unchanged=19,
        pois_and_isochrone_unchanged=True,private_ak_in_report=False,case_byte_equal=True,
        new_http_requests=r.quality['coverage_repair_http_requests'],
        measurement_variant_pairs=r.quality['coverage_measurement_variant_pair_count'],
        report_sha256=hashlib.sha256(raw).hexdigest(),audit_precision_m=1e-6,
        raw_geometries_unchanged_by_audit=True,claim_scope=r.quality['coverage_repair_scope'])
    destination=ROOT.parent/'evidence/同济POI补算_2026-10-26/最终七组独立核验.json'
    _atomic_json(destination,result)
    print(json.dumps(dict(ok=True,groups_verified=len(groups),unverified_pois=0,
        original_reports_unchanged=19,new_http_requests=result['new_http_requests'],saved=str(destination)),ensure_ascii=False))


if __name__=='__main__':
    main()
