from copy import deepcopy

import pytest

from app.schemas.checkup import CheckupJob
from app.providers.baidu.poi import ProviderPOI
from test_checkup_service import CENTER, _run


def partial_case():
    snapshots = []
    original, *_ = _run({"小学": [ProviderPOI("school", "保存的小学", CENTER.lng, CENTER.lat, "地址")]},
        facility_ids=["primary_school"], max_grid_routes=0, on_update=snapshots.append)
    return original, CheckupJob(id=original.report_id, status="failed", stage="failed", progress=65,
        partial_result=deepcopy(snapshots[-1]), partial_revision=1,
        error={"code": "PROVIDER_BUDGET_EXHAUSTED", "message": "原任务达到上限", "retryable": False})


def test_restore_completed_search_and_center_routes_without_network(tmp_path):
    from tools.restore_partial_checkup import restore_partial_checkup

    original, source = partial_case()
    before = source.model_dump_json()
    restored = restore_partial_checkup(source, stage_dir=tmp_path, report_id="restored", label="保存的地点")
    report = restored.result
    assert source.model_dump_json() == before
    assert report.isochrone == original.isochrone
    assert report.pois == original.pois
    assert report.routes == original.routes
    assert report.score == original.score
    assert report.location_label == "保存的地点"
    assert report.quality["restored_from_failed_checkup"] == original.report_id
    assert report.quality["new_http_requests"] == 0
    assert report.categories[0].unknown_ratio > 0
    assert not report.quality["coverage_validation"]["evidence_complete"]


def test_restore_rejects_conflicting_saved_poi_counts(tmp_path):
    from tools.restore_partial_checkup import restore_partial_checkup

    _, source = partial_case()
    source.partial_result["categories"][0]["known_count"] += 1
    with pytest.raises(ValueError, match="count"):
        restore_partial_checkup(source, stage_dir=tmp_path, report_id="bad", label="保存的地点")
