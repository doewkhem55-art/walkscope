"""Fixed, evidence-first workflow: location -> isochrone -> catalog -> report."""

import math
import time
from datetime import datetime, timezone
from itertools import zip_longest
from types import SimpleNamespace

from shapely.geometry import Polygon
from shapely.ops import unary_union

from app.errors import ProviderStopError
from app.modules.blindspot.service import _geojson, _grid_cells, _measure, _polygon_parts
from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.poi import search_catalog
from app.modules.checkup import scoring
from app.modules.checkup.representative_service_areas import ACTUAL_POI_METHOD
from app.modules.checkup.snapshots import load_stages
from app.modules.isochrone.geometry import geometry_to_local
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.checkup import CategoryAssessment, CheckupPOI, CheckupReport, GroupAssessment
from app.schemas.common import CenterPoint, WarningItem
from app.schemas.isochrone import IsochroneRequest
from app.schemas.location import LocationRequest
from app.schemas.routing import RouteResult, RouteStatus, RoutingErrorCode, RoutingFailure


COVERAGE_GRID_SIZE_M = 20
COVERAGE_METHOD = ACTUAL_POI_METHOD
# Bound local CPU/memory independently of the unchanged paid-route budget.
_MAX_COVERAGE_GRID_CELLS = 100_000


class CheckupError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def _coverage_ratios(stat, area):
    """Return whole-cell coverage ratios that always sum to one.

    Grid cell areas can exceed the polygon area by a few float ulps, and an
    early stop leaves whole cells unclassified. Anything not proven covered or
    blind is therefore reported as unknown instead of being dropped, and no
    ratio can leave [0, 1].
    """
    covered = min(max(stat["covered"], 0.0), area)
    blind = min(max(stat["blind"], 0.0), area - covered)
    unknown = max(area - covered - blind, 0.0)
    return {
        "covered_ratio": covered / area,
        "blind_ratio": blind / area,
        "unknown_ratio": unknown / area,
    }


def _route_stop_code(result):
    """Stop reason carried by a routing envelope, if the provider refused."""
    root = result.root
    if isinstance(root, RoutingFailure):
        code = getattr(root.error.code, "value", root.error.code)
        if code == RoutingErrorCode.RATE_LIMITED.value and not root.error.retryable:
            return "ROUTING_DAILY_QUOTA_EXHAUSTED"
        return None
    for warning in root.warnings:
        if warning.code == "ROUTING_DAILY_QUOTA_EXHAUSTED":
            return "ROUTING_DAILY_QUOTA_EXHAUSTED"
    return None


def _guarded_route(route):
    """Turn a provider quota stop into a control-flow stop for this run."""

    def guarded(request):
        result = route(request)
        code = _route_stop_code(result)
        if code is not None:
            raise ProviderStopError(code, "百度步行批量算路当日配额不足，已停止本次体检的后续路线请求。")
        return result

    return guarded


def _quantize(value):
    return min(100.0, max(0.0, math.floor(value / 2.5 + 0.5) * 2.5))


def _fair_candidates(groups, limit):
    """Round-robin categories avoids spending the route budget on supermarkets."""
    chosen = {}
    if limit <= 0:
        return []
    for row in zip_longest(*groups):
        for poi in row:
            if poi is not None and poi.poi_id not in chosen and len(chosen) < limit:
                chosen[poi.poi_id] = poi
                if len(chosen) == limit:
                    return list(chosen.values())
    return list(chosen.values())


def _projection(center):
    r = 6_371_008.8
    cosine = math.cos(math.radians(center.lat))
    if abs(cosine) < 0.01:
        raise CheckupError("INVALID_LOCATION", "暂不支持极区体检。")

    def to_m(lng, lat):
        return (math.radians(lng - center.lng) * r * cosine, math.radians(lat - center.lat) * r)

    def to_center(x, y):
        return CenterPoint(lng=center.lng + math.degrees(x / (r * cosine)),
                           lat=center.lat + math.degrees(y / r), crs="BD09LL")
    return to_m, to_center


