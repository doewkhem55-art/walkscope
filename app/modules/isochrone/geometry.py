"""Helpers for consuming Polygon / MultiPolygon isochrone geometry."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.schemas.isochrone import IsochroneGeometry


def iter_rings(geometry: IsochroneGeometry) -> Iterator[list[list[float]]]:
    """Yield every ring (exterior and holes) of a Polygon or MultiPolygon."""
    if geometry.type == "Polygon":
        assert isinstance(geometry.coordinates, list)
        yield from geometry.coordinates
        return
    for polygon in geometry.coordinates:
        yield from polygon


def ring_points(geometry: IsochroneGeometry) -> Iterator[tuple[float, float]]:
    for ring in iter_rings(geometry):
        for point in ring:
            yield float(point[0]), float(point[1])


def geometry_to_local(
    geometry: IsochroneGeometry,
    to_m: Callable[[float, float], tuple[float, float]],
) -> BaseGeometry:
    """Project all polygons and holes into the local metre plane."""
    if geometry.type == "Polygon":
        polygon_groups = [geometry.coordinates]
    else:
        polygon_groups = geometry.coordinates
    polygons = []
    for rings in polygon_groups:
        exterior = [to_m(lng, lat) for lng, lat in rings[0]]
        holes = [[to_m(lng, lat) for lng, lat in ring] for ring in rings[1:]]
        polygons.append(Polygon(exterior, holes))
    return unary_union(polygons) if len(polygons) > 1 else polygons[0]
