"""20 m checkup coverage, bounded routes and allocation; all providers are mock."""
import inspect

import pytest
from shapely.geometry import shape

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.service import (
    COVERAGE_GRID_SIZE_M, CheckupError, _projection, analyze_coverage, run_checkup,
)
from app.schemas.isochrone import GeoJSONPolygon
from app.schemas.routing import RouteStatus
from test_checkup_service import CENTER, FakeRoute


def square(half_m):
    _, to_center = _projection(CENTER)
    corners = [(-half_m, -half_m), (half_m, -half_m), (half_m, half_m), (-half_m, half_m), (-half_m, -half_m)]
    return GeoJSONPolygon(type="Polygon", coordinates=[[
        [point.lng, point.lat] for x, y in corners for point in [to_center(x, y)]
    ]])


def category(*, with_poi=True):
    return {"facility_id": "childcare_0_3", "search_status": "complete", "pois": [
        {"poi_id": "mock-near", "location": CENTER.model_dump()}
    ] if with_poi else []}


def test_output_grid_is_20m_but_routes_use_actual_poi_adaptive_samples():
    route = FakeRoute([], grid_status=RouteStatus.SUCCESS)
    ratios, _, quality = analyze_coverage(CENTER, square(40), [category()], load_catalog(), route, [])
    assert COVERAGE_GRID_SIZE_M == quality['grid_size_m'] == 20
    assert quality['grid_cell_count'] == 25
    assert quality['coverage_route_pairs'] < 25
    assert quality['coverage_origin_grid_m'] is None
    assert quality['coverage_origin_mode'] == 'actual_poi'
    assert quality['coverage_sampling_fine_m'] == 50
    assert all(t.target_id.startswith('adaptive:') for r in route.requests for t in r.targets)
    assert ratios['childcare_0_3']['covered_ratio'] == pytest.approx(1)


def test_finer_grid_keeps_the_route_budget_and_unmeasured_cells_unknown():
    route = FakeRoute([], grid_status=RouteStatus.NO_ROUTE, center_status=RouteStatus.NO_ROUTE)
    warnings = []
    ratios, layers, quality = analyze_coverage(CENTER, square(40), [category()], load_catalog(), route, warnings,
                                             max_route_pairs=1)
    assert quality["coverage_route_pairs"] == len(route.requests) == 1
    assert quality["coverage_budget_exhausted"] is True
    state = ratios["childcare_0_3"]
    assert state["blind_ratio"] == 0 and state["unknown_ratio"] == 1
    assert sum(state.values()) == pytest.approx(1)
    assert layers['blind_spots']['features'] == []
    assert layers['unknown_areas']['features']
    assert any(w.code == 'ROUTING_BUDGET_EXCEEDED' for w in warnings)
    assert inspect.signature(analyze_coverage).parameters['max_route_pairs'].default == 5000
    assert inspect.signature(run_checkup).parameters['max_grid_routes'].default == 5000


def test_a_normal_fine_grid_can_exceed_the_old_10000_cell_limit():
    def forbidden(_):
        pytest.fail('a complete empty category needs no paid routing')
    _, _, quality = analyze_coverage(CENTER, square(1010), [category(with_poi=False)], load_catalog(), forbidden, [])
    assert quality['grid_cell_count'] > 10000
    assert quality['grid_size_m'] == 20
    assert quality['coverage_route_pairs'] == 0


def test_oversized_fine_grid_is_rejected_before_allocating_cells(monkeypatch):
    def forbidden(*_):
        pytest.fail('reject oversized grid before constructing its polygons')
    monkeypatch.setattr('app.modules.checkup.service._grid_cells', forbidden)
    with pytest.raises(CheckupError, match='20米网格'):
        analyze_coverage(CENTER, square(4000), [category()], load_catalog(), forbidden, [])


def test_three_categories_in_one_grid_keep_their_distinct_actual_poi_origins():
    _, to_center = _projection(CENTER)
    corners = [(-29, -9), (29, -9), (29, 9), (-29, 9), (-29, -9)]
    geometry = GeoJSONPolygon(type='Polygon', coordinates=[[
        [point.lng, point.lat] for x, y in corners for point in [to_center(x, y)]
    ]])
    ids = ['childcare_0_3', 'kindergarten', 'primary_school']
    origins = [to_center(index, 0) for index in (-1, 0, 1)]
    categories = [dict(category(), facility_id=fid, pois=[
        {'poi_id': fid, 'location': origin.model_dump()}
    ]) for fid, origin in zip(ids, origins)]
    route = FakeRoute([], grid_status=RouteStatus.NO_ROUTE, center_status=RouteStatus.NO_ROUTE)
    ratios, _, quality = analyze_coverage(CENTER, geometry, categories, load_catalog(), route, [], max_route_pairs=3)
    assert quality['grid_cell_count'] == quality['coverage_route_pairs'] == 3
    assert len(route.requests) == 1
    assert quality['coverage_poi_origin_count'] == 3
    assert quality['coverage_shared_category_uses'] == 3
    assert quality['coverage_budget_exhausted'] is True
    assert all(ratios[fid]['blind_ratio'] == 0 and ratios[fid]['unknown_ratio'] == 1 for fid in ids)
