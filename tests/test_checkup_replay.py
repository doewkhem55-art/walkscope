"""Saved-report replay: question debugging without re-running a checkup.

The formal question pipeline must work on a saved completed report loaded from
``CHECKUP_REPLAY_DIR`` / ``CHECKUP_REPLAY_REPORTS``. No map or facility provider
is touched, so this path never spends the Baidu quota.
"""

import json

from fastapi.testclient import TestClient

from app.api import checkups
from app.api.main import app
from tests.report_fixtures import build_report


def _write_job(directory, report_id="replay-report"):
    report = build_report(report_id)
    payload = {
        "id": report.report_id,
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "result": report.model_dump(mode="json"),
    }
    path = directory / f"{report.report_id}_report.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_replay_reports_directly(tmp_path):
    path = _write_job(tmp_path)

    loaded = checkups.load_replay_reports([path])

    assert loaded == ["replay-report"]
    replayed = checkups._jobs["replay-report"][1].result
    assert replayed.report_id == "replay-report"
    assert replayed.quality["historical_replay"] is True
    assert replayed.quality["not_live_checkup"] is True
    assert replayed.quality["replay_source_id"] == "replay-report"
    assert replayed.quality["replay_source_created_at"] == replayed.created_at
    checkups._jobs.pop("replay-report", None)


def test_startup_env_dir_loads_reports_and_questions_work(tmp_path, monkeypatch):
    _write_job(tmp_path, "replay-startup")
    monkeypatch.setenv("CHECKUP_REPLAY_DIR", str(tmp_path))
    monkeypatch.setattr(checkups, "_jobs", {})
    monkeypatch.setattr(checkups, "_questions_running", set())
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/checkups/replay-startup").status_code == 200
            catalog = client.get("/api/v1/checkup-catalog").json()
            assert "replay-startup" in catalog["runtime"]["replay_report_ids"]
            answer = client.post(
                "/api/v1/checkups/replay-startup/questions",
                json={"question": "小学够不够？"},
            )
            assert answer.status_code == 200
            assert answer.json()["mode"] == "report_facts"
    finally:
        app.dependency_overrides.clear()


def test_replay_loader_skips_failed_or_broken_files(tmp_path):
    broken = tmp_path / "broken_report.json"
    broken.write_text("{not json", encoding="utf-8")
    failed = tmp_path / "failed_report.json"
    failed.write_text(json.dumps({"id": "failed-1", "status": "failed", "stage": "failed",
                                  "progress": 100, "result": None}), encoding="utf-8")

    assert checkups.load_replay_reports([broken, failed]) == []
