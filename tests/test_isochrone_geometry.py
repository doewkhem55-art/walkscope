"""MultiPolygon isochrone geometry + grid generator contracts."""

import math

import pytest
from pydantic import ValidationError
from shapely.geometry import Point, shape

from app.modules.isochrone.geometry import geometry_to_local
from app.modules.isochrone.grid import generate_grid_isochrone
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.common import CenterPoint, Meta
from app.schemas.isochrone import (
    GeoJSONMultiPolygon,
    GeoJSONPolygon,
    IsochroneConfidence,
    IsochroneData,
    IsochroneQuality,
    IsochroneRequest,
    IsochroneSuccess,
)
from app.schemas.routing import (
    RouteResult,
    RouteStatus,
    RoutingData,
    RoutingRequest,
    RoutingResult,
    RoutingSuccess,
    RoutingSummary,
)

CENTER = CenterPoint(lng=121.508602, lat=31.288268, crs="BD09LL")
SQUARE_A = [[121.500, 31.280], [121.510, 31.280], [121.510, 31.290], [121.500, 31.290], [121.500, 31.280]]
SQUARE_B = [[121.520, 31.300], [121.530, 31.300], [121.530, 31.310], [121.520, 31.310], [121.520, 31.300]]


def _quality(**overrides):
    payload = dict(
        requested_directions=0,
        valid_boundary_directions=0,
        routing_sample_count=10,
        routing_success_count=10,
        sample_success_rate=1.0,
        confidence=IsochroneConfidence.MEDIUM,
        unknown_ratio=0.0,
        grid_cell_m=100.0,
        algorithm_version="grid-marching-v1",
    )
    payload.update(overrides)
    return IsochroneQuality(**payload)


def test_multipolygon_with_hole_is_accepted_for_grid_algorithm() -> None:
    outer = [[121.50, 31.28], [121.52, 31.28], [121.52, 31.30], [121.50, 31.30], [121.50, 31.28]]
    hole = [[121.505, 31.285], [121.505, 31.295], [121.515, 31.295], [121.515, 31.285], [121.505, 31.285]]
    geometry = GeoJSONMultiPolygon(type="MultiPolygon", coordinates=[[outer, hole], [SQUARE_A]])
    data = IsochroneData(
        center=CENTER, time_limit_s=900, geometry=geometry,
        algorithm_version="grid-marching-v1", boundary_points=[], quality=_quality(),
    )
    assert data.geometry.type == "MultiPolygon"
    assert len(data.geometry.coordinates) == 2


def test_radial_algorithm_rejects_multipolygon() -> None:
    geometry = GeoJSONMultiPolygon(type="MultiPolygon", coordinates=[[SQUARE_A], [SQUARE_B]])
    with pytest.raises(ValidationError):
        IsochroneData(
            center=CENTER, time_limit_s=900, geometry=geometry,
            algorithm_version="radial-v1", boundary_points=[], quality=_quality(),
        )


def test_geometry_to_local_keeps_holes_and_multiple_polygons() -> None:
    outer = [[121.50, 31.28], [121.52, 31.28], [121.52, 31.30], [121.50, 31.30], [121.50, 31.28]]
    hole = [[121.505, 31.285], [121.505, 31.295], [121.515, 31.295], [121.515, 31.285], [121.505, 31.285]]
    far = [[121.54, 31.32], [121.55, 31.32], [121.55, 31.33], [121.54, 31.33], [121.54, 31.32]]
    geometry = GeoJSONMultiPolygon(type="MultiPolygon", coordinates=[[outer, hole], [far]])
    cosine = math.cos(math.radians(CENTER.lat))

    def to_m(lng: float, lat: float) -> tuple[float, float]:
        return (
            math.radians(lng - CENTER.lng) * 6_371_008.8 * cosine,
            math.radians(lat - CENTER.lat) * 6_371_008.8,
        )

    local = geometry_to_local(geometry, to_m)
    assert local.geom_type == "MultiPolygon"
    assert len(list(local.geoms)) == 2
    outer_area = math.radians(0.02) ** 2 * 6_371_008.8 ** 2 * cosine
    hole_area = math.radians(0.01) ** 2 * 6_371_008.8 ** 2 * cosine
    assert abs(local.area - (outer_area - hole_area + 0)) < 0.3 * outer_area


def test_poi_radius_uses_farthest_ring_of_multipolygon() -> None:
    geometry = GeoJSONMultiPolygon(type="MultiPolygon", coordinates=[[SQUARE_A], [SQUARE_B]])
    radius = minimum_poi_search_radius_m(CENTER, geometry, 1000)
    assert radius > 2500  # SQUARE_B is far from the centre


class _FakeRoute:
    def __call__(self, request: RoutingRequest) -> RoutingResult:
        routes = []
        for target in request.targets:
            distance = int(
                math.hypot(
                    math.radians(target.location.lng - request.origin.lng) * 6_371_008.8 * math.cos(math.radians(request.origin.lat)),
                    math.radians(target.location.lat - request.origin.lat) * 6_371_008.8,
                )
            )
            routes.append(RouteResult(
                target_id=target.target_id, location=target.location,
                status=RouteStatus.SUCCESS, distance_m=distance,
                duration_s=max(1, int(distance / 1.4)),
            ))
        data = RoutingData(
            origin=request.origin, travel_mode="walking", routes=routes,
            summary=RoutingSummary(requested=len(routes), success=len(routes), no_route=0, unavailable=0),
        )
        return RoutingResult(root=RoutingSuccess(
            ok=True, data=data, warnings=[], meta=Meta(schema_version="1.0", provider="baidu"),
        ))


def test_grid_generator_returns_algorithm_version_and_no_boundary_points() -> None:
    result = generate_grid_isochrone(
        _FakeRoute(), IsochroneRequest(schema_version="1.0", center=CENTER, time_limit_s=900),
        radius_m=300.0, cell_m=100.0,
    )
    assert isinstance(result.root, IsochroneSuccess)
    data = result.root.data
    assert data.algorithm_version == "grid-marching-v1"
    assert data.boundary_points == []
    assert data.quality.algorithm_version == "grid-marching-v1"
    assert data.quality.requested_directions == 0
    assert data.quality.unknown_ratio == 0.0
    assert data.geometry.type in {"Polygon", "MultiPolygon"}


@pytest.mark.parametrize("include_tiers", [False, True])
def test_grid_generator_preserves_a_single_polygon_hole_in_the_report_contract(include_tiers) -> None:
    def route_with_unreachable_patch(request: RoutingRequest) -> RoutingResult:
        result = _FakeRoute()(request)
        # One enclosed measured node (100 m east of centre) takes over 15 minutes.
        for route in result.root.data.routes:
            if route.target_id == "grid:r0003:c0004":
                route.duration_s = 1200
        return result

    result = generate_grid_isochrone(
        route_with_unreachable_patch,
        IsochroneRequest(schema_version="1.0", center=CENTER, time_limit_s=900),
        radius_m=300.0, cell_m=100.0, include_tiers=include_tiers,
    )

    assert isinstance(result.root, IsochroneSuccess)
    geometry = result.root.data.geometry
    assert geometry.type == "MultiPolygon"
    assert len(geometry.coordinates) == 1
    polygon = shape(geometry.model_dump(mode="json")).geoms[0]
    assert len(polygon.interiors) == 1
    assert polygon.covers(Point(CENTER.lng, CENTER.lat))
    hole_lng = CENTER.lng + math.degrees(100 / (6_371_008.8 * math.cos(math.radians(CENTER.lat))))
    assert not polygon.covers(Point(hole_lng, CENTER.lat))
