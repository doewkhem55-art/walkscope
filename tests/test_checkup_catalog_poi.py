from shapely.geometry import Polygon

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.poi import search_catalog
from app.providers.baidu.poi import POIProviderError, ProviderPOI
from app.schemas.common import CenterPoint


CENTER = CenterPoint(lng=121.5, lat=31.2, crs="BD09LL")
ISOCHRONE = Polygon([
    (121.49, 31.19), (121.51, 31.19), (121.51, 31.21),
    (121.49, 31.21), (121.49, 31.19),
])


class FakeProvider:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def search(self, keyword, center, radius_m):
        self.calls.append((keyword, center, radius_m))
        result = self.responses.get(keyword, [])
        if isinstance(result, Exception):
            raise result
        return result


def poi(uid, name, lng=121.5, lat=31.2):
    return ProviderPOI(uid, name, lng, lat, "示例路1号")


def test_catalog_merges_analysis_metadata_and_preserves_search_terms():
    catalog = load_catalog()
    facilities = catalog["facilities"]
    by_id = {item["id"]: item for item in facilities}

    assert catalog["version"] == "2026.1"
    assert len(catalog["groups"]) == 7
    assert len(facilities) == 45
    assert by_id["childcare_0_3"]["search_terms"] == ["托育机构", "托育中心"]
    assert by_id["primary_school"]["metric_profile"] == "legacy_community_15m_1km"
    assert by_id["fire_station"]["coverage_method"] == "insufficient_evidence"
    assert sum(item["coverage_method"] == "grid_walking_distance" for item in facilities) == 38
    assert sum(item["coverage_method"] == "insufficient_evidence" for item in facilities) == 7


def test_search_catalog_dedupes_per_facility_and_covers_boundary_points():
    duplicate = poi("same", "托育中心")
    provider = FakeProvider({
        "托育机构": [
            duplicate,
            poi("edge", "边界托育点", lng=121.49),
            poi("outer", "圈外托育点", lng=121.515),
        ],
        "托育中心": [duplicate, poi("edge-2", "另一边界点", lng=121.51)],
    })
    progress = []

    results = search_catalog(provider, CENTER, ISOCHRONE, 1450, progress.append)
    childcare = results[0]
    by_id = {item["poi_id"]: item for item in childcare["pois"]}

    assert len(results) == 45
    assert childcare["facility_id"] == "childcare_0_3"
    assert childcare["search_status"] == "complete"
    assert childcare["count"] == childcare["known_count"] == 3
    assert len(childcare["pois"]) == 4
    assert by_id["baidu:same"]["in_isochrone"] is True
    assert by_id["baidu:edge"]["in_isochrone"] is True
    assert by_id["baidu:outer"]["in_isochrone"] is False
    assert by_id["baidu:edge-2"]["in_isochrone"] is True
    assert provider.calls[:2] == [
        ("托育机构", CENTER, 1450),
        ("托育中心", CENTER, 1450),
    ]
    assert all(call[2] == 1450 for call in provider.calls)
    assert progress == list(range(1, 46))


def test_partial_and_failed_categories_keep_known_pois_and_hide_provider_errors():
    provider = FakeProvider({
        "托育机构": [poi("known", "已知托育点")],
        "托育中心": RuntimeError("secret AK=do-not-return"),
        "幼儿园": POIProviderError("RATE_LIMITED", "secret provider detail", True),
    })

    results = search_catalog(provider, CENTER, ISOCHRONE, 1500)
    by_id = {item["facility_id"]: item for item in results}
    partial = by_id["childcare_0_3"]
    failed = by_id["kindergarten"]
    empty = by_id["primary_school"]

    assert partial["search_status"] == "partial"
    assert partial["count"] is None
    assert partial["known_count"] == 1
    assert [item["poi_id"] for item in partial["pois"]] == ["baidu:known"]
    assert partial["warnings"][0]["code"] == "PARTIAL_POI_RESULTS"
    assert "secret" not in partial["warnings"][0]["message"]
    assert failed["search_status"] == "failed"
    assert failed["count"] is None
    assert failed["known_count"] == 0
    assert failed["warnings"][0]["code"] == "POI_SEARCH_FAILED"
    assert empty["search_status"] == "complete"
    assert empty["count"] == 0


def test_daily_quota_stops_catalog_http_and_leaves_remaining_unknown():
    provider = FakeProvider({
        "托育机构": POIProviderError("DAILY_QUOTA_EXCEEDED", "quota", False),
    })
    results = search_catalog(provider, CENTER, ISOCHRONE, 1500)
    assert len(provider.calls) == 1
    assert all(row["search_status"] == "failed" and row["count"] is None for row in results)


def test_daily_quota_stops_remaining_searches_and_labels_the_failure():
    catalog = load_catalog()
    selected = catalog["facilities"][:3]
    responses = {
        keyword: POIProviderError("DAILY_QUOTA_EXCEEDED", "quota", False)
        for keyword in selected[0]["search_terms"]
    }
    provider = FakeProvider(responses)
    results = search_catalog(provider, CENTER, ISOCHRONE, 1500, facilities=selected)

    assert len(results) == 3
    assert results[0]["search_status"] == "failed"
    assert results[0]["warnings"][0]["code"] == "POI_DAILY_QUOTA_EXHAUSTED"
    assert results[1]["search_status"] == "failed"
    assert results[1]["warnings"][0]["code"] == "POI_DAILY_QUOTA_EXHAUSTED"
    assert results[2]["search_status"] == "failed"
    # Quota stops the remaining keywords and categories without new requests.
    assert len(provider.calls) == 1
