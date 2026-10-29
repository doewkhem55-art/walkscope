"""Offline audit of the Zhongnan education continuation; never edits reports."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shapely import make_valid, set_precision
from shapely.geometry import Point, Polygon, shape
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity
from app.schemas.checkup import CheckupJob
from app.modules.checkup.service import _projection
from app.modules.blindspot.service import _polygon_parts
from tools.bounded_baidu import _atomic_json


def audit_report(raw, original_raw, restored_raw, *, require_complete=False, secret_values=()):
    assert not any(value.encode() in raw for value in secret_values if value), 'Private credential in report'
    report = CheckupJob.model_validate_json(raw).result
    restored = CheckupJob.model_validate_json(restored_raw).result
    original = json.loads(original_raw)['partial_result']
    assert report is not None and restored is not None
    canonical = lambda rows, keys: sorted(rows, key=lambda row: tuple(row[key] for key in keys))
    dumped = lambda rows: [row.model_dump(mode='json') for row in rows]
    assert report.center.model_dump(mode='json') == original['center'] == restored.center.model_dump(mode='json')
    assert report.isochrone.model_dump(mode='json') == original['isochrone'] == restored.isochrone.model_dump(mode='json')
    assert len(report.pois) == 191 and canonical(dumped(report.pois), ['facility_id', 'poi_id']) == canonical(original['pois'], ['facility_id', 'poi_id']) == canonical(dumped(restored.pois), ['facility_id', 'poi_id'])
    assert len(report.routes) == 78 and canonical(dumped(report.routes), ['target_id']) == canonical(original['routes'], ['target_id']) == canonical(dumped(restored.routes), ['target_id'])
    assert report.score == restored.score == original['groups'][0]['score'] == 40
    assert len(report.categories) == 6
    original_categories = {row['facility_id']: row for row in original['categories']}
    restored_categories = {row.facility_id: row for row in restored.categories}
    for row in report.categories:
        old = original_categories[row.facility_id]
        prior = restored_categories[row.facility_id]
        assert row.score == prior.score == old['score']
        assert row.known_count == prior.known_count == old['known_count']
        assert row.search_status == prior.search_status == old['search_status'] == 'complete'
    audit = report.quality['coverage_validation']
    assert audit['poi_check_passed'] and audit['unverified_poi_count'] == 0
    if require_complete:
        assert audit['evidence_complete'], 'Group coverage remains incomplete'

    project, _ = _projection(report.center)
    clean = lambda geometry: unary_union(_polygon_parts(set_precision(make_valid(geometry), 1e-6)))
    domain = clean(transform(project, shape(report.isochrone.geometry.model_dump())))
    raw_invalid = []

    def polygon_components(geometry):
        if geometry.geom_type == 'Polygon':
            return [geometry]
        return [part for child in getattr(geometry, 'geoms', ()) for part in polygon_components(child)]

    def region(collection, scope, name):
        parts = []
        for index, feature in enumerate(collection['features']):
            geographic = shape(feature['geometry'])
            assert geographic.geom_type in {'Polygon', 'MultiPolygon'}, (scope, name, 'Non-polygon evidence')
            projected = transform(project, geographic)
            repaired = make_valid(projected)
            normalized = clean(projected)
            if not geographic.is_valid or not projected.is_valid:
                components = polygon_components(projected)
                degenerate = [p for p in components if p.area <= 1e-8]
                rings = [ring for p in components for ring in [p.exterior, *p.interiors]
                         if len(set(ring.coords)) < 3 or Polygon(ring).area <= 1e-8]
                raw_invalid.append(dict(scope=scope, layer=name, feature_index=index,
                    reason=explain_validity(geographic), projected_reason=explain_validity(projected),
                    raw_projected_area_m2=projected.area, repaired_area_m2=repaired.area,
                    audit_precision_area_m2=normalized.area,
                    repair_area_difference_m2=abs(repaired.area-projected.area),
                    precision_area_difference_m2=abs(normalized.area-repaired.area),
                    degenerate_polygon_component_count=len(degenerate),
                    degenerate_polygon_component_area_m2=sum(p.area for p in degenerate),
                    degenerate_ring_count=len(rings)))
            parts.append(normalized)
        return clean(unary_union(parts))

    def check(scope, record, facility_ids):
        covered, blind, unknown = [region(record[name], scope, name) for name in
                                  ('covered_areas', 'blind_spots', 'unknown_areas')]
        values = [record[key] for key in ('covered_ratio', 'blind_ratio', 'unknown_ratio')]
        assert all(math.isfinite(value) and 0 <= value <= 1 for value in values), scope
        assert abs(sum(values)-1) < 1e-8, scope
        assert all(geometry.is_valid for geometry in (covered, blind, unknown)), scope
        error = domain.symmetric_difference(unary_union([covered, blind, unknown])).area
        overlap = max(covered.intersection(blind).area, covered.intersection(unknown).area,
                      blind.intersection(unknown).area)
        ratio_error = max(abs(geometry.area/domain.area-value) for geometry, value in
                          zip((covered, blind, unknown), values))
        assert error < .01 and overlap < .01 and ratio_error < 1e-8, (scope, error, overlap, ratio_error)
        seen = set()
        for poi in report.pois:
            if poi.in_isochrone and poi.facility_id in facility_ids:
                point = Point(*project(poi.location.lng, poi.location.lat))
                assert not blind.contains(point), (scope, 'Inside POI in confirmed blind zone', poi.poi_id)
                seen.add((poi.poi_id, poi.location.lng, poi.location.lat))
        if require_complete:
            assert record['unknown_ratio'] <= 1e-8, (scope, 'Unknown remains', record['unknown_ratio'])
        return dict(**{key: record[key] for key in ('covered_ratio', 'blind_ratio', 'unknown_ratio')},
                    partition_error_m2=error, max_overlap_m2=overlap, max_ratio_error=ratio_error,
                    inside_poi_count=len(seen), inside_poi_in_confirmed_blind_count=0)

    groups = {gid: check(gid, record, set(record['facility_ids']))
              for gid, record in report.blindspot['group_coverage'].items()}
    for gid, result in groups.items():
        validation = audit['groups'][gid]
        assert validation['poi_check_passed'] and validation['unverified_poi_count'] == 0, gid
        result['unverified_poi_count'] = validation['unverified_poi_count']
    categories = {}
    for row in report.categories:
        record = {name: dict(type='FeatureCollection', features=[feature for feature in report.blindspot[name]['features']
                  if feature['properties']['facility_type'] == row.facility_id])
                  for name in ('covered_areas', 'blind_spots', 'unknown_areas')}
        record.update({key: getattr(row, key) for key in ('covered_ratio', 'blind_ratio', 'unknown_ratio')})
        categories[row.facility_id] = check(row.facility_id, record, {row.facility_id})
    total = report.blindspot.get('total_coverage')
    if total is not None:
        check('total', total, set(total['facility_ids']))
    return dict(ok=True, report_id=report.report_id, score=report.score, category_count=6,
        poi_category_records=191, center_routes=78, inputs_and_scores_unchanged=True,
        groups=groups, categories=categories, require_complete=require_complete,
        all_categories_complete=all(row.unknown_ratio <= 1e-8 for row in report.categories),
        group_evidence_complete=audit['evidence_complete'], private_ak_in_report=False,
        report_sha256=hashlib.sha256(raw).hexdigest(), audit_precision_m=1e-6,
        raw_invalid_feature_count=len(raw_invalid), raw_invalid_features=raw_invalid,
        raw_geometries_unchanged_by_audit=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', default='wuhan-zhongnan-education-complete-20261026')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', args.report):
        parser.error('Use a saved report identity')
    reports = ROOT / 'data/reports'
    read = lambda name: (reports / f'{name}_report.json').read_bytes()
    private = ROOT / '.local/baidu-keys.json'
    values = [row['value'] for row in json.loads(private.read_bytes())] if private.exists() else []
    try:
        result = audit_report(read(args.report), read('12de2c15ac4a44798a2bdbeff6ff2a8f'),
            read('wuhan-zhongnan-education-restored-20261026'), require_complete=args.require_complete,
            secret_values=values)
    except AssertionError as error:
        result = dict(ok=False, report_id=args.report, error=str(error),
            require_complete=args.require_complete, raw_geometries_unchanged_by_audit=True)
    destination = ROOT / 'data/progress/zhongnan-audit-20261026.json'
    _atomic_json(destination, result)
    print(json.dumps(dict(ok=result['ok'], report_id=args.report, saved=str(destination),
        all_categories_complete=result.get('all_categories_complete'),
        raw_invalid_feature_count=result.get('raw_invalid_feature_count'), error=result.get('error')), ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
