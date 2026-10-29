"""Terminal reports survive memory eviction/restart without another map run."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import checkups
from app.api.main import app
from app.modules.checkup.service import CheckupError
from app.schemas.checkup import CheckupJob
from tests.report_fixtures import build_report


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKUP_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("CHECKUP_REPLAY_DIR", raising=False)
    monkeypatch.delenv("CHECKUP_REPLAY_REPORTS", raising=False)
    monkeypatch.setattr(checkups, "_jobs", {})
    return tmp_path / "reports"


def execute(report_id, *, failed=False):
    checkups._jobs[report_id] = (time.monotonic(), CheckupJob(
        id=report_id, status="queued", stage="queued", progress=0))

    def runner(_request, *, report_id, progress):
        progress("report", 95)
        if failed:
            raise CheckupError("TEST_FAILURE", "Test unavailable", False)
        return build_report(report_id)

    checkups._execute(report_id, None, runner)


@pytest.mark.parametrize("failed", [False, True])
def test_terminal_job_survives_memory_clear_and_startup(isolated_store, failed):
    execute("durable-job", failed=failed)
    before = checkups.checkup_status("durable-job").model_dump(mode="json")
    path = isolated_store / "durable-job_report.json"
    assert path.is_file()
    assert json.loads(path.read_text()) == before
    assert list(isolated_store.glob("*.tmp")) == []

    checkups._jobs.clear()
    with TestClient(app) as client:
        response = client.get("/api/v1/checkups/durable-job")
    assert response.status_code == 200
    assert response.json() == before
    assert response.json()["status"] == ("failed" if failed else "completed")


def test_malformed_and_nonterminal_files_do_not_hide_good_report(isolated_store, caplog):
    execute("good-report")
    (isolated_store / "broken_report.json").write_text("{broken", encoding="utf-8")
    (isolated_store / "queued_report.json").write_text(json.dumps({
        "id": "queued", "status": "queued", "stage": "queued", "progress": 0}), encoding="utf-8")
    mismatched = checkups.checkup_status("good-report").model_dump(mode="json")
    mismatched["id"] = "mismatched"
    (isolated_store / "mismatched_report.json").write_text(json.dumps(mismatched), encoding="utf-8")
    checkups._jobs.clear()

    with TestClient(app) as client:
        assert client.get("/api/v1/checkups/good-report").status_code == 200
        assert client.get("/api/v1/checkups/queued").status_code == 404
        assert client.get("/api/v1/checkups/mismatched").status_code == 404
    assert "persisted" in caplog.text.lower()


def test_failed_atomic_write_keeps_successful_result_in_memory(isolated_store, monkeypatch, caplog):
    def fail_replace(*_args):
        raise PermissionError("disk unavailable")

    monkeypatch.setattr(checkups.os, "replace", fail_replace)
    execute("write-failure")
    job = checkups.checkup_status("write-failure")
    assert job.status == "completed"
    assert job.result.report_id == "write-failure"
    assert job.error is None
    assert not (isolated_store / "write-failure_report.json").exists()
    assert list(isolated_store.glob("*.tmp")) == []
    assert "persist" in caplog.text.lower()


def test_memory_ttl_and_eviction_do_not_remove_durable_report(isolated_store):
    execute("expired-report")
    created, job = checkups._jobs["expired-report"]
    checkups._jobs["expired-report"] = (created - checkups._TTL_SECONDS - 1, job)
    assert checkups.checkup_status("expired-report").result.report_id == "expired-report"
    checkups._jobs.clear()
    assert checkups.checkup_status("expired-report").result.report_id == "expired-report"


def test_status_never_reads_a_path_outside_report_directory(isolated_store, tmp_path):
    execute("safe-report")
    payload = (isolated_store / "safe-report_report.json").read_text()
    (tmp_path / "outside_report.json").write_text(payload, encoding="utf-8")
    with pytest.raises(checkups.HTTPException) as caught:
        checkups.checkup_status("../outside")
    assert caught.value.status_code == 404
