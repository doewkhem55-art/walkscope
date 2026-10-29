"""Adaptive measured-distance service areas from real or legacy shared origins.

Actual-origin geometry is a continuous measured-distance contour. The 20 m
cells assist sampling only; legacy shared-origin reports retain cell geometry.
"""
import math
import hashlib
import json
from types import SimpleNamespace

import numpy as np
from shapely import union_all
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from app.errors import ProviderStopError
from app.modules.blindspot.service import _geojson, _grid_cells, _measure, _polygon_parts
from app.modules.checkup.distance_contours import measured_distance_contour
from app.schemas.common import CenterPoint, WarningItem
from app.schemas.routing import RouteStatus

_OVERLAY_PRECISION_M = 1e-6


def _clipped_coverage_union(parts, domain):
    # Shared contour edges differ by floating-point ulps. A micrometre overlay
    # precision stabilizes topology; it does not resample the distance field.
    merged = union_all(parts, grid_size=_OVERLAY_PRECISION_M)
    polygonal = MultiPolygon(_polygon_parts(merged))
    clipped = polygonal.intersection(domain, grid_size=_OVERLAY_PRECISION_M)
    return MultiPolygon(_polygon_parts(clipped))


METHOD = "shared_500m_origins_adaptive_1km_service_area_v1"
LEGACY_ACTUAL_POI_METHOD = "actual_poi_origins_adaptive_1km_service_area_v1"
ACTUAL_POI_METHOD = "actual_poi_origins_measured_contour_1km_service_area_v2"
ORIGIN_GRID_M = 500
SERVICE_DISTANCE_M = 1000
COARSE_M = 200
FINE_M = 50


def build_origins(domain, categories, definitions, to_m):
    """Half-open lattice cells centred on multiples of 500 m from the centre.

    A single deterministic point in the clipped cell is shared by all member
    categories. Only POIs inside the analysis domain establish membership.
    """
    cells = dict(_grid_cells(domain, ORIGIN_GRID_M))
    origins = {}
    unique = set()
    outside = 0
    for category in categories:
        fid = category['facility_id']
        if definitions[fid].get('coverage_method') != 'grid_walking_distance':
            continue
        for raw in category['pois']:
            location = CenterPoint.model_validate(raw['location'])
            x, y = to_m(location.lng, location.lat)
            if not domain.covers(Point(x, y)):
                outside += 1
                continue
            row, col = math.floor(round((y + 250) / 500, 9)), math.floor(round((x + 250) / 500, 9))
            key = f'grid:r{row:04d}:c{col:04d}'
            # At a domain's outer lattice edge, its half-open neighbour can
            # have zero clipped area; use the touching positive-area cell.
            if key not in cells:
                key = next((k for k, cell in cells.items() if cell.covers(Point(x, y))), None)
            if key is None:
                continue
            if key not in origins:
                point = cells[key].representative_point()
                origins[key] = dict(id=key, xy=(point.x, point.y), facility_ids=set(), poi_ids=set(),
                                    max_poi_offset_m=0.0)
            origin = origins[key]
            origin['facility_ids'].add(fid)
            origin['poi_ids'].add(raw['poi_id'])
            origin['max_poi_offset_m'] = max(origin['max_poi_offset_m'],
                math.hypot(x-origin['xy'][0], y-origin['xy'][1]))
            unique.add((raw['poi_id'], location.lng, location.lat))
    # Serve rare categories first; dense training POIs cannot take every turn.
    counts = {fid: sum(fid in o['facility_ids'] for o in origins.values()) for fid in definitions}
    ordered = sorted(origins.values(), key=lambda o: (min(counts[f] for f in o['facility_ids']), o['id']))
    return ordered, len(unique), outside


