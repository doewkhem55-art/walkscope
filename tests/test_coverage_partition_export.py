"""Small geometry regressions extracted from the Zhongnan coverage audit."""
import json
import gzip
from pathlib import Path

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import unary_union

from app.modules.blindspot.service import _geojson, _grid_cells
from app.modules.checkup.representative_service_areas import analyze_representative_service_areas
from test_poi_service_areas import category, local_region, poi, spec, to_center, to_m


def test_shared_boundary_union_does_not_crash_on_real_saved_contours():
    from app.modules.checkup.representative_service_areas import _clipped_coverage_union

    path=Path(__file__).parent/'fixtures/zhongnan_union_ring_missing.json.gz'
    parts=[shape(row) for row in json.loads(gzip.decompress(path.read_bytes()))]
    domain=box(-2500,-2500,2500,2500)
    result=_clipped_coverage_union(parts,domain)
    assert result.is_valid and result.area>0
    assert result.geom_type in {'Polygon','MultiPolygon'}
    assert result.difference(domain).area==0


def test_combined_unknown_is_clipped_to_domain_after_overlay_roundoff(monkeypatch):
    import app.modules.checkup.representative_service_areas as engine

    raw = json.loads((Path(__file__).parent / 'fixtures/zhongnan_union_domain_rounding.json').read_text())
    domain = shape(raw['domain'])
    parts = [shape(value) for value in raw['unknown_parts']]
    assert all(part.is_valid and part.difference(domain).area < 1e-6 for part in parts)
    # The two individually clipped real polygons provoke an 88.8 m2 GEOS
    # union artefact outside the domain; the final partition must guard it.
    assert unary_union(parts).difference(domain).area > 80
    results = iter(parts)
    monkeypatch.setattr(engine, 'measured_distance_contour',
        lambda *_args, **_kwargs: (Polygon(), Polygon(), next(results), []))
    point = domain.representative_point()
    rows = [category('school', [poi('one', point.x, point.y), poi('two', point.x+.01, point.y)])]
    def forbidden(_):
        raise AssertionError('No provider access in a geometry-only regression')
    ratios, layers, _ = analyze_representative_service_areas(domain, _grid_cells(domain, 20),
        to_m, to_center, rows, {'facilities': [spec('school')]}, forbidden, [],
        max_route_pairs=0, origin_mode='actual_poi')
    for record in [layers['group_coverage']['education'], layers['total_coverage']]:
        unknown = local_region(record['unknown_areas'])
        assert unknown.difference(domain).area < 1e-5
        assert unknown.area/domain.area == pytest.approx(record['unknown_ratio'], abs=1e-7)
    assert ratios['school']['covered_ratio'] == 0


@pytest.mark.parametrize('tiny_hole', [True, False])
def test_geojson_roundtrip_removes_collapsed_rings_without_changing_real_area(tiny_hole):
    tiny = Polygon([(1,1), (1+1e-12,1), (1,1+1e-12)])
    domain = (Polygon(box(0,0,100,100).exterior.coords, holes=[tiny.exterior.coords])
        if tiny_hole else MultiPolygon([box(10,10,100,100), tiny]))
    assert domain.is_valid
    exported = shape(_geojson(domain, to_center))
    assert exported.is_valid
    assert local_region({'features': [{'geometry': _geojson(domain,to_center)}]}).area == pytest.approx(domain.area, abs=1e-5)
