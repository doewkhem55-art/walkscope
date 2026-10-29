"""Continuous 1000 m contours from measured walking-distance triangles."""
import math

from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import triangulate, unary_union


def clip_distance_triangle(coords, values, limit=1000):
    """Clip a linear triangle field at the distance threshold, not a cell edge."""
    if not all(math.isfinite(value) for value in values):
        return Polygon(), []
    vertices, crossings = [], []
    for index, (point, value) in enumerate(zip(coords, values)):
        following, next_value = coords[(index+1) % 3], values[(index+1) % 3]
        if value <= limit:
            vertices.append(point)
        if (value <= limit) != (next_value <= limit):
            fraction = (limit-value)/(next_value-value)
            crossing = tuple(point[axis] + fraction*(following[axis]-point[axis]) for axis in (0, 1))
            vertices.append(crossing)
            if crossing not in crossings:
                crossings.append(crossing)
    geometry = Polygon(vertices) if len(set(vertices)) >= 3 else Polygon()
    return geometry, crossings


def measured_distance_contour(domain, origin, measured, base_tiles, *, limit=1000, coarse_m=200,
                              origin_zero_anchor=False):
    """Use only finite measured triangles; missing nodes remain holes in evidence.

    The coarse nodes anchor the sampling mesh, including its missing values.
    They prevent triangulation from bridging an unmeasured interior or gap.
    The returned range is only a necessary straight-line bound on *unknown*
    coverage, never an invented walking service circle.
    """
    if origin_zero_anchor:
        # Identity constraint, not an API measurement or a service-radius disk.
        # Missing mesh neighbours still block triangulation in exactly the same way.
        measured = dict(measured)
        measured[tuple(round(v,6) for v in origin)] = 0.0
    nodes = set(measured)
    for a, b, size in base_tiles:
        nodes.update(((a,b), (a+size,b), (a+size,b+size), (a,b+size), (a+size/2,b+size/2)))
    # Circumscribe the mathematical 1 km bound so chord approximation cannot
    # incorrectly declare a thin edge strip unreachable.
    candidate = domain.intersection(Point(*origin).buffer(limit/math.cos(math.pi/512), quad_segs=128))
    service, evidence, contours = [], [], []
    if len(nodes) < 3:
        return Polygon(), Polygon(), candidate, contours

    def measured_mesh_triangles():
        # Keep the measured coarse-tile edges as mesh constraints. A global
        # Delaunay edge can cross into an unmeasured neighbouring tile and erase
        # already measured interior area, even when all five local nodes exist.
        # Missing nodes inside each tile remain in that tile's mesh and still
        # prevent interpolation across an unmeasured hole.
        if not base_tiles:
            yield from triangulate(MultiPoint(sorted(nodes)))
        for a,b,size in base_tiles:
            local=[xy for xy in nodes if a <= xy[0] <= a+size and b <= xy[1] <= b+size]
            if sum(math.isfinite(measured.get(xy,math.nan)) for xy in local) >= 3:
                yield from triangulate(MultiPoint(sorted(local)))

    for triangle in measured_mesh_triangles():
        coords = [tuple(round(v, 6) for v in xy) for xy in list(triangle.exterior.coords)[:3]]
        values = [measured.get(xy, math.nan) for xy in coords]
        if not all(math.isfinite(value) for value in values):
            continue
        if any(math.dist(coords[index], coords[(index+1) % 3]) > coarse_m*math.sqrt(2)+1e-5
               for index in range(3)):
            continue
        part = triangle.intersection(candidate)
        if part.is_empty or part.area <= 0:
            continue
        evidence.append(part)
        inside, crossings = clip_distance_triangle(coords, values, limit)
        if not inside.is_empty:
            service.append(inside.intersection(candidate))
        if len(crossings) == 2:
            contours.append(crossings)
    return unary_union(service), unary_union(evidence), candidate, contours