def build_actual_poi_origins(domain, categories, definitions, to_m):
    """Share an origin only for the same facility identity and exact location.

    Outside facilities can serve the domain when their straight-line distance
    is at most 1 km; that window alone never establishes walkable coverage.
    """
    origins = {}
    outside = 0
    for category in categories:
        fid = category['facility_id']
        for raw in category['pois']:
            location = CenterPoint.model_validate(raw['location'])
            x, y = to_m(location.lng, location.lat)
            point = Point(x, y)
            if domain.distance(point) > SERVICE_DISTANCE_M + 1e-6:
                outside += 1
                continue
            identity = (raw['poi_id'], location.lng, location.lat, location.crs)
            key = 'poi:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest()
            if key not in origins:
                origins[key] = dict(id=key, xy=(x, y), location=location,
                    facility_ids=set(), poi_ids={raw['poi_id']}, max_poi_offset_m=0.0,
                    outside_domain=not domain.covers(point))
            origins[key]['facility_ids'].add(fid)
    counts = {fid: sum(fid in origin['facility_ids'] for origin in origins.values()) for fid in definitions}
    ordered = sorted(origins.values(), key=lambda origin:
        (origin['outside_domain'], min(counts[fid] for fid in origin['facility_ids']), origin['id']))
    return ordered, len(origins), outside


