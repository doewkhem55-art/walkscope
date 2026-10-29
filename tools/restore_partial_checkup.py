"""Recover completed acquisition stages from a failed local checkup, offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shapely.geometry import Point, shape

from app.modules.checkup.catalog import _CATALOG_PATH, load_catalog
from app.modules.checkup.coverage_display import report_with_display_policy
from app.modules.checkup.service import run_checkup
from app.modules.routing.service import _summary
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.checkup import CheckupJob, CheckupPOI, CheckupRequest
from app.schemas.common import CenterPoint
from app.schemas.isochrone import IsochroneData
from app.schemas.routing import RouteResult, RoutingResult
from tools.bounded_baidu import _atomic_json


def restore_partial_checkup(source, *, stage_dir, report_id, label):
    """Keep the original failure immutable; never fabricate or request routes."""
    if source.status != 'failed' or source.partial_result is None or source.id == report_id:
        raise ValueError('A failed source with saved progress and a fresh identity is required')
    saved = source.partial_result
    center = CenterPoint.model_validate(saved['center'])
    iso = IsochroneData.model_validate(saved['isochrone'])
    domain = shape(saved['isochrone']['geometry'])
    catalog = load_catalog()
    definitions = {row['id']: row for row in catalog['facilities']}
    pois = [CheckupPOI.model_validate(row) for row in saved['pois']]
    rows = []
    for row in saved['categories']:
        fid = row['facility_id']
        if fid not in definitions or row['search_status'] != 'complete':
            raise ValueError('Only fully acquired selected searches can be restored')
        members = [poi for poi in pois if poi.facility_id == fid]
        for poi in members:
            if poi.in_isochrone != domain.covers(Point(poi.location.lng, poi.location.lat)):
                raise ValueError('Saved POI membership conflicts with its saved domain')
        if row['known_count'] != sum(poi.in_isochrone for poi in members):
            raise ValueError('Saved POI count conflicts with its completed search')
        rows.append(dict(facility_id=fid, search_status='complete',
                         pois=[poi.model_dump(mode='json') for poi in members]))
    ids = [row['facility_id'] for row in rows]
    if not rows or saved['completed_categories'] != len(rows) or saved['total_categories'] != len(rows):
        raise ValueError('Selected searches are not complete')
    if set(poi.facility_id for poi in pois) - set(ids):
        raise ValueError('Saved POIs belong to unselected searches')
    location_input = dict(type='coordinate', **center.model_dump(mode='json'))
    gate1 = dict(location=dict(ok=True, data=dict(center=center.model_dump(mode='json'),
        label=label, source='input_coordinate'), warnings=[],
        meta=dict(schema_version='1.0', provider='saved-checkup-progress')),
        isochrone=dict(ok=True, data=saved['isochrone'], warnings=[],
                      meta=dict(schema_version='1.0', provider='saved-checkup-progress')))
    radius = minimum_poi_search_radius_m(center, iso.geometry,
        max([1000] + [definitions[fid].get('service_distance_m') or 0 for fid in ids]))
    gate2 = dict(gate1_file='gate1.json', catalog_version=catalog['version'],
                 center=center.model_dump(mode='json'), search_radius_m=radius, categories=rows)
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    paths = [stage_dir / name for name in ('gate1.json', 'gate2.json', 'manifest.json')]
    if any(path.exists() for path in paths):
        raise FileExistsError('Refusing to replace an earlier restored acquisition stage')
    for path, data in zip(paths, (gate1, gate2)):
        _atomic_json(path, data)
    fingerprint = hashlib.sha256(json.dumps(location_input, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    manifest = dict(source='baidu-live-staged', input=location_input, input_sha256=fingerprint,
        catalog_version=catalog['version'], catalog_sha256=hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest(),
        **{name: dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
           for name, path in zip(('gate1', 'gate2'), paths)})
    _atomic_json(paths[2], manifest)
    measured = {row['target_id']: RouteResult.model_validate(row) for row in saved['routes']}

    def saved_routes(request):
        if request.origin != center:
            raise AssertionError('Offline acquisition recovery cannot measure service areas')
        routes = []
        for target in request.targets:
            row = measured.get(target.target_id)
            if row is None or row.location != target.location:
                raise ValueError('A saved center route is missing or has a different identity')
            routes.append(row)
        return RoutingResult.model_validate(dict(ok=True, data=dict(
            origin=center.model_dump(mode='json'), travel_mode='walking',
            routes=[row.model_dump(mode='json') for row in routes], summary=_summary(routes).model_dump()),
            warnings=[], meta=dict(schema_version='1.0', provider='saved-checkup-progress')))

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Restoring saved acquisition must never call a provider')

    report = run_checkup(CheckupRequest(location=location_input, facility_ids=ids),
        report_id=report_id, stage_manifest=paths[2], resolve_location=forbidden,
        generate_isochrone=forbidden, poi_provider=None, route=saved_routes, max_grid_routes=0)
    report.quality.update(restored_from_failed_checkup=source.id,
        source_failure=source.error.model_dump(mode='json') if source.error else None,
        source_progress_sha256=hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
        new_http_requests=0, provider_requests=0, coverage_publication_status='incomplete')
    report = report_with_display_policy(report)
    return CheckupJob(id=report_id, status='completed', stage='completed', progress=100, result=report)
