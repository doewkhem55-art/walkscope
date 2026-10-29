"""User-selected map policy: unverified areas join the displayed blind area.

Evidence ratios and geometries stay unchanged. Display geometry is explicitly
labelled so a conservative planning view cannot masquerade as verified absence.
"""
from copy import deepcopy
from types import SimpleNamespace

from shapely import make_valid
from shapely.geometry import Point, Polygon, shape
from shapely.ops import unary_union
from shapely.geometry import mapping

from app.schemas.common import WarningItem
from app.modules.blindspot.service import _geojson, _polygon_parts
from app.modules.isochrone.geometry import geometry_to_local

POLICY = 'include_unknown'


def poi_coverage_audit(layers, pois):
    """Check every in-circle POI against its own group's measured service union.

    This is a publication check, never permission to fabricate missing coverage.
    A terminal workflow or complete POI search does not pass this check by itself.
    """
    rows = [p.model_dump(mode='json') if hasattr(p, 'model_dump') else p for p in pois]
    groups = {}
    for gid, record in layers.get('group_coverage', {}).items():
        covered = unary_union([make_valid(shape(f['geometry']))
            for f in record.get('covered_areas', {}).get('features', [])])
        ids = set(record.get('facility_ids', []))
        seen, pending = set(), []
        for poi in rows:
            if poi.get('in_isochrone') is not True or poi.get('facility_id') not in ids:
                continue
            location = poi['location']
            identity = (poi['poi_id'], location['lng'], location['lat'])
            if identity in seen:
                continue
            seen.add(identity)
            # Numerical GeoJSON round trips only; no metre-sized POI buffer.
            if covered.is_empty or covered.distance(Point(location['lng'], location['lat'])) > 1e-11:
                pending.append(dict(poi_id=poi['poi_id'], name=poi.get('name', poi['poi_id']),
                    facility_id=poi['facility_id'], location=location))
        groups[gid] = dict(inside_poi_count=len(seen), unverified_poi_count=len(pending),
            unverified_pois=pending, poi_check_passed=not pending,
            unknown_ratio=record.get('unknown_ratio', 1),
            evidence_complete=not pending and record.get('unknown_ratio', 1) <= 1e-8)
    return dict(groups=groups, unverified_poi_count=sum(r['unverified_poi_count'] for r in groups.values()),
        poi_check_passed=bool(groups) and all(r['poi_check_passed'] for r in groups.values()),
        evidence_complete=bool(groups) and all(r['evidence_complete'] for r in groups.values()))


def apply_display_policy(layers, *, center=None, geometry=None, pois=None):
    """Display the service-area complement; POI coordinates alone prove no buffer."""
    result = deepcopy(layers)
    result.pop('poi_known_areas', None)
    domain = to_m = to_center = None
    if center is not None and geometry is not None:
        from app.modules.checkup.service import _projection
        to_m, to_center = _projection(center)
        domain = geometry_to_local(SimpleNamespace(**geometry) if isinstance(geometry, dict) else geometry, to_m)
    records = list(result.get('group_coverage', {}).items())
    if isinstance(result.get('total_coverage'), dict):
        records.append(('total', result['total_coverage']))
    for scope, record in records:
        for key in ('poi_known_areas', 'display_known_poi_ratio', 'display_poi_cell_m', 'display_known_ratio'):
            record.pop(key, None)
        parts = []
        for key in ('blind_spots', 'unknown_areas'):
            for feature in record.get(key, {}).get('features', []):
                geometry = shape(feature['geometry'])
                if not geometry.is_empty:
                    # JSON round trips can collapse contour rings. Repair only
                    # temporary display polygons; preserve stored evidence above.
                    parts.extend(_polygon_parts(make_valid(geometry)))
        geometry = unary_union(parts)
        display_ratio = min(1.0, max(0.0, record.get('blind_ratio', 0) + record.get('unknown_ratio', 0)))
        if domain is not None and domain.area > 0:
            local_blind = geometry_to_local(SimpleNamespace(**mapping(geometry)), to_m) if not geometry.is_empty else Polygon()
            local_display = local_blind.intersection(domain)
            display_ratio = min(1.0, max(0.0, local_display.area / domain.area))
            geometry = shape(_geojson(local_display, to_center)) if not local_display.is_empty else Polygon()
        features = [] if geometry.is_empty else [dict(type='Feature', geometry=mapping(geometry),
            properties=dict(facility_type=scope, classification='blind_including_unverified', display_policy=POLICY))]
        record.update(display_policy=POLICY, display_label='盲区（含未核验区域）',
            display_blind_spots=dict(type='FeatureCollection', features=features),
            display_blind_ratio=display_ratio)
    return result


def report_with_display_policy(report):
    """Read-only response projection for stored reports and model questions."""
    result = report.model_copy(deep=True)
    if not report.blindspot.get('group_coverage') and not isinstance(report.blindspot.get('total_coverage'), dict):
        return result
    result.blindspot = apply_display_policy(report.blindspot, center=report.center,
        geometry=report.isochrone.geometry, pois=report.pois)
    result.quality['coverage_display_policy'] = POLICY
    result.quality['coverage_validation'] = poi_coverage_audit(result.blindspot, report.pois)
    result.quality['coverage_publication_status'] = ('verified_estimate'
        if result.quality['coverage_validation']['evidence_complete'] else 'incomplete')
    result.warnings = [w for w in result.warnings if w.code != 'POI_SERVICE_AREA_UNVERIFIED']
    if not result.quality['coverage_validation']['poi_check_passed']:
        result.warnings.append(WarningItem(code='POI_SERVICE_AREA_UNVERIFIED', message=(
            '部分圈内POI尚未落入本组已测服务区，覆盖核验未完成；不能将其所在高亮区域解释为确认无服务。')))
    result.quality.pop('coverage_known_poi_cell_m', None)
    result.warnings = [w for w in result.warnings if w.code != 'POI_PRESENCE_EXCLUDED_FROM_DISPLAY_BLIND']
    if not any(w.code == 'UNKNOWN_INCLUDED_IN_DISPLAY_BLIND' for w in result.warnings):
        result.warnings.append(WarningItem(code='UNKNOWN_INCLUDED_IN_DISPLAY_BLIND', message=(
            '按当前展示口径，未确认覆盖的未知区域合并进盲区高亮；此处盲区包含未核验部分，'
            '面积看display_blind_ratio。原blind_ratio/unknown_ratio仍分别保存核验结论，不能把未知当作已核实无服务。')))
    return result