def analyze_representative_service_areas(domain, cells, to_m, to_center, categories, catalog,
                                        route, warnings, *, max_route_pairs=5000,
                                        progress=lambda _: None, sample_cache=None,
                                        origin_mode='shared_500m'):
    if origin_mode not in {'shared_500m', 'actual_poi'}:
        raise ValueError('Unknown service-area origin mode')
    actual_poi = origin_mode == 'actual_poi'
    method = ACTUAL_POI_METHOD if actual_poi else METHOD
    definitions = {f['id']: f for f in catalog['facilities']}
    supported = {c['facility_id']: (actual_poi and bool(c['pois'])) or definitions[c['facility_id']].get('coverage_method') == 'grid_walking_distance'
                 for c in categories}
    origins, source_count, outside_count = (build_actual_poi_origins if actual_poi else build_origins)(
        domain, categories, definitions, to_m)
    points = np.array([(cell.representative_point().x, cell.representative_point().y) for _, cell in cells])
    n = len(cells)
    covered = {c['facility_id']: np.zeros(n, dtype=bool) for c in categories}
    potential = {c['facility_id']: np.full(n, not supported[c['facility_id']] or c['search_status'] != 'complete', dtype=bool)
                 for c in categories}
    requested = unavailable = succeeded = no_route = skipped = batches = refinements = 0
    stopped = None
    reused_samples = 0
    service_features = []
    sample_features = []
    representative_features = []
    contour_features = []
    continuous_covered = {c['facility_id']: [] for c in categories}
    continuous_unknown = {c['facility_id']: [] for c in categories}
    nearby_attempted = 0
    inside_unverified = 0
    progress(.05)

    def region(mask):
        ids = np.flatnonzero(mask)
        return domain if len(ids) == n else unary_union([cells[int(i)][1] for i in ids]) if len(ids) else Polygon()

    def feature(geometry, props):
        return dict(type='Feature', geometry=_geojson(geometry, to_center), properties=props)

    contexts = []
    for origin in origins:
        x, y = origin['xy']
        fids = sorted(origin['facility_ids'])
        eligible = np.sum((points - (x, y)) ** 2, axis=1) <= SERVICE_DISTANCE_M ** 2
        state = np.full(n, -1, dtype=np.int8)  # -1 unknown, 0 outside service, 1 covered
        state[~eligible] = 0
        location = origin['location'] if actual_poi else to_center(x, y)
        measured, cached_points = {}, set()
        cached = (sample_cache or {}).get(origin['id'], {})
        prior_location = cached.get('location', [])
        if (len(prior_location) == 2
                and math.isclose(prior_location[0], location.lng, rel_tol=0, abs_tol=1e-8)
                and math.isclose(prior_location[1], location.lat, rel_tol=0, abs_tol=1e-8)):
            for point, distance in cached.get('samples', {}).items():
                if (len(point) == 2 and all(math.isfinite(v) for v in point)
                        and isinstance(distance, (int, float)) and not isinstance(distance, bool)
                        and math.isfinite(distance) and distance >= 0):
                    measured[point] = float(distance)
                    cached_points.add(point)
        for point in cached.get('failed_points', []):
            if len(point) == 2 and all(math.isfinite(v) for v in point):
                measured.setdefault(tuple(point), math.nan)
        representative_features.append(dict(type='Feature', geometry=dict(type='Point', coordinates=[location.lng, location.lat]),
            properties=dict(representative_id=origin['id'], facility_ids=fids, poi_count=len(origin['poi_ids']),
                            max_poi_offset_m=round(origin['max_poi_offset_m'], 2),
                            origin_grid_m=None if actual_poi else 500,
                            origin_mode=origin_mode, calculation_method=method,
                            poi_id=next(iter(origin['poi_ids'])) if actual_poi else None)))
        leaves = []
        candidate = domain.intersection(Point(x,y).buffer(
            SERVICE_DISTANCE_M/math.cos(math.pi/512),quad_segs=128)) if actual_poi else None
        if actual_poi and not candidate.is_empty:
            minx,miny,maxx,maxy=candidate.bounds
            leaves = [(col*COARSE_M, row*COARSE_M, COARSE_M)
                for row in range(math.floor(miny/COARSE_M), math.floor(maxy/COARSE_M)+1)
                for col in range(math.floor(minx/COARSE_M), math.floor(maxx/COARSE_M)+1)
                if candidate.intersection(box(col*COARSE_M,row*COARSE_M,
                    (col+1)*COARSE_M,(row+1)*COARSE_M)).area > 1e-8]
        elif np.any(eligible):
            ids = np.flatnonzero(eligible)
            minx, miny = points[ids].min(axis=0)
            maxx, maxy = points[ids].max(axis=0)
            leaves = [(col*COARSE_M, row*COARSE_M, COARSE_M)
                for row in range(math.floor(miny/COARSE_M), math.floor(maxy/COARSE_M)+1)
                for col in range(math.floor(minx/COARSE_M), math.floor(maxx/COARSE_M)+1)]
        contexts.append(SimpleNamespace(origin=origin, fids=fids, eligible=eligible, state=state,
            location=location, measured=measured, cached_points=cached_points, used_cached_points=set(),
            leaves=leaves, base_tiles=list(leaves), active=[], children=[],
            candidate=candidate,
            properties=representative_features[-1]['properties']))

    def resolve(context, *, finish=False):
        """Consume completed tiles and cache before spending another batch."""
        nonlocal refinements, skipped
        pending = []
        needed = context.eligible & np.logical_or.reduce([~covered[fid] for fid in context.fids])
        for a, b, size, original_mask, edge_required, coords in context.active:
            mask = original_mask & needed
            if not np.any(mask) and not edge_required:
                skipped += 1
                continue
            context.used_cached_points.update(set(coords) & context.cached_points)
            if not finish and not all(xy in context.measured for xy in coords):
                pending.append((a, b, size, mask, edge_required, coords))
                continue
            values = np.array([context.measured.get(xy, math.nan) for xy in coords])
            finite = np.all(np.isfinite(values))
            margin = np.min(np.abs(values-1000)) if finite else 0
            residual = abs(values[4]-values[:4].mean()) if finite else 0
            # Refine variation only when it could cross the service threshold.
            # Deep-interior measured tiles must not turn into holes merely
            # because their centre differs from the corner mean.
            near = finite and (margin <= size or values.min() <= 1000 <= values.max()
                               or residual > max(size/2, margin))
            partial = not finite and np.any(np.isfinite(values))
            if size > FINE_M and (near or partial) and (
                    (stopped is None and requested < max_route_pairs) or context.cached_points):
                half = size/2
                context.children.extend((a+dx, b+dy, half) for dx in (0, half) for dy in (0, half))
                refinements += 1
            elif finite:
                if values.max() <= 1000:
                    context.state[mask] = 1
                elif values.min() > 1000:
                    context.state[mask] = 0
                else:
                    # Same four-triangle measured interpolation as before;
                    # neither missing routes nor straight-line offsets prove coverage.
                    px, py = (points[mask, 0]-a)/size, (points[mask, 1]-b)/size
                    v00, v10, v11, v01, vc = values
                    triangles = [((0,0),(1,0),v00,v10), ((1,0),(1,1),v10,v11),
                                 ((1,1),(0,1),v11,v01), ((0,1),(0,0),v01,v00)]
                    distance = np.zeros(len(px))
                    for (ax,ay),(bx,by),va,vb in triangles:
                        det = (by-.5)*(ax-.5)+(0.5-bx)*(ay-.5)
                        wa = ((by-.5)*(px-.5)+(0.5-bx)*(py-.5))/det
                        wb = ((.5-ay)*(px-.5)+(ax-.5)*(py-.5))/det
                        wc = 1-wa-wb
                        inside = (wa >= -1e-9)&(wb >= -1e-9)&(wc >= -1e-9)
                        distance[inside] = (wa*va+wb*vb+wc*vc)[inside]
                    context.state[mask] = (distance <= 1000).astype(np.int8)
                for fid in context.fids:
                    covered[fid] |= context.state == 1
            # A failed or incomplete terminal tile stays unknown.
        context.active = pending

    def measure_nodes(context, chosen):
        nonlocal requested, batches, succeeded, no_route, unavailable, stopped
        targets = [SimpleNamespace(poi_id=f'adaptive:{a:g}:{b:g}', location=to_center(a,b)) for a,b in chosen]
        try:
            result = _measure(context.location, targets, route, warnings)
        except ProviderStopError as exc:
            stopped = exc.code
            return False
        batches += 1
        requested += len(chosen)
        for xy, target in zip(chosen, targets):
            value = result.get(target.poi_id)
            if value is not None and value.status == RouteStatus.SUCCESS and value.distance_m is not None:
                context.measured[xy] = float(value.distance_m)
                succeeded += 1
            else:
                context.measured[xy] = math.nan
                if value is not None and value.status == RouteStatus.NO_ROUTE:
                    no_route += 1
                else:
                    unavailable += 1
        return True

    # Give every inside real POI a small nearby turn before spending 25 pairs
    # on another origin or any outside facility. These are measured mesh nodes,
    # never a synthetic circle or a POI-centred display exclusion.
    if actual_poi:
        for context in contexts:
            if context.origin['outside_domain'] or stopped is not None or requested >= max_route_pairs:
                continue
            x,y=context.origin['xy']
            a,b=math.floor(x/COARSE_M)*COARSE_M,math.floor(y/COARSE_M)*COARSE_M
            # Seed a complete local tile. Five globally nearest nodes can lie
            # in four different tiles and prove no enclosing triangle at all.
            nodes={(a,b),(a+COARSE_M,b),(a+COARSE_M,b+COARSE_M),(a,b+COARSE_M),
                (a+COARSE_M/2,b+COARSE_M/2)}
            nearest = sorted(nodes, key=lambda xy: ((xy[0]-context.origin['xy'][0])**2
                + (xy[1]-context.origin['xy'][1])**2, xy))[:5]
            chosen = [xy for xy in nearest if xy not in context.measured][:max_route_pairs-requested]
            if chosen and measure_nodes(context, chosen):
                nearby_attempted += 1

    # Breadth first across all origins: finish the shared 200 m layer before
    # any 100/50 m refinement. Each origin receives at most 25 new pairs per turn.
    level = 0
    while any(context.leaves for context in contexts):
        for context in contexts:
            context.active, context.children = [], []
            needed = context.eligible & np.logical_or.reduce([~covered[fid] for fid in context.fids])
            for a, b, size in context.leaves:
                tile_mask = context.eligible & (points[:, 0] >= a) & (points[:, 0] < a+size) & (points[:, 1] >= b) & (points[:, 1] < b+size)
                mask = needed & tile_mask
                # A thin/disconnected piece of the actual domain may share an
                # auxiliary cell whose representative lies in another tile.
                # It still needs measured surrounding nodes for a continuous
                # contour; a missing 20m representative cannot erase that piece.
                edge_required = actual_poi and not np.any(tile_mask) and (
                    context.candidate.intersection(box(a,b,a+size,b+size)).area > 1e-8)
                if not np.any(mask) and not edge_required:
                    skipped += 1
                    continue
                coords = [(a,b), (a+size,b), (a+size,b+size), (a,b+size), (a+size/2,b+size/2)]
                context.active.append((a,b,size,mask,edge_required,coords))
            resolve(context)
        while stopped is None and requested < max_route_pairs:
            made_request = False
            for context in contexts:
                resolve(context)
                nodes = {xy for *_, coords in context.active for xy in coords} - context.measured.keys()
                nodes = sorted(nodes, key=lambda xy: ((xy[0]-context.origin['xy'][0])**2
                    + (xy[1]-context.origin['xy'][1])**2, xy)) if actual_poi else sorted(nodes)
                if not nodes or stopped is not None or requested >= max_route_pairs:
                    continue
                chosen = nodes[:min(25, max_route_pairs-requested)]
                if not measure_nodes(context, chosen):
                    break
                made_request = True
                resolve(context)
            if not made_request:
                break
        for context in contexts:
            resolve(context, finish=True)
            context.leaves = context.children
        level += 1
        progress(.05 + .75*min(level, 3)/3)

    for context in contexts:
        origin, fids = context.origin, context.fids
        reused_samples += len(context.used_cached_points)
        for fid in fids:
            potential[fid] |= context.eligible & (context.state == -1) & ~covered[fid]
        if actual_poi:
            known, evidence, candidate, contours = measured_distance_contour(
                domain, origin['xy'], context.measured, context.base_tiles, origin_zero_anchor=True)
            own_covered = not known.is_empty and known.distance(Point(*origin['xy'])) <= 1e-5
            context.properties.update(successful_distance_samples=sum(math.isfinite(v) for v in context.measured.values()),
                unavailable_distance_samples=sum(not math.isfinite(v) for v in context.measured.values()),
                inside_analysis_domain=not origin['outside_domain'], own_location_in_service_area=own_covered,
                self_distance_anchor=dict(source='identity_zero_distance',distance_m=0,
                    location=context.location.model_dump(mode='json')),
                service_area_status='measured_estimate' if not known.is_empty else 'unverified')
            inside_unverified += int(not origin['outside_domain'] and not own_covered)
            unresolved = candidate.difference(evidence).difference(known)
            for fid in fids:
                continuous_covered[fid].append(known)
                continuous_unknown[fid].append(unresolved)
            for crossings in contours:
                line = LineString(crossings).intersection(candidate)
                parts = list(line.geoms) if hasattr(line, 'geoms') else [line]
                for part in parts:
                    if part.geom_type != 'LineString' or part.length <= 1e-8:
                        continue
                    coords = [to_center(*xy) for xy in part.coords]
                    contour_features.append(dict(type='Feature', geometry=dict(type='LineString',
                        coordinates=[[point.lng, point.lat] for point in coords]), properties=dict(
                        representative_id=origin['id'], poi_id=next(iter(origin['poi_ids'])),
                        distance_m=1000, boundary_source='interpolated_between_measured_walking_distances',
                        calculation_method=method)))
        else:
            known = region(context.state == 1)
        if not known.is_empty:
            service_features.append(feature(known, dict(representative_id=origin['id'],
                poi_id=next(iter(origin['poi_ids'])) if actual_poi else origin['id'],
                facility_ids=fids, facility_type=fids[0], service_distance_m=1000,
                origin_mode=origin_mode, origin_location=context.location.model_dump(mode='json'),
                classification='covered', calculation_method=method)))
        for (a,b), distance in context.measured.items():
            p = to_center(a,b)
            sample_features.append(dict(type='Feature', geometry=dict(type='Point', coordinates=[p.lng,p.lat]),
                properties=dict(representative_id=origin['id'], origin_mode=origin_mode,
                    calculation_method=method, distance_m=distance if math.isfinite(distance) else None)))

    def collection(geometry, fid, classification):
        return dict(type='FeatureCollection', features=[] if geometry.is_empty else [feature(geometry,
            dict(facility_type=fid, classification=classification, calculation_method=method, estimated=True))])

    def record(fid, ids):
        if actual_poi:
            known = _clipped_coverage_union([part for f in ids for part in continuous_covered[f]], domain)
            incomplete = any(not supported[f] or next(c['search_status'] for c in categories
                if c['facility_id'] == f) != 'complete' for f in ids)
            pending = domain if incomplete or not ids else _clipped_coverage_union(
                [part for f in ids for part in continuous_unknown[f]], domain)
            unresolved = _clipped_coverage_union([pending.difference(known,
                grid_size=_OVERLAY_PRECISION_M)], domain)
            classified = _clipped_coverage_union([known, unresolved], domain)
            blind = _clipped_coverage_union([domain.difference(classified,
                grid_size=_OVERLAY_PRECISION_M)], domain)
            geometries = [known, blind, unresolved]
        else:
            known = np.logical_or.reduce([covered[f] for f in ids]) if ids else np.zeros(n,dtype=bool)
            unresolved = np.logical_or.reduce([potential[f] for f in ids]) & ~known if ids else np.ones(n,dtype=bool)
            blind = ~known & ~unresolved
            geometries = [region(mask) for mask in (known,blind,unresolved)]
        ratios = [min(1,max(0,g.area/domain.area)) for g in geometries]
        return dict(definition='all_categories_confirmed_blind', calculation_method=method, facility_ids=ids,
            covered_areas=collection(geometries[0],fid,'covered'), blind_spots=collection(geometries[1],fid,'blind'),
            unknown_areas=collection(geometries[2],fid,'unknown'), covered_ratio=ratios[0], blind_ratio=ratios[1],
            unknown_ratio=max(0,1-ratios[0]-ratios[1]) if not geometries[2].is_empty else 0,
            estimated=True)

    layers = {k:dict(type='FeatureCollection',features=[]) for k in ('covered_areas','blind_spots','unknown_areas')}
    ratios, groups = {}, {}
    for c in categories:
        fid = c['facility_id']
        row = record(fid,[fid])
        ratios[fid] = {k:row[k] for k in ('covered_ratio','blind_ratio','unknown_ratio')}
        for k in layers:
            layers[k]['features'].extend(row[k]['features'])
        groups.setdefault(definitions[fid]['group_id'],[]).append(fid)
    layers.update(poi_service_areas=dict(type='FeatureCollection',features=service_features),
                  representative_points=dict(type='FeatureCollection',features=representative_features),
                  route_samples=dict(type='FeatureCollection',features=sample_features),
                  group_coverage={gid:record(gid,ids) for gid,ids in groups.items()},
                  total_coverage=record('total',list(ratios)))
    if actual_poi:
        layers['service_distance_contours'] = dict(type='FeatureCollection', features=contour_features)
    unknown_ratio = max((ratios[f]['unknown_ratio'] for f in ratios if supported[f]),default=1)
    exhausted = requested >= max_route_pairs and unknown_ratio > 0
    if actual_poi:
        warnings.append(WarningItem(code='ACTUAL_POI_SERVICE_AREA_ESTIMATE', message=(
            '从真实设施坐标出发核验1000米实际步行服务区；200米粗采样、边界加密至50米，'
            '用已测距离插值定位1000米等距线，合成服务区后从生活圈扣除，不合并20米方格作为边界。'
            '距生活圈不超过1000米的圈外候选设施也可提供服务；未测范围保持未知。')))
    else:
        warnings.append(WarningItem(code='REPRESENTATIVE_SERVICE_AREA_ESTIMATE',message=(
            '圈内设施按500米格归并并共享代表点；统一1000米实际步行距离，200米粗采样、边界加密至50米，'
            '20米格仅表达采样估算。代表点不能保留格内全部真实出入口；未纳入圈外设施，结果不是逐点核验。')))
    if stopped or exhausted:
        warnings.append(WarningItem(code=stopped or 'ROUTING_BUDGET_EXCEEDED',message='路线已达预算或停止；未完成采样区域保留未知。'))
    if unknown_ratio > 1e-9:
        warnings.append(WarningItem(code='UNKNOWN_COVERAGE',message='仍有未测、不可用或检索不完整区域，不能当作盲区。'))
    progress(1)
    return ratios, layers, dict(grid_cell_count=n,grid_size_m=20,coverage_method=method,
        coverage_route_direction='poi_to_sample' if actual_poi else 'representative_to_sample',
        coverage_origin_mode=origin_mode,coverage_origin_grid_m=None if actual_poi else 500,coverage_service_distance_m=1000,
        coverage_sampling_coarse_m=200,coverage_sampling_fine_m=50,coverage_estimated=True,
        coverage_sampling_schedule='all_origins_coarse_then_round_robin_refinement',
        **(dict(coverage_origin_priority='inside_pois_nearby_5_then_inside_coarse_then_outside',
            coverage_overlay_precision_m=_OVERLAY_PRECISION_M,
            coverage_boundary_fragment_policy='sample_positive_domain_intersections_without_auxiliary_representatives',
            coverage_triangulation_scope='within_coarse_tiles_preserve_measured_local_evidence',
            coverage_origin_zero_anchor='analytic_identity_separate_from_provider_samples',
            coverage_access_scope='all_real_pois_uniform_1000m_not_capacity_assessment',
            coverage_inside_origins_nearby_attempted=nearby_attempted,
            coverage_inside_origins_unverified=inside_unverified) if actual_poi else {}),
        coverage_refinement_rule='threshold_margin_aware_v2',
        coverage_geometry_method='measured_distance_triangles_1000m_isoline_v1' if actual_poi else 'grid_cell_union',
        coverage_geometry_grid_m=None if actual_poi else 20,
        coverage_contour_segment_count=len(contour_features),
        coverage_distance_sample_count=sum(math.isfinite(value) for context in contexts for value in context.measured.values()),
        coverage_failed_sample_count=sum(not math.isfinite(value) for context in contexts for value in context.measured.values()),
        coverage_service_distance_policy='uniform_1000m_overrides_catalog_for_grid_walking_distance',
        coverage_precision=('actual_poi_origins_200_to_50m_sampling_continuous_1000m_contour' if actual_poi
            else '500m_shared_origins_200_to_50m_adaptive_field_20m_output'),
        coverage_poi_origin_count=len(origins),coverage_source_poi_count=source_count,
        coverage_outside_poi_memberships_excluded=outside_count,
        coverage_outside_poi_origins_included=sum(origin.get('outside_domain', False) for origin in origins),
        coverage_origin_rule=('exact_poi_id_location_crs' if actual_poi
            else 'clipped_cell_representative_point_center_aligned_half_open'),
        coverage_shared_category_uses=sum(len(o['facility_ids']) for o in origins),coverage_skipped_tiles_or_cells=skipped,
        coverage_refined_tiles=refinements,coverage_route_pairs=requested,coverage_reused_distance_samples=reused_samples,coverage_route_succeeded=succeeded,
        coverage_route_no_route=no_route,coverage_route_unavailable=unavailable,coverage_route_batches=batches,
        coverage_budget_exhausted=bool(stopped or exhausted),coverage_stop_reason=stopped,
        max_supported_category_unknown_ratio=unknown_ratio)
