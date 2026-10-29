"""Search every catalog facility and mark POIs inside the isochrone."""

from collections.abc import Callable, Mapping
from typing import Any

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from app.errors import ProviderStopError
from app.modules.checkup.catalog import load_catalog
from app.modules.poi.service import _dedupe_key, _public_id
from app.providers.baidu.poi import POIProviderError
from app.schemas.common import CenterPoint


def _polygon_geometry(geometry: Any) -> BaseGeometry:
    if not isinstance(geometry, BaseGeometry):
        if hasattr(geometry, "model_dump"):
            geometry = geometry.model_dump()
        if not isinstance(geometry, Mapping):
            raise TypeError("geometry must be a Shapely geometry or GeoJSON object")
        geometry = shape(geometry)
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty or not geometry.is_valid:
        raise ValueError("geometry must be a valid polygon")
    return geometry


def _add_items(unique: dict, items: list[Any], facility: dict, polygon: BaseGeometry) -> None:
    for item in items:
        key = _dedupe_key(item)
        unique.setdefault(key, {
            "poi_id": _public_id(item),
            "name": item.name,
            "location": {"lng": item.lng, "lat": item.lat, "crs": "BD09LL"},
            "address": item.address or None,
            "facility_id": facility["id"],
            "in_isochrone": polygon.covers(Point(item.lng, item.lat)),
        })


def search_catalog(
    provider: Any,
    center: CenterPoint,
    geometry: Any,
    search_radius_m: int,
    progress: Callable[[int], None] | None = None,
    facilities: list[dict[str, Any]] | None = None,
    on_category: Callable[[dict], None] | None = None,
    on_start: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Search the selected catalog facilities and keep known results on partial failure.

    Geometry and provider coordinates are interpreted as BD09LL [lng, lat].
    ``facilities`` defaults to the full catalog; pass a filtered list for a
    pre-submit category selection. The callback receives the 1-based number of
    completed facilities.
    """
    if isinstance(search_radius_m, bool) or not isinstance(search_radius_m, int) or search_radius_m <= 0:
        raise ValueError("search_radius_m must be a positive integer")
    polygon = _polygon_geometry(geometry)
    results = []
    quota_exhausted = False
    selected = facilities if facilities is not None else load_catalog()["facilities"]
    for index, facility in enumerate(selected, start=1):
        if on_start is not None:
            on_start(facility['id'])
        unique = {}
        succeeded = failed = incomplete = 0
        stopped = None
        keywords = facility.get("search_terms", [])
        for keyword in keywords:
            if quota_exhausted:
                failed += 1
                continue
            try:
                items = provider.search(keyword, center, search_radius_m)
                if not isinstance(items, list):
                    raise TypeError("provider result must be a list")
                _add_items(unique, items, facility, polygon)
            except POIProviderError as error:
                failed += 1
                quota_exhausted |= error.code == "DAILY_QUOTA_EXCEEDED"
                if isinstance(error.partial_results, list):
                    if error.code == "INCOMPLETE_RESULTS" or error.partial_results:
                        incomplete += 1
                    _add_items(unique, error.partial_results, facility, polygon)
            except ProviderStopError as error:
                # Local budget / provider quota stop: end the whole search
                # after publishing the current category's already-known POIs.
                failed += 1
                stopped = error
                break
            except Exception:
                failed += 1
            else:
                succeeded += 1

        if not keywords:
            failed = 1
        pois = list(unique.values())
        known_count = sum(item["in_isochrone"] for item in pois)
        if failed:
            has_partial_data = succeeded or incomplete or unique
            search_status = "partial" if has_partial_data else "failed"
            count = None
            warnings = [{
                "code": (
                    "POI_DAILY_QUOTA_EXHAUSTED" if quota_exhausted
                    else "PARTIAL_POI_RESULTS" if has_partial_data
                    else "POI_SEARCH_FAILED"
                ),
                "message": (
                    "地点检索当日额度不足，该类别未完成检索；已得到的点位仍保留。"
                    if quota_exhausted
                    else f"{facility['name_zh']}设施检索结果不完整。"
                ),
            }]
        else:
            search_status = "complete"
            count = known_count
            warnings = []
        if stopped is not None:
            warnings = [{"code": stopped.code,
                         "message": "设施检索因请求预算或供应商配额停止；本类别已得到的点位仍保留，数量尚未完整核实。"}]
        results.append({
            "facility_id": facility["id"],
            "name_zh": facility["name_zh"],
            "group_id": facility["group_id"],
            "search_status": search_status,
            "count": count,
            "known_count": known_count,
            "pois": pois,
            "warnings": warnings,
        })
        if on_category is not None:
            on_category(results[-1])
        if progress is not None:
            progress(index)
        if stopped is not None:
            raise stopped
        if quota_exhausted:
            remaining = selected[index:]
            for rest in remaining:
                results.append({
                    "facility_id": rest["id"],
                    "name_zh": rest["name_zh"],
                    "group_id": rest["group_id"],
                    "search_status": "failed",
                    "count": None,
                    "known_count": 0,
                    "pois": [],
                    "warnings": [{
                        "code": "POI_DAILY_QUOTA_EXHAUSTED",
                        "message": "地点检索当日额度不足，该类别未开始检索。",
                    }],
                })
                if on_category is not None:
                    on_category(results[-1])
            if progress is not None:
                progress(len(selected))
            break
    return results
