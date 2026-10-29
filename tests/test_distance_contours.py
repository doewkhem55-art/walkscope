import math

import pytest
from shapely.geometry import Point, Polygon, box

from app.modules.blindspot.service import _grid_cells
from app.modules.checkup.distance_contours import clip_distance_triangle, measured_distance_contour
from app.modules.checkup.representative_service_areas import analyze_representative_service_areas
from test_poi_service_areas import Oracle, category, local_region, poi, spec, to_center, to_m


def test_1000m_crossings_are_interpolated_on_measured_edges_without_cell_snapping():
    service, crossings = clip_distance_triangle([(0, 0), (200, 0), (0, 200)], [800, 1200, 800])
    assert set(crossings) == {(100, 0), (100, 100)}
    assert service.area == pytest.approx(15000)
    assert service.covers(Point(99, 99))
    assert not service.covers(Point(101, 50))
    missing, crossings = clip_distance_triangle([(0, 0), (200, 0), (0, 200)], [800, math.nan, 800])
    assert missing.is_empty and crossings == []


def test_contour_keeps_diagonal_boundary_and_domain_holes():
    domain = box(-100, -100, 100, 100).difference(box(-80, -20, -60, 20))
    tiles = [(a, b, 200) for a in (-200, 0) for b in (-200, 0)]
    nodes = {xy for a,b,size in tiles for xy in
             [(a,b),(a+size,b),(a+size,b+size),(a,b+size),(a+size/2,b+size/2)]}
    measured = {xy: 1000+xy[0]+.5*xy[1] for xy in nodes}
    service, evidence, _, segments = measured_distance_contour(domain, (0,0), measured, tiles)
    expected = Polygon([(-100,-100),(50,-100),(-50,100),(-100,100)]).intersection(domain)
    assert service.symmetric_difference(expected).area < 1e-6
    assert evidence.equals(domain)
    assert len(segments) > 0
    assert not service.covers(Point(-70, 0))
    assert not service.covers(Point(11, -20))


def test_missing_mesh_node_cannot_be_bridged_by_a_convex_hull_or_fake_circle():
    domain = box(0, 0, 200, 200)
    measured = {xy: 100 for xy in [(0,0),(200,0),(200,200),(0,200)]}
    service, evidence, candidate, segments = measured_distance_contour(domain, (0,0), measured, [(0,0,200)])
    assert service.is_empty and evidence.is_empty and segments == []
    assert candidate.equals(domain)


def test_unmeasured_neighbour_tile_cannot_erase_a_fully_measured_tile():
    tile=box(0,0,200,200)
    domain=tile.union(box(200,90,202,110))
    measured={xy:100 for xy in [(0,0),(200,0),(200,200),(0,200),(100,100)]}
    service,evidence,_,_=measured_distance_contour(domain,(150,86),measured,
        [(0,0,200),(200,0,200)],origin_zero_anchor=True)
    assert service.intersection(tile).area==pytest.approx(tile.area,abs=1e-6)
    assert evidence.intersection(tile).area==pytest.approx(tile.area,abs=1e-6)
    assert service.intersection(box(200,90,202,110)).area==0


def test_actual_service_union_is_contoured_before_domain_subtraction_independent_of_output_grid():
    domain = box(-100, -100, 100, 100)
    geometries = []
    for grid in (20, 37):
        route = Oracle(lambda origin,target: ('success', round(
            1000+to_m(target.location.lng,target.location.lat)[0]
            +to_m(target.location.lng,target.location.lat)[1]), 1))
        ratios, layers, quality = analyze_representative_service_areas(domain, _grid_cells(domain,grid),
            to_m,to_center,[category('a',[poi('one',-500,0)])], {'facilities':[spec('a')]},
            route,[],origin_mode='actual_poi')
        covered = local_region(layers['total_coverage']['covered_areas'])
        geometries.append(covered)
        blind = local_region(layers['total_coverage']['blind_spots'])
        assert ratios['a']['covered_ratio'] == pytest.approx(.5,abs=1e-7)
        assert ratios['a']['unknown_ratio'] < 1e-7
        assert blind.symmetric_difference(domain.difference(covered)).area < 1e-5
        assert quality['coverage_geometry_grid_m'] is None
        assert quality['coverage_contour_segment_count'] > 0
    assert geometries[0].symmetric_difference(geometries[1]).area < 1e-5