def _access_status(*, reachable: int, unresolved: int, count, observed: list) -> str:
    """Access verdict for one category, independent from the presence score.

    ``count``/``known_count`` keep their fact semantics (an incomplete search
    still reports ``count=None``). Route evidence now only describes whether a
    facility was confirmed reachable; it never changes the presence-count score.
    """
    if reachable:
        return "reachable"
    if count is None or unresolved:
        return "unverified"
    return "not_found" if not observed else "unreachable"


def aggregate_saved_coverage(center, geometry, categories, blindspot):
    """Intersect confirmed no-service evidence for every selected category.

    A category with missing or inconsistent ratios/geometries is wholly unknown.
    A known covered category still rules out aggregate no-service at that place.
    This only derives additive fields; saved per-category facts remain untouched.
    """
    center = CenterPoint.model_validate(center)
    to_m, to_center = _projection(center)
    if isinstance(geometry, dict):
        geometry = SimpleNamespace(**geometry)
    domain = geometry_to_local(geometry, to_m)
    if not domain.is_valid or domain.is_empty or domain.area <= 0:
        raise CheckupError("INVALID_GEOMETRY", "等时圈范围无效，不能生成覆盖结论。")
    empty = Polygon()
    tolerance = domain.area * 1e-6
    categories = [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in categories]
    regions = {}
    for category in categories:
        fid = category["facility_id"]
        try:
            ratios = [category.get(key) for key in ("covered_ratio", "blind_ratio", "unknown_ratio")]
            if not all(isinstance(r, (float, int)) and not isinstance(r, bool)
                       and math.isfinite(r) and 0 <= r <= 1 for r in ratios) or abs(sum(ratios) - 1) > 1e-6:
                raise ValueError("missing or contradictory coverage ratios")
            evidence = []
            for key in ("blind_spots", "unknown_areas"):
                collection = blindspot.get(key)
                if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection" \
                        or not isinstance(collection.get("features"), list):
                    raise ValueError("missing coverage collection")
                parts = []
                for feature in collection["features"]:
                    if feature.get("properties", {}).get("facility_type") != fid:
                        continue
                    raw = feature["geometry"]
                    if raw.get("type") not in {"Polygon", "MultiPolygon"}:
                        raise ValueError("non-polygon coverage evidence")
                    region = geometry_to_local(SimpleNamespace(**raw), to_m)
                    if not region.is_valid:
                        raise ValueError("invalid coverage evidence")
                    parts.extend(_polygon_parts(region.intersection(domain)))
                evidence.append(unary_union(parts) if parts else empty)
            blind, unknown = evidence
            if (any((ratio == 0) != region.is_empty for ratio, region in zip(ratios[1:], evidence))
                    or abs(blind.area - ratios[1] * domain.area) > tolerance
                    or abs(unknown.area - ratios[2] * domain.area) > tolerance
                    or blind.intersection(unknown).area > tolerance):
                raise ValueError("coverage ratios disagree with geometry")
            # A tiny roundoff overlap belongs to unknown, never confirmed blind.
            regions[fid] = (blind.difference(unknown), blind.union(unknown))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            regions[fid] = (empty, domain)

    def record(group_id, facility_ids):
        blind = possible = domain
        for fid in facility_ids:
            confirmed, unresolved = regions[fid]
            blind = blind.intersection(confirmed)
            possible = possible.intersection(unresolved)
        if not facility_ids:
            blind, possible = empty, domain
        blind = unary_union(_polygon_parts(blind))
        unknown = unary_union(_polygon_parts(possible.difference(blind)))
        def collection(region, classification):
            return {"type": "FeatureCollection", "features": [] if region.is_empty else [{
                "type": "Feature", "geometry": _geojson(region, to_center),
                "properties": {"facility_type": group_id, "classification": classification},
            }]}
        blind_ratio = min(1.0, max(0.0, blind.area / domain.area))
        unknown_ratio = min(1.0 - blind_ratio, max(0.0, unknown.area / domain.area))
        return {"definition": "all_categories_confirmed_blind", "facility_ids": facility_ids,
                "blind_spots": collection(blind, "blind"), "unknown_areas": collection(unknown, "unknown"),
                "covered_ratio": 1.0 - blind_ratio - unknown_ratio,
                "blind_ratio": blind_ratio, "unknown_ratio": unknown_ratio}

    groups = {}
    for category in categories:
        fid = category["facility_id"]
        group_id = category["group_id"]
        groups.setdefault(group_id, []).append(fid)
    return {"group_coverage": {gid: record(gid, ids) for gid, ids in groups.items()},
            "total_coverage": record("total", [c["facility_id"] for c in categories])}


