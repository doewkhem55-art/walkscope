"""POI-origin walking service areas, clipped to a 20 m representative-point grid.

Only measured walking distances establish coverage. Euclidean distance merely
rules out impossible candidates; unmeasured/unavailable candidates stay unknown.
The polygons are a grid estimate, not continuous road-network isochrones.
"""

import math
from collections import deque
from itertools import zip_longest
from types import SimpleNamespace

import numpy as np
from shapely import STRtree
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from app.errors import ProviderStopError
from app.modules.blindspot.service import _geojson, _measure
from app.schemas.common import CenterPoint, WarningItem
from app.schemas.routing import RouteStatus


METHOD = "poi_origin_service_area_union_difference_v1"
_BATCH_SIZE = 16


def analyze_poi_service_areas(domain, cells, to_m, to_center, categories, catalog,
                              route, warnings, *, max_route_pairs=5000,
                              progress=lambda _: None):
    """Return category ratios, service-area layers, and honest evidence quality.

    Memory is O(categories*cells + POIs + route budget), never POIs*cells.
    A spatial index is queried lazily per origin; no global candidate-pair set.
    The caller validates the domain/grid and guards provider quota envelopes.
    """
    definitions = {spec["id"]: spec for spec in catalog["facilities"]}
    points = [cell.representative_point() for _, cell in cells]
    xy = np.array([(point.x, point.y) for point in points], dtype=float)
    tree = STRtree(points)
    progress(0.05)  # Representative points and their spatial index are ready.
    n = len(cells)
    supported = {}
    origins = {}
    category_origins = []
    for category in categories:
        fid = category["facility_id"]
        spec = definitions[fid]
        radius = spec.get("service_distance_m")
        supported[fid] = (spec.get("coverage_method") == "grid_walking_distance"
                          and isinstance(radius, (int, float)) and not isinstance(radius, bool)
                          and math.isfinite(radius) and radius > 0)
        keys = []
        if supported[fid]:
            seen = set()
            for raw in category["pois"]:
                location = CenterPoint.model_validate(raw["location"])
                key = (raw["poi_id"], location.lng, location.lat, location.crs)
                if key in seen:
                    continue
                seen.add(key)
                if key not in origins:
                    origins[key] = dict(poi_id=raw["poi_id"], location=location,
                                        xy=to_m(location.lng, location.lat), radii={},
                                        cursor=-1, measured={})
                origins[key]["radii"][fid] = radius
                keys.append(key)
        category_origins.append(keys)

    # Round-robin category frontiers, deduplicating only identical ID+location.
    queue = deque()
    queued = set()
    for row in zip_longest(*category_origins):
        for key in row:
            if key is not None and key not in queued:
                queued.add(key)
                queue.append(key)

    def candidates(origin, radius):
        x, y = origin["xy"]
        window = radius + 1e-6
        indices = tree.query(box(x - window, y - window, x + window, y + window))
        delta = xy[indices] - (x, y)
        indices = indices[np.einsum("ij,ij->i", delta, delta) <= (radius + 1e-6) ** 2]
        return np.sort(indices)

    progress(0.1)  # POI origins, thresholds, and fair routing order are ready.
    requested = unavailable = succeeded = no_route = batches = 0
    stopped = None
    while queue and requested < max_route_pairs:
        origin = origins[queue.popleft()]
        indices = candidates(origin, max(origin["radii"].values()))
        remaining = indices[indices > origin["cursor"]]
        if not len(remaining):
            continue
        # Give every active origin a turn before spending another large batch.
        allowance = min(_BATCH_SIZE, math.ceil((max_route_pairs - requested) / (len(queue) + 1)))
        selected_indices = remaining[:allowance]
        selected = [SimpleNamespace(poi_id=f"service-cell:{cells[int(i)][0]}",
                                    location=to_center(*xy[i])) for i in selected_indices]
        batches += 1
        try:
            results = _measure(origin["location"], selected, route, warnings)
        except ProviderStopError as exc:
            stopped = exc.code
            break
        requested += len(selected)
        for index, target in zip(selected_indices, selected):
            result = results.get(target.poi_id)
            if result is None or result.status == RouteStatus.UNAVAILABLE:
                distance = None
                unavailable += 1
            elif result.status == RouteStatus.NO_ROUTE:
                distance = math.inf
                no_route += 1
            else:
                distance = result.distance_m
                succeeded += 1
            origin["measured"][int(index)] = distance
        origin["cursor"] = int(selected_indices[-1])
        if len(remaining) > len(selected_indices):
            # Store only a cursor, not the origin's potentially 100k candidate list.
            queue.append((origin["poi_id"], origin["location"].lng,
                                      origin["location"].lat, origin["location"].crs))
        progress(0.1 + 0.65 * requested / max(1, max_route_pairs))

    covered = {category["facility_id"]: np.zeros(n, dtype=bool) for category in categories}
    potential = {category["facility_id"]: np.full(
        n, not supported[category["facility_id"]] or category["search_status"] != "complete", dtype=bool)
        for category in categories}
    poi_features = []
    candidate_pairs = untested_pairs = 0

    def region(indices):
        if len(indices) == n:
            return domain
        return unary_union([cells[int(i)][1] for i in indices]) if len(indices) else Polygon()

    def feature(geometry, properties):
        return {"type": "Feature", "geometry": _geojson(geometry, to_center), "properties": properties}

    for number, origin in enumerate(origins.values()):
        indices = candidates(origin, max(origin["radii"].values()))
        candidate_pairs += len(indices)
        untested_pairs += len(indices) - len(origin["measured"])
        # Finite distances or infinity prove an attempted candidate's membership;
        # None and absent entries retain possible service coverage.
        distances = np.array([origin["measured"].get(int(i), math.nan) for i in indices], dtype=float)
        unresolved = np.isnan(distances)
        delta = xy[indices] - origin["xy"]
        straight_squared = np.einsum("ij,ij->i", delta, delta)
        by_radius = {}
        for fid, radius in origin["radii"].items():
            eligible = straight_squared <= (radius + 1e-6) ** 2
            known_indices = indices[eligible & (distances <= radius)]
            covered[fid][known_indices] = True
            potential[fid][indices[eligible & unresolved]] = True
            by_radius.setdefault(radius, []).append(fid)
        for radius, fids in by_radius.items():
            known_indices = indices[(straight_squared <= (radius + 1e-6) ** 2) & (distances <= radius)]
            if len(known_indices):
                poi_features.append(feature(region(known_indices), {
                    "poi_id": origin["poi_id"], "facility_ids": fids,
                    "facility_type": fids[0], "service_distance_m": radius,
                    "classification": "covered", "calculation_method": METHOD,
                }))
        if number % 100 == 0:
            progress(0.75 + 0.1 * (number + 1) / max(1, len(origins)))

    empty = Polygon()
    regions = {fid: (region(np.flatnonzero(mask)), region(np.flatnonzero(potential[fid])))
               for fid, mask in covered.items()}
    progress(0.86)  # Category service-area cells have been dissolved.

    def collection(geometry, fid, classification):
        return {"type": "FeatureCollection", "features": [] if geometry.is_empty else [
            feature(geometry, {"facility_type": fid, "classification": classification})]}

    def record(fid, facility_ids):
        # Explicit service-area UNION, then DIFFERENCE against the analysis domain.
        known = unary_union([regions[item][0] for item in facility_ids]).intersection(domain)
        possible = unary_union([regions[item][1] for item in facility_ids]).intersection(domain)
        if not facility_ids:
            known, possible = empty, domain
        unknown = possible.difference(known)
        blind = domain.difference(known.union(possible))
        covered_ratio = min(1.0, max(0.0, known.area / domain.area))
        blind_ratio = min(1.0 - covered_ratio, max(0.0, blind.area / domain.area))
        return {"definition": "all_categories_confirmed_blind", "calculation_method": METHOD,
                "facility_ids": facility_ids,
                "covered_areas": collection(known, fid, "covered"),
                "blind_spots": collection(blind, fid, "blind"),
                "unknown_areas": collection(unknown, fid, "unknown"),
                "covered_ratio": covered_ratio, "blind_ratio": blind_ratio,
                "unknown_ratio": 0.0 if unknown.is_empty else max(0.0, 1.0 - covered_ratio - blind_ratio)}

    layers = {key: {"type": "FeatureCollection", "features": []}
              for key in ("blind_spots", "unknown_areas", "covered_areas")}
    ratios = {}
    groups = {}
    for number, category in enumerate(categories):
        fid = category["facility_id"]
        result = record(fid, [fid])
        ratios[fid] = {key: result[key] for key in ("covered_ratio", "blind_ratio", "unknown_ratio")}
        for key in layers:
            layers[key]["features"].extend(result[key]["features"])
        groups.setdefault(definitions[fid]["group_id"], []).append(fid)
        progress(0.86 + 0.1 * (number + 1) / max(1, len(categories)))
    layers["poi_service_areas"] = {"type": "FeatureCollection", "features": poi_features}
    layers["group_coverage"] = {gid: record(gid, ids) for gid, ids in groups.items()}
    layers["total_coverage"] = record("total", list(ratios))
    exhausted = bool(untested_pairs and requested >= max_route_pairs and stopped is None)
    unknown_ratio = max((ratios[fid]["unknown_ratio"] for fid in ratios if supported[fid]), default=1.0)
    if exhausted:
        warnings.append(WarningItem(code="ROUTING_BUDGET_EXCEEDED", message="设施服务区路线达到预算；未测范围保持未知，不作为盲区。"))
    if stopped is not None:
        warnings.append(WarningItem(code=stopped, message="设施服务区路线请求已停止；未测范围保持未知，不再发起后续路线请求。"))
    if unavailable:
        warnings.append(WarningItem(code="PARTIAL_ROUTING_RESULTS", message="部分设施出发的步行路线不可用，对应范围保持未知。"))
    if unknown_ratio > 1e-9:
        warnings.append(WarningItem(code="UNKNOWN_COVERAGE", message="存在证据不足的覆盖区域，不能将其认定为盲区。"))
    progress(1.0)
    return ratios, layers, {
        "grid_cell_count": n, "grid_size_m": 20,
        "coverage_route_pairs": requested, "coverage_budget_exhausted": exhausted,
        "coverage_route_unavailable": unavailable, "coverage_stop_reason": stopped,
        "max_supported_category_unknown_ratio": unknown_ratio,
        "coverage_method": METHOD, "coverage_route_direction": "poi_to_grid",
        "coverage_precision": "20m_clipped_representative_point_estimate",
        "coverage_poi_origin_count": len(origins), "coverage_candidate_route_pairs": candidate_pairs,
        "coverage_untested_route_pairs": untested_pairs, "coverage_route_succeeded": succeeded,
        "coverage_route_no_route": no_route, "coverage_route_batches": batches,
    }
