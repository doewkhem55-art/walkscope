from copy import deepcopy
import json
from pathlib import Path

import pytest
from shapely.geometry import box, mapping, shape

from app.modules.checkup.coverage_display import apply_display_policy, report_with_display_policy


def collection(geometry):
    return dict(type='FeatureCollection', features=[dict(type='Feature',geometry=mapping(geometry),
        properties=dict(facility_type='education_children'))])


def test_collapsed_saved_contour_rings_are_repaired_only_for_display():
    # Real cache reconstruction: rounded zero-area rings made GEOS union fail.
    row = json.loads((Path(__file__).parent / 'fixtures/community_display_invalid.json').read_text())
    layers = dict(group_coverage={'community_safety': row}, total_coverage=deepcopy(row))
    before = deepcopy(layers)
    result = apply_display_policy(layers)
    for record in [result['group_coverage']['community_safety'], result['total_coverage']]:
        assert all(shape(f['geometry']).is_valid and shape(f['geometry']).area > 0
                   for f in record['display_blind_spots']['features'])
        for key in ('blind_spots', 'unknown_areas', 'covered_ratio', 'blind_ratio', 'unknown_ratio'):
            assert record[key] == row[key]
    assert layers == before


def test_unknown_is_included_only_in_display_without_changing_evidence():
    row=dict(definition='all_categories_confirmed_blind',covered_ratio=.6,blind_ratio=.2,unknown_ratio=.2,
             blind_spots=collection(box(0,0,2,10)),unknown_areas=collection(box(2,0,4,10)))
    layers=dict(group_coverage={'education_children':row},total_coverage=deepcopy(row))
    before=deepcopy(layers)
    updated=apply_display_policy(layers)
    assert layers==before
    for fid,result in [('education_children',updated['group_coverage']['education_children']),('total',updated['total_coverage'])]:
        assert result['blind_ratio']==.2 and result['unknown_ratio']==.2
        assert result['display_policy']=='include_unknown' and result['display_blind_ratio']==pytest.approx(.4)
        feature=result['display_blind_spots']['features'][0]
        assert feature['properties']['facility_type']==fid  # Existing map parser requires this identity.
        assert shape(feature['geometry']).area==40
    assert apply_display_policy(updated)==updated


def test_saved_education_report_preserves_service_area_evidence_without_poi_cell_shortcuts():
    from pathlib import Path
    from app.schemas.checkup import CheckupJob
    p=Path(__file__).resolve().parents[1]/'data/reports/tongji-education-shared500-20261024_report.json'
    source=CheckupJob.model_validate_json(p.read_text()).result
    before=source.model_dump(mode='json')
    result=report_with_display_policy(source)
    row=result.blindspot['total_coverage']
    assert row['blind_ratio']==0
    assert row['unknown_ratio']==pytest.approx(.2575126635317553)
    assert row['display_blind_ratio']==pytest.approx(row['unknown_ratio'])
    assert 'poi_known_areas' not in row and 'display_poi_cell_m' not in row
    assert source.model_dump(mode='json')==before
    twice=report_with_display_policy(result)
    assert sum(w.code=='UNKNOWN_INCLUDED_IN_DISPLAY_BLIND' for w in twice.warnings)==1
    assert not any(w.code=='POI_PRESENCE_EXCLUDED_FROM_DISPLAY_BLIND' for w in twice.warnings)


def test_poi_coordinates_and_legacy_20m_cells_never_substitute_for_actual_service_areas():
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from types import SimpleNamespace
    from app.modules.checkup.service import _projection
    from app.modules.blindspot.service import _geojson
    from app.modules.isochrone.geometry import geometry_to_local
    from app.schemas.common import CenterPoint
    center = CenterPoint(lng=120, lat=30, crs='BD09LL')
    to_m, to_center = _projection(center)
    domain = box(-100, -100, 100, 100)
    geo = _geojson(domain, to_center)
    def row(fid):
        return dict(definition='all_categories_confirmed_blind', facility_ids=[fid],
            covered_ratio=0, blind_ratio=0, unknown_ratio=1,
            blind_spots=dict(type='FeatureCollection', features=[]), unknown_areas=collection(shape(geo)))
    layers = dict(group_coverage={'education_children':row('school'), 'health_medical':row('clinic')},
        total_coverage={**row('school'), 'facility_ids':['school','clinic']})
    pois = [dict(facility_id=fid, location=to_center(x, y).model_dump(mode='json'))
        for fid,x,y in [('school',0,0), ('school',0,0), ('clinic',40,0), ('clinic',500,0)]]
    before = deepcopy(layers)
    layers['poi_known_areas'] = collection(shape(_geojson(box(-10,-10,10,10),to_center)))
    result = apply_display_policy(layers, center=center, geometry=geo, pois=pois)
    school = result['group_coverage']['education_children']
    health = result['group_coverage']['health_medical']
    assert school['display_blind_ratio']==pytest.approx(1)
    assert health['display_blind_ratio']==pytest.approx(1)
    assert result['total_coverage']['display_blind_ratio']==pytest.approx(1)
    def local(collection):
        return unary_union([geometry_to_local(SimpleNamespace(**f['geometry']),to_m) for f in collection['features']])
    assert 'poi_known_areas' not in result and 'poi_known_areas' not in school
    assert local(school['display_blind_spots']).contains(Point(40,0))
    assert local(school['display_blind_spots']).covers(Point(0,0))
    assert local(health['display_blind_spots']).covers(Point(40,0))
    assert school['covered_ratio']==0 and school['unknown_ratio']==1
    assert {k:v for k,v in layers.items() if k != 'poi_known_areas'}==before


def test_get_and_terminal_actions_share_the_display_projection_without_mutating_storage(monkeypatch):
    import time
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.api import checkups
    from app.schemas.checkup import CheckupJob
    p=Path(__file__).resolve().parents[1]/'data/reports/tongji-education-shared500-20261024_report.json'
    job=CheckupJob.model_validate_json(p.read_text())
    original=job.model_dump(mode='json')
    monkeypatch.setattr(checkups,'_jobs',{job.id:(time.monotonic(),job)})
    with TestClient(app) as client:
        response=client.get('/api/v1/checkups/'+job.id).json()
        for action in ('pause','resume','cancel'):
            assert client.post('/api/v1/checkups/'+job.id+'/'+action,json={}).json()==response
    assert job.model_dump(mode='json')==original
