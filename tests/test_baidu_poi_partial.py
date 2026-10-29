import json
from urllib.parse import parse_qs, urlsplit

import pytest
from shapely.geometry import Polygon

from app.modules.checkup.poi import search_catalog
from app.modules.poi.service import POIService
from app.providers.baidu.poi import BaiduPOIProvider, POIProviderError, ProviderPOI
from app.schemas.common import CenterPoint
from app.schemas.poi import POISearchRequest


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def close(self):
        pass


def test_page_limit_error_retains_all_pages_already_received():
    pages = []

    def http_get(url, *, timeout):
        page = int(parse_qs(urlsplit(url).query)["page_num"][0])
        pages.append(page)
        results = [
            {
                "uid": f"uid-{page}-{index}",
                "name": f"POI {page}-{index}",
                "location": {"lng": 121.5, "lat": 31.2},
            }
            for index in range(20)
        ]
        return Response({"status": 0, "total": 151, "results": results})

    provider = BaiduPOIProvider(ak="fake-ak", http_get=http_get)
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    with pytest.raises(POIProviderError) as caught:
        provider.search("测试关键词", center, 1500)

    error = caught.value
    assert error.code == "INCOMPLETE_RESULTS"
    assert pages == list(range(provider.MAX_PAGES))
    assert len(error.partial_results) == provider.MAX_PAGES * provider.PAGE_SIZE
    assert error.partial_results[0].uid == "uid-0-0"
    assert error.partial_results[-1].uid == f"uid-{provider.MAX_PAGES - 1}-19"


def test_mid_page_timeout_retains_partial_results_in_checkup_with_unknown_count():
    pages = []

    def http_get(url, *, timeout):
        query = parse_qs(urlsplit(url).query)
        keyword = query["query"][0]
        page = int(query["page_num"][0])
        pages.append((keyword, page))
        if keyword == "幼儿园" and page == 0:
            return Response({
                "status": 0,
                "total": 40,
                "results": [{
                    "uid": "kindergarten-1",
                    "name": "已取得幼儿园",
                    "location": {"lng": 121.5, "lat": 31.2},
                }],
            })
        if keyword == "幼儿园":
            raise TimeoutError("private request budget detail")
        return Response({"status": 0, "results": []})

    provider = BaiduPOIProvider(ak="fake-ak", http_get=http_get)
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    polygon = Polygon([
        (121.49, 31.19), (121.51, 31.19), (121.51, 31.21),
        (121.49, 31.21), (121.49, 31.19),
    ])

    categories = {
        item["facility_id"]: item
        for item in search_catalog(provider, center, polygon, 1500)
    }
    kindergarten = categories["kindergarten"]

    assert ("幼儿园", 0) in pages
    assert ("幼儿园", 1) in pages
    assert kindergarten["search_status"] == "partial"
    assert kindergarten["count"] is None
    assert kindergarten["known_count"] == 1
    assert kindergarten["pois"][0]["poi_id"] == "baidu:kindergarten-1"


def test_checkup_catalog_keeps_page_cap_partial_results():
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    polygon = Polygon([
        (121.49, 31.19), (121.51, 31.19), (121.51, 31.21),
        (121.49, 31.21), (121.49, 31.19),
    ])
    partial = [ProviderPOI("cap-1", "已取得设施", 121.5, 31.2, "示例路")]

    class Provider:
        def search(self, keyword, center, radius_m):
            if keyword == "幼儿园":
                raise POIProviderError(
                    "INCOMPLETE_RESULTS", "page cap", False, partial_results=partial
                )
            if keyword in {"中学", "初中", "高中"}:
                raise POIProviderError(
                    "TIMEOUT", "timeout", True, partial_results=partial
                )
            return []

    categories = {
        item["facility_id"]: item
        for item in search_catalog(Provider(), center, polygon, 1500)
    }
    kindergarten = categories["kindergarten"]
    secondary_school = categories["secondary_school"]

    assert kindergarten["search_status"] == "partial"
    assert kindergarten["count"] is None
    assert kindergarten["known_count"] == 1
    assert kindergarten["pois"][0]["poi_id"] == "baidu:cap-1"
    assert secondary_school["search_status"] == "partial"
    assert secondary_school["count"] is None
    assert secondary_school["known_count"] == 1
    assert secondary_school["pois"][0]["poi_id"] == "baidu:cap-1"


def test_v1_poi_service_keeps_its_existing_count_contract():
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    request = POISearchRequest.model_validate({
        "schema_version": "1.0",
        "center": center.model_dump(),
        "facility_types": ["market"],
        "search_radius_m": 1500,
    })
    partial = ProviderPOI("cap-1", "市场", 121.5, 31.2, "示例路")

    class Provider:
        def search(self, keyword, center, radius_m):
            if keyword == "菜市场":
                raise POIProviderError(
                    "INCOMPLETE_RESULTS", "page cap", False, partial_results=[partial]
                )
            return []

    result = POIService(Provider()).search(request).root

    assert result.ok is True
    assert result.data.counts.market is None
    assert result.data.pois == []


def test_v1_poi_service_keeps_unknown_count_for_mid_page_partial_error():
    center = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
    request = POISearchRequest.model_validate({
        "schema_version": "1.0",
        "center": center.model_dump(),
        "facility_types": ["market"],
        "search_radius_m": 1500,
    })
    partial = ProviderPOI("timeout-1", "市场", 121.5, 31.2, "示例路")

    class Provider:
        def search(self, keyword, center, radius_m):
            if keyword == "菜市场":
                raise POIProviderError(
                    "TIMEOUT", "timeout", True, partial_results=[partial]
                )
            return []

    result = POIService(Provider()).search(request).root

    assert result.ok is True
    assert result.data.counts.market is None
    assert result.data.pois == []