def analyze_coverage(center, geometry, categories, catalog, route, warnings,
                     *, max_route_pairs=5000, progress=lambda value: None, sample_cache=None,
                     origin_mode='actual_poi'):
    """Union POI-origin walking service areas, then subtract from the domain.

    True POI origins use continuous 1000 m contours of the measured-distance field.
    Unmeasured candidates and unsupported categories remain unknown.
    """
    to_m, to_center = _projection(center)
    domain = geometry_to_local(geometry, to_m)
    if not domain.is_valid or domain.area <= 0 or max(map(abs, domain.bounds)) > 20_000:
        raise CheckupError("INVALID_GEOMETRY", "等时圈范围无效，不能生成覆盖结论。")
    minx, miny, maxx, maxy = domain.bounds
    half = COVERAGE_GRID_SIZE_M / 2
    rows = math.ceil((maxy + half) / COVERAGE_GRID_SIZE_M) - math.floor((miny + half) / COVERAGE_GRID_SIZE_M)
    cols = math.ceil((maxx + half) / COVERAGE_GRID_SIZE_M) - math.floor((minx + half) / COVERAGE_GRID_SIZE_M)
    if rows * cols > _MAX_COVERAGE_GRID_CELLS:
        raise CheckupError("INVALID_GEOMETRY", f"等时圈范围超出{COVERAGE_GRID_SIZE_M}米网格可处理规模。")
    cells = _grid_cells(domain, COVERAGE_GRID_SIZE_M)
    if not cells:
        raise CheckupError("INVALID_GEOMETRY", "等时圈范围无法生成有效网格。")
    from app.modules.checkup.representative_service_areas import analyze_representative_service_areas

    ratios, layers, quality = analyze_representative_service_areas(
        domain, cells, to_m, to_center, categories, catalog, _guarded_route(route), warnings,
        max_route_pairs=max_route_pairs, progress=progress, sample_cache=sample_cache, origin_mode=origin_mode,
    )
    from app.modules.checkup.coverage_display import apply_display_policy
    quality["coverage_display_policy"] = "include_unknown"
    return ratios, apply_display_policy(layers, center=center, geometry=geometry), quality


