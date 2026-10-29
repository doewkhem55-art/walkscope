"""Bounded 45-category POI search using a saved, real Gate 1 result.

Run explicitly with RUN_BAIDU_GATE2=1 and a server-side BAIDU_MAP_AK.
Only normalized public POIs are saved under ignored data/cache/.
"""

import argparse
import json
import math
import os
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from shapely.geometry import Point, shape


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.poi import search_catalog
from app.modules.isochrone.sampling import EARTH_RADIUS_M, destination_point
from app.modules.poi.service import _public_id
from app.providers.baidu.poi import BaiduPOIProvider, POIProviderError, ProviderPOI
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.isochrone import IsochroneResult
from app.schemas.location import LocationResult


PARTITION_VERSION = "circle-4-depth2-v1"


def partition_circles(center, radius_m):
    """Cover the parent circle using four overlapping spherical circles."""
    angular = radius_m / EARTH_RADIUS_M
    cosine = (math.cos(angular) * math.cos(angular / 2)
              + math.sin(angular) * math.sin(angular / 2) / math.sqrt(2))
    child_radius = math.ceil(max(radius_m / 2,
        EARTH_RADIUS_M * math.acos(max(-1, min(1, cosine))))) + 2
    return [(destination_point(center, bearing, radius_m / 2), child_radius)
            for bearing in (45, 135, 225, 315)]


def circle_distance_m(center, item):
    dlat, dlng = math.radians(item.lat - center.lat), math.radians(item.lng - center.lng)
    hav = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(center.lat))
           * math.cos(math.radians(item.lat)) * math.sin(dlng / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1, hav)))


