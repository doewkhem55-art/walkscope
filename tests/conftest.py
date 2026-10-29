"""Keep API persistence tests and legacy endpoint tests off the real report store."""

import pytest


@pytest.fixture(autouse=True)
def isolated_report_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKUP_REPORT_DIR", str(tmp_path / "saved-checkups"))