def run_checkup(request, *, report_id, resolve_location, generate_isochrone, poi_provider, route,
                progress=lambda stage, value: None, max_center_routes=1500, max_grid_routes=5000,
                stage_manifest=None, on_update=None):
    catalog = load_catalog()
    all_facilities = catalog["facilities"]
    if request.facility_ids is None:
        selected = all_facilities
    else:
        by_id = {item["id"]: item for item in all_facilities}
        unknown = [item for item in request.facility_ids if item not in by_id]
        if unknown:
            raise CheckupError(
                "INVALID_FACILITY_SELECTION",
                "以下设施类别不在目录中，已停止体检：" + "、".join(unknown),
            )
        selected = [by_id[item] for item in request.facility_ids]
    if not selected:
        raise CheckupError("INVALID_FACILITY_SELECTION", "至少选择一个设施类别。")
    selected_ids = [item["id"] for item in selected]
    selected_set = set(selected_ids)
    partial_scope = selected_set != {item["id"] for item in all_facilities}
    selected_group_ids = {item["group_id"] for item in selected}
    catalog = {**catalog, "facilities": selected}
    # The HTTP adapter attaches a publisher while preserving the existing
    # two-argument progress callback used by CLI runners and integrations.
    publish = on_update or getattr(progress, 'publish', lambda snapshot: None)
    snapshot = dict(report_id=report_id, center=None, location_label='', isochrone=None,
                    categories=[], groups=[], pois=[], routes=[], completed_categories=0,
                    total_categories=len(selected), active_facility_id=None)
    searched_rows = []
    started_facilities = set()
    search_finished = False

    def publish_categories(active=None):
        # Quota stops append untouched catalog rows for the final report. Keep
        # those pending here rather than claiming they were actually analyzed.
        by_id = {row['facility_id']: row for row in searched_rows
                 if row['facility_id'] in started_facilities}
        category_rows, group_rows = [], []
        for definition in selected:
            row = by_id.get(definition['id'])
            value = scoring.score_category(row['facility_id'], row['search_status'], row['count'], row['known_count']) if row else None
            category_rows.append(dict(facility_id=definition['id'], group_id=definition['group_id'],
                name_zh=definition['name_zh'], search_status=row['search_status'] if row else 'pending',
                known_count=row['known_count'] if row else 0, score=value.score if value else None,
                score_provisional=value.status == 'provisional' if value else False))
        for group in catalog['groups']:
            members = [c for c in category_rows if c['group_id'] == group['id']]
            if not members:
                continue
            finished = [c for c in members if c['search_status'] != 'pending']
            values = [c['score'] for c in finished if c['score'] is not None]
            complete = len(finished) == len(members) and all(c['search_status'] == 'complete' for c in finished)
            active_here = any(d['id'] == active and d['group_id'] == group['id'] for d in selected)
            state = 'complete' if complete else ('partial' if finished and (search_finished or len(finished) == len(members)) else
                    'searching' if active_here or finished else 'pending')
            ids = {p['poi_id'] for row in by_id.values() if row['group_id'] == group['id'] for p in row['pois'] if p['in_isochrone']}
            group_rows.append(dict(**group, status=state, score=_quantize(sum(values)/len(values)) if values else None,
                score_provisional=not complete, completed_categories=len(finished), total_categories=len(members),
                complete_categories=sum(c['search_status'] == 'complete' for c in members),
                facility_count=len(ids)))
        snapshot.update(categories=category_rows, groups=group_rows,
            pois=[p for row in by_id.values() for p in row['pois']], completed_categories=len(by_id),
            complete_categories=sum(c['search_status'] == 'complete' for c in category_rows), active_facility_id=active)
        # Publish a new outer object; earlier callbacks retain their historical
        # stage even when later stages add the center, geometry or routes.
        publish(dict(snapshot))

    def category_started(facility_id):
        started_facilities.add(facility_id)
        publish_categories(facility_id)

    def category_done(row):
        searched_rows.append(row)
        publish_categories()

    publish_categories()
    route = _guarded_route(route)
    warnings = []
    timings: dict[str, float] = {}
    stage_started = time.perf_counter()
    stages = None
    if stage_manifest is not None and str(stage_manifest).strip():
        try:
            stages = load_stages(stage_manifest, request.location, catalog)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
            raise CheckupError("STAGE_SNAPSHOT_INVALID", "登记的阶段数据身份、文件或内容校验失败；已停止体检，请核实清单。") from None
    progress("location", 2)
    location = stages[0] if stages else resolve_location(LocationRequest(schema_version="1.0", input=request.location)).root
    if not location.ok:
        raise CheckupError("LOCATION_FAILED", "无法确定体检地点，请补充城市或换用地图选点。", location.error.retryable)
    warnings.extend(location.warnings)
    center = location.data.center
    snapshot.update(center=center.model_dump(mode='json'), location_label=location.data.label or '地图选点')
    publish_categories()
    timings["location"] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    progress("isochrone", 8)
    try:
        iso = stages[1] if stages else generate_isochrone(IsochroneRequest(schema_version="1.0", center=center, time_limit_s=900)).root
    except ProviderStopError as exc:
        raise CheckupError(exc.code, f"{exc.message}已停止体检；不会替换为圆形范围。") from None
    if not iso.ok:
        warning_codes = ", ".join(sorted({warning.code for warning in iso.warnings}))
        raise CheckupError(
            "ISOCHRONE_FAILED",
            f"{iso.error.message}（{iso.error.code.value}"
            f"{'; ' + warning_codes if warning_codes else ''}）"
            "已停止体检；不会替换为圆形范围。",
            iso.error.retryable,
        )
    if iso.data.center != center or iso.data.time_limit_s != 900:
        raise CheckupError("ISOCHRONE_FAILED", "等时圈结果与体检请求不一致，已停止体检；不会替换为圆形范围。")
    warnings.extend(iso.warnings)
    snapshot['isochrone'] = iso.data.model_dump(mode='json')
    publish_categories()
    timings["isochrone"] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    buffer = max([1000] + [f.get("service_distance_m") or 0 for f in selected])
    radius = minimum_poi_search_radius_m(center, iso.data.geometry, buffer)
    progress("poi", 25)
    if stages:
        searches = [item for item in stages[2] if item["facility_id"] in selected_set]
        started_facilities.update(item['facility_id'] for item in searches)
        searched_rows.extend(searches)
        publish_categories()
    else:
        searches = search_catalog(
            poi_provider, center, iso.data.geometry.model_dump(), radius,
            progress=lambda n: progress("poi", 25 + int(n / max(1, len(selected)) * 30)),
            facilities=selected,
            on_start=category_started, on_category=category_done,
        )
    search_finished = True
    publish_categories()
    timings["poi"] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    for category in searches:
        warnings.extend(WarningItem.model_validate(w) for w in category["warnings"])
    pois = [CheckupPOI.model_validate(p) for c in searches for p in c["pois"]]
    positions = {}
    for poi in pois:
        if poi.poi_id in positions and positions[poi.poi_id] != poi.location:
            raise CheckupError("POI_CONFLICT", "相同设施编号出现不一致位置，已停止生成结论。")
        positions[poi.poi_id] = poi.location
    to_m, _ = _projection(center)
    targets = _fair_candidates([
        sorted([p for p in pois if p.facility_id == c["facility_id"] and p.in_isochrone],
               key=lambda p: math.hypot(*to_m(p.location.lng, p.location.lat))) for c in searches
    ], max_center_routes)
    progress("routing", 57)
    center_stop = None
    try:
        measured = _measure(center, targets, route, warnings)
    except ProviderStopError as exc:
        # Keep the report honest instead of inventing routes: every in-domain
        # facility stays unknown and no category is scored.
        center_stop = exc.code
        warnings.append(WarningItem(code=exc.code, message="中心到设施的步行路线请求被停止（本地请求预算或供应商配额）；圈内设施的可用性保持未知。"))
        measured = {}
    inside_unique = {p.poi_id: p for p in pois if p.in_isochrone}
    routes = [measured.get(pid) or RouteResult(target_id=pid, location=p.location, status="unavailable")
              for pid, p in inside_unique.items()]
    snapshot['routes'] = [r.model_dump(mode='json') for r in routes]
    publish_categories()
    route_by_id = {r.target_id: r for r in routes}
    timings["routing"] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    progress("coverage", 65)
    ratios, blindspot, quality = analyze_coverage(center, iso.data.geometry, searches, catalog, route, warnings,
        max_route_pairs=max_grid_routes, progress=lambda fraction: progress('coverage', 65 + int(fraction * 28)))
    timings["coverage"] = round(time.perf_counter() - stage_started, 3)
    stage_started = time.perf_counter()
    if stages:
        quality["stage_reuse"] = stages[3]
        warnings.append(WarningItem(code="STAGED_DATA_REUSED", message=(
            "复用 baidu-live-staged 定位、等时圈和设施阶段；文件 SHA256：Gate1 "
            + stages[3]["gate1"]["sha256"] + "，Gate2 " + stages[3]["gate2"]["sha256"]
            + "；每类完整性与采集时间见质量字段，后续步行路线和网格在本次计算。")))
        if any(item["captured_at"] is None for item in stages[3]["categories"]) or any(
            stages[3][stage]["captured_at"] is None for stage in ("gate1", "gate2")
        ):
            warnings.append(WarningItem(code="STAGED_COLLECTION_TIME_UNKNOWN",
                message="部分阶段或类别快照未记录采集时间，保留为未知；报告生成时间不代表采集时间。"))
    progress("report", 94)
    definitions = {f["id"]: f for f in catalog["facilities"]}
    assessments = []
    for category in searches:
        fid = category["facility_id"]
        observed = [route_by_id[p["poi_id"]] for p in category["pois"] if p["in_isochrone"]]
        success = [r for r in observed if r.status == RouteStatus.SUCCESS]
        nearest = min(success, key=lambda r: r.duration_s, default=None)
        reachable = sum(r.duration_s <= 900 for r in success)
        unresolved = sum(r.status == RouteStatus.UNAVAILABLE for r in observed)
        status = _access_status(
            reachable=reachable, unresolved=unresolved, count=category["count"], observed=observed)
        category_score = scoring.score_category(
            fid, category["search_status"], category["count"], category["known_count"])
        assessments.append(CategoryAssessment(
            **{key: category[key] for key in ("facility_id", "name_zh", "group_id", "search_status", "count", "known_count")},
            reachable_count=reachable, unresolved_count=unresolved,
            nearest_duration_s=nearest.duration_s if nearest else None,
            nearest_distance_m=nearest.distance_m if nearest else None,
            access_status=status,
            score=category_score.score,
            score_provisional=category_score.status == "provisional",
            score_profile=category_score.profile,
            score_basis_count=category_score.basis_count,
            score_band=category_score.band,
            next_band=category_score.next_band,
            **ratios[fid], data_notes=definitions[fid].get("data_notes", ""),
        ))
    unverified = [item.facility_id for item in assessments if item.access_status == "unverified"]
    if unverified:
        warnings.append(WarningItem(code="ROUTE_EVIDENCE_INCOMPLETE", message=(
            "以下类别的检索或路线证据不完整（不影响设施配置分，仅表示可达性未核实）："
            + "、".join(unverified) + "。")))
    provisional = [item.facility_id for item in assessments if item.score_provisional]
    if provisional:
        warnings.append(WarningItem(code="PROVISIONAL_PRESENCE_SCORE", message=(
            "以下类别检索未完成，配置分按已知点位数量暂估（不代表最终数量）："
            + "、".join(provisional) + "。")))
    unavailable = [item.facility_id for item in assessments if item.score is None]
    if unavailable:
        warnings.append(WarningItem(code="CATEGORY_SCORE_UNAVAILABLE", message=(
            "以下类别检索失败且没有已知点位，暂不生成配置分（不代表确认没有设施）："
            + "、".join(unavailable) + "。")))
    unsupported = [item.facility_id for item in assessments
                   if item.score == 0 and item.access_status == "not_found"
                   and definitions[item.facility_id].get("coverage_method") == "insufficient_evidence"]
    if unsupported:
        warnings.append(WarningItem(code="NOT_FOUND_SCORED_ZERO", message=(
            "以下类别的目录口径需要场地几何或服务边界证据，本次完整检索未发现设施，按 0 分计入；"
            "“未检索到”不代表确认不存在：" + "、".join(unsupported) + "。")))
    groups = []
    raw_group_scores = []
    for group in catalog["groups"]:
        if group["id"] not in selected_group_ids:
            continue
        members = [c for c in assessments if c.group_id == group["id"]]
        values = [c.score for c in members if c.score is not None]
        raw = sum(values) / len(values) if values else None
        raw_group_scores.append(raw)
        groups.append(GroupAssessment(**group, score=_quantize(raw) if raw is not None else None,
            score_provisional=len(values) < len(members) or any(c.score_provisional for c in members),
            assessed_categories=len(values), total_categories=len(members),
            summary=f"{sum(c.reachable_count > 0 for c in members)}/{len(members)} 类已确认中心出发 15 分钟可达；{len(values)}/{len(members)} 类可生成设施配置分。"))
    score = (_quantize(sum(raw_group_scores) / len(raw_group_scores))
             if raw_group_scores and all(s is not None for s in raw_group_scores)
             and all(c.score is not None for c in assessments) else None)
    known = sum(c.score is not None for c in assessments)
    accessible = sum(c.reachable_count > 0 for c in assessments)
    weak = sorted((g for g in groups if g.score is not None), key=lambda g: g.score)[:2]
    status_counts = {status: sum(c.search_status == status for c in assessments)
                     for status in ("complete", "partial", "failed")}
    total_selected = len(assessments)
    scope_text = (f"本次选择 {total_selected} 类设施"
                  if partial_scope else f"目录共 {total_selected} 类设施")
    summary = (f"{scope_text}：{status_counts['complete']} 类完整、"
               f"{status_counts['partial']} 类部分完成、{status_counts['failed']} 类失败或缺失；"
               f"{known}/{total_selected} 类可生成设施配置分；其中 {accessible} 类在本次近似圈内且已确认中心出发 15 分钟步行可达。")
    if provisional:
        summary += f" {len(provisional)} 类检索未完成，配置分按已知点位数量暂估。"
    if unavailable:
        summary += f" {len(unavailable)} 类检索失败且无已知点位，暂不生成配置分。"
    if weak:
        summary += " 可优先核查" + "、".join(g.name_zh for g in weak) + "的设施可达性。"
    if score is None:
        summary += " 仍有类别缺少可计分数量，暂不生成完整综合分。"
    if partial_scope:
        warnings.append(WarningItem(
            code="SELECTED_SCOPE",
            message=f"本次只分析所选 {total_selected} 类设施；未选类别不计 0 分、不进入分母，分数不能与全 45 类总分直接比较。",
        ))
    warnings.append(WarningItem(code="PROJECT_PROXY", message="设施配置参考分衡量所选类别的设施点位数量，不代表官方达标、人口覆盖、服务质量或容量；面积和服务边界缺失的类别按其目录口径计分并注明。"))
    warnings.append(WarningItem(code="APPROXIMATE_BOUNDARIES", message="等时圈由步行测时推导；设施出发使用真实POI坐标、1000米实际步行阈值与200至50米自适应采样，插值得到1000米等距边界，服务区合并后从生活圈扣除。20米格仅辅助采样调度，最终边界不由格子拼接。边界为测距插值估算，均不是真实街区或建筑边界。"))
    timings["report"] = round(time.perf_counter() - stage_started, 3)
    timings["total"] = round(sum(value for key, value in timings.items() if key != "total"), 3)
    quality.update(assessed_categories=known, total_categories=total_selected,
                   selected_categories=total_selected, center_route_pairs=len(targets),
                   stage_timings_s=timings,
                   stage_counts={"selected_categories": total_selected,
                                 "poi_categories_searched": len(searches),
                                 "center_route_targets": len(targets),
                                 "coverage_route_pairs": quality.get("coverage_route_pairs")},
                   center_route_budget=max_center_routes, coverage_route_budget=max_grid_routes,
                   center_stop_reason=center_stop,
                   unverified_categories=unverified,
                   provisional_categories=provisional,
                   unavailable_categories=unavailable,
                   not_found_scored_zero=unsupported,
                   score_method_version=scoring.SCORING_VERSION,
                   confidence="low" if unavailable or quality["coverage_budget_exhausted"]
                   or quality["coverage_stop_reason"] is not None or center_stop is not None
                   or quality["max_supported_category_unknown_ratio"] > 0.1
                   or iso.data.quality.confidence.value == "low" else "medium")
    return CheckupReport(
        report_id=report_id, created_at=datetime.now(timezone.utc).isoformat(), center=center,
        location_label=location.data.label or "地图选点", catalog_version=catalog["version"],
        isochrone=iso.data, search_radius_m=radius, categories=assessments, groups=groups,
        selected_facility_ids=selected_ids,
        score=score, score_provisional=any(c.score_provisional for c in assessments),
        score_method=scoring.SCORE_METHOD_TEXT,
        summary=summary, pois=pois, routes=routes, blindspot=blindspot,
        warnings=list({(w.code, w.message): w for w in warnings}.values()), quality=quality,
    )