def run_gate2(gate1_file, *, ak, max_http=20, start=1, end=45,
              resume_file=None, output_dir=None, http_get=urlopen, pace_seconds=1.5,
              partition_incomplete=False):
    if not 1 <= max_http <= 728 or not 1 <= start <= end <= 45:
        raise ValueError("invalid Gate 2 request range or HTTP budget")
    source = json.loads(Path(gate1_file).read_text(encoding="utf-8"))
    location = LocationResult.model_validate(source["location"]).root
    isochrone = IsochroneResult.model_validate(source["isochrone"]).root
    if not location.ok or not isochrone.ok or location.data.center != isochrone.data.center \
            or isochrone.data.time_limit_s != 900:
        raise ValueError("Gate 1 result is not a matching successful 15-minute run")
    center, geometry = location.data.center, isochrone.data.geometry
    polygon = shape(geometry.model_dump(mode="json"))
    if not polygon.is_valid or polygon.is_empty or not polygon.covers(Point(center.lng, center.lat)):
        raise ValueError("Gate 1 polygon is invalid or excludes its center")

    catalog = load_catalog()
    facilities = catalog["facilities"]
    keyword_to_id = {term: facility["id"] for facility in facilities for term in facility["search_terms"]}
    if len(keyword_to_id) != sum(len(f["search_terms"]) for f in facilities):
        raise ValueError("catalog search terms must be unique for staged search")
    buffer_m = max([1000] + [f.get("service_distance_m") or 0 for f in facilities])
    radius_m = minimum_poi_search_radius_m(center, geometry, buffer_m)

    previous = {}
    completed_keywords = {}
    partition_queries = {}
    if resume_file is not None:
        saved = json.loads(Path(resume_file).read_text(encoding="utf-8"))
        if (saved["catalog_version"] != catalog["version"] or saved["center"] != center.model_dump()
                or saved["search_radius_m"] != radius_m or saved["gate1_file"] != Path(gate1_file).name):
            raise ValueError("resume result belongs to a different Gate 1 run or catalog")
        previous = {row["facility_id"]: row for row in saved["categories"]}
        completed_keywords = saved.get("completed_keywords", {})
        partition_queries = saved.get("partition_queries", {})

    if partition_incomplete and (resume_file is None or (start, end) == (1, 45)
            or any(previous.get(f["id"], {}).get("search_status") != "partial"
                   for f in facilities[start - 1:end])):
        raise ValueError("partition requires an explicitly selected partial resume category")

    selected = {f["id"] for f in facilities[start - 1:end]
                if previous.get(f["id"], {}).get("search_status") != "complete"}
    attempted = set()
    request_counts = {}
    provider_errors = {}
    calls = 0
    last_call = 0.0
    halted = None

    def transport(url, timeout):
        nonlocal calls, last_call, halted
        if halted is not None or calls >= max_http:
            halted = halted or "HTTP_BUDGET_EXHAUSTED"
            raise TimeoutError("Gate 2 local HTTP budget reached")
        keyword = parse_qs(urlsplit(url).query).get("query", [""])[0]
        facility_id = keyword_to_id[keyword]
        attempted.add(facility_id)
        time.sleep(max(0.0, pace_seconds - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        calls += 1
        request_counts[facility_id] = request_counts.get(facility_id, 0) + 1
        with http_get(url, timeout=timeout) as response:
            body = response.read()
        try:
            payload = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return body  # Let the existing provider classify malformed responses.
        if isinstance(payload, dict) and payload.get("status") in {4, 302, "4", "302"}:
            halted = "DAILY_POI_QUOTA_EXHAUSTED"
        return payload

    provider = BaiduPOIProvider(ak=ak, http_get=transport)

    positions = {p["poi_id"]: (p["location"]["lng"], p["location"]["lat"])
                 for row in previous.values() for p in row["pois"]}

    def partition_search(keyword, point, search_radius_m):
        unique = {}
        attempted.add(keyword_to_id[keyword])

        def retain(items):
            for item in items:
                key, position = _public_id(item), (item.lng, item.lat)
                if key in positions and positions[key] != position:
                    raise POIProviderError("COORDINATE_CONFLICT", "POI coordinates disagree.", False)
                positions[key] = position
                if circle_distance_m(center, item) <= radius_m:
                    unique.setdefault(key, item)

        def visit(child, radius, depth):
            key = json.dumps([PARTITION_VERSION, keyword, child.model_dump(), radius], sort_keys=True)
            cached = partition_queries.get(key, {})
            retain([ProviderPOI(**item) for item in cached.get("pois", [])])
            if cached.get("status") == "complete":
                return
            if cached.get("status") != "split":
                before = calls
                error = None
                try:
                    items = provider.search(keyword, child, radius)
                except POIProviderError as exc:
                    error, items = exc, exc.partial_results or []
                retain(items)
                status = ("complete" if error is None else "split"
                          if error.code == "INCOMPLETE_RESULTS" and depth < 2 else "partial")
                observations = {_public_id(ProviderPOI(**item)): item for item in cached.get("pois", [])}
                observations.update({_public_id(item): asdict(item) for item in items})
                partition_queries[key] = {
                    "version": PARTITION_VERSION, "keyword": keyword,
                    "parent_center": point.model_dump(), "parent_radius_m": search_radius_m,
                    "center": child.model_dump(), "radius_m": radius, "depth": depth,
                    "status": status, "pois": list(observations.values()),
                    "captured_at": datetime.now(timezone.utc).isoformat() if calls > before else None,
                }
                if status == "complete":
                    return
                if status == "partial":
                    raise error
            for descendant, descendant_radius in partition_circles(child, radius):
                visit(descendant, descendant_radius, depth + 1)

        try:
            for child, child_radius in partition_circles(point, search_radius_m):
                visit(child, child_radius, 1)
        except POIProviderError as error:
            raise POIProviderError(error.code, str(error), error.retryable,
                                   partial_results=list(unique.values())) from error
        return list(unique.values())

    class SelectedProvider:
        def search(self, keyword, point, search_radius_m):
            nonlocal halted
            facility_id = keyword_to_id[keyword]
            if facility_id not in selected:
                raise POIProviderError("NOT_RUN", "This category was not searched in this run.", False)
            if keyword in completed_keywords:
                attempted.add(facility_id)
                items = [ProviderPOI(**item) for item in completed_keywords[keyword]]
                return ([item for item in items if circle_distance_m(center, item) <= radius_m]
                        if partition_incomplete else items)
            if halted is not None:
                raise POIProviderError("NOT_RUN", "This category was not searched in this run.", False)
            try:
                items = (partition_search(keyword, point, search_radius_m) if partition_incomplete
                         else provider.search(keyword, point, search_radius_m))
                completed_keywords[keyword] = [asdict(item) for item in items]
                return items
            except POIProviderError as error:
                provider_errors.setdefault(facility_id, set()).add(error.code)
                if partition_incomplete and error.code not in {"INCOMPLETE_RESULTS", "COORDINATE_CONFLICT"}:
                    halted = halted or error.code
                raise

    searched = search_catalog(SelectedProvider(), center, geometry.model_dump(), radius_m)
    current = {row["facility_id"]: row for row in searched if row["facility_id"] in attempted}
    for facility_id, row in current.items():
        old = previous.get(facility_id)
        if old is not None and row["search_status"] != "complete":
            pois = {item["poi_id"]: item for item in old["pois"]}
            pois.update({item["poi_id"]: item for item in row["pois"]})
            row["pois"] = list(pois.values())
            row["known_count"] = sum(item["in_isochrone"] for item in row["pois"])
            if old["search_status"] == "partial" or row["search_status"] == "partial":
                row["search_status"] = "partial"
                row["warnings"] = [{"code": "PARTIAL_POI_RESULTS",
                                    "message": f"{row['name_zh']}设施检索结果不完整。"}]
        previous[facility_id] = row
    rows = [previous[f["id"]] for f in facilities if f["id"] in previous]
    counts = {status: sum(row["search_status"] == status for row in rows)
              for status in ("complete", "partial", "failed")}
    counts["pending"] = len(facilities) - len(rows)
    result = {
        "gate1_file": Path(gate1_file).name,
        "catalog_version": catalog["version"],
        "center": center.model_dump(),
        "search_radius_m": radius_m,
        "categories": rows,
        "completed_keywords": completed_keywords,
        "status_counts": counts,
        "http_requests_this_run": calls,
        "http_requests_by_facility_this_run": request_counts,
        "provider_error_codes_this_run": {key: sorted(value) for key, value in provider_errors.items()},
        "max_http_requests_this_run": max_http,
        "stop_reason": halted,
    }
    if partition_queries:
        result["partition_queries"] = partition_queries
    destination = Path(output_dir) if output_dir is not None else ROOT / "data" / "cache"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"gate2-live-{uuid.uuid4().hex}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate1", type=Path, required=True)
    parser.add_argument("--max-http", type=int, default=20)
    parser.add_argument("--start", type=int, default=1, help="1-based first facility")
    parser.add_argument("--end", type=int, default=45, help="1-based last facility")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--partition-incomplete", action="store_true")
    args = parser.parse_args()
    if os.environ.get("RUN_BAIDU_GATE2") != "1":
        print("Skipped: set RUN_BAIDU_GATE2=1 for one real POI stage.")
        return 2
    ak = os.environ.get("BAIDU_MAP_AK", "").strip()
    if not ak:
        print("Skipped: server-side BAIDU_MAP_AK is not configured.")
        return 2
    try:
        result, output = run_gate2(args.gate1, ak=ak, max_http=args.max_http,
                                   start=args.start, end=args.end, resume_file=args.resume,
                                   partition_incomplete=args.partition_incomplete)
    except Exception as exc:
        print("gate2_exception_type:", type(exc).__name__)
        return 1
    print("saved:", output.relative_to(ROOT))
    print("search_radius_m:", result["search_radius_m"])
    print("category_statuses:", result["status_counts"])
    print("provider_http_requests:", result["http_requests_this_run"])
    print("stop_reason:", result["stop_reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
