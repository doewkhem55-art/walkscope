"""Work line 3 API tests: ``POST /api/v1/checkups/{id}/questions`` boundary behaviour.

Complements the work line 1+2 API tests (``tests/test_checkup_api.py``) without
duplicating them: this file owns the question endpoint's contract, its answer states
and the guarantee that a failed question never damages the stored report.

All offline: dependency overrides replace both the checkup runner and the model.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import checkups
from app.api.main import app
from app.schemas.checkup import ReportAnswer
from tests.report_fixtures import build_report

CHECKUP_REQUEST = {
    "location": {"type": "coordinate", "lng": 121.5, "lat": 31.2, "crs": "BD09LL"}
}
QUESTION_URL = "/api/v1/checkups/{checkup_id}/questions"


class HoldingExecutor:
    """Keeps the submitted job so the test decides when it runs."""

    def __init__(self):
        self.pending = []

    def submit(self, function, *args, **kwargs):
        self.pending.append((function, args, kwargs))

    def run_next(self):
        function, args, kwargs = self.pending.pop(0)
        function(*args, **kwargs)


class StubModel:
    """Minimal stand-in for the DeepSeek adapter used by the route dependency."""

    def __init__(self, payload):
        self.payload = payload

    def generate_object(self, **_kwargs):
        return self.payload


@pytest.fixture
def client(monkeypatch):
    saved_overrides = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    monkeypatch.setattr(checkups, "_jobs", {})
    monkeypatch.setattr(checkups, "_questions_running", set())
    monkeypatch.setattr(checkups, "_lock", threading.Lock())
    monkeypatch.setattr(checkups, "_executor", HoldingExecutor())
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


@pytest.fixture
def completed_checkup(client):
    """Submit one synthetic checkup and finish it, returning (client, checkup_id)."""

    def runner(_request, *, report_id, progress):
        progress("report", 95)
        return build_report(report_id)

    app.dependency_overrides[checkups.get_checkup_runner] = lambda: runner
    queued = client.post("/api/v1/checkups", json=CHECKUP_REQUEST)
    assert queued.status_code == 202
    checkup_id = queued.json()["id"]
    checkups._executor.run_next()
    assert client.get(f"/api/v1/checkups/{checkup_id}").json()["status"] == "completed"
    return checkup_id


def _stored_report(client, checkup_id):
    return client.get(f"/api/v1/checkups/{checkup_id}").json()["result"]


def _ask(client, checkup_id, question, **kwargs):
    payload = question if isinstance(question, dict) else {"question": question}
    return client.post(QUESTION_URL.format(checkup_id=checkup_id), json=payload, **kwargs)


def test_question_on_completed_report_returns_the_contract_answer(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: None

    response = _ask(client, completed_checkup, "婴幼儿托育有多少个？")

    assert response.status_code == 200
    answer = ReportAnswer.model_validate(response.json())
    assert answer.mode == "report_facts"
    assert answer.evidence_ids == ["facility:childcare_0_3"]


def test_question_is_refused_before_the_report_exists(client):
    app.dependency_overrides[checkups.get_checkup_runner] = lambda: (
        lambda _request, *, report_id, progress: build_report(report_id)
    )
    checkup_id = client.post("/api/v1/checkups", json=CHECKUP_REQUEST).json()["id"]

    queued_answer = _ask(client, checkup_id, "报告好了吗？")
    assert queued_answer.status_code == 409

    checkups._executor.run_next()
    assert _ask(client, checkup_id, "现在可以问了吗？").status_code == 200


def test_question_on_a_failed_report_is_refused(client):
    app.dependency_overrides[checkups.get_checkup_runner] = lambda: (
        lambda _request, *, report_id, progress: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    checkup_id = client.post("/api/v1/checkups", json=CHECKUP_REQUEST).json()["id"]
    checkups._executor.run_next()
    stored = client.get(f"/api/v1/checkups/{checkup_id}").json()
    assert (stored["status"], stored["result"]) == ("failed", None)

    assert _ask(client, checkup_id, "为什么会失败？").status_code == 409


def test_question_on_an_unknown_or_expired_report_is_404(client, completed_checkup):
    assert _ask(client, "no-such-checkup", "还在吗？").status_code == 404


def test_question_on_an_untrusted_origin_is_refused(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: None

    response = _ask(client, completed_checkup, "托育情况？", headers={"Origin": "https://untrusted.example"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 2001},
        {"question": "正常问题", "messages": []},
        {"question": "正常问题", "poi_id": "poi:in"},
        {"question": 123},
        {"question": "正常问题", "mode": "model"},
        {"question": "正常问题", "history": [{"role": "system", "content": "x"}]},
        {"question": "正常问题", "history": [{"role": "user", "content": "x"}] * 13},
        {"question": "正常问题", "history": [{"role": "user", "content": "x" * 4001}]},
    ],
)
def test_unagreed_request_fields_are_rejected_not_ignored(client, completed_checkup, payload):
    """The v1.1 contract accepts question/history/focus/mode; anything else is 422,
    and history stays bounded instead of being silently truncated by the server."""
    response = client.post(QUESTION_URL.format(checkup_id=completed_checkup), json=payload)

    assert response.status_code == 422

def test_history_focus_and_mode_are_accepted(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    payload = {
        "question": "这个类别怎么样？",
        "history": [
            {"role": "user", "content": "报告里小学多少分？"},
            {"role": "assistant", "content": "小学是90分。"},
        ],
        "focus": {"facility_id": "primary_school", "group_id": None, "poi_id": None,
                  "selected_facility_ids": None},
        "mode": "report",
    }
    response = _ask(client, completed_checkup, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "report_facts"
    assert body["retrieval"]["status"] == "not_requested"
    assert body["focus_applied"]["facility_id"] == "primary_school"
    assert body["elapsed_ms"] is not None


def test_focus_outside_the_report_is_422(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    for focus in (
        {"facility_id": "not-in-report"},
        {"group_id": "not-a-group"},
        {"poi_id": "not-a-poi"},
        {"selected_facility_ids": ["primary_school", "not-in-report"]},
    ):
        response = _ask(client, completed_checkup, {"question": "解释一下", "focus": focus})
        assert response.status_code == 422


def test_web_failure_is_visible_in_the_answer_instead_of_a_502(client, completed_checkup):
    class BrokenSearch:
        def search(self, *_args, **_kwargs):
            raise RuntimeError("spotlight quota exceeded")

        def fetch(self, *_args, **_kwargs):
            raise RuntimeError("fetch down")

    class ReportOnlyModel:
        def generate_object(self, **_kwargs):
            return {"answer": "本次未获得联网补充，只依据报告。", "evidence_ids": ["report"], "mode": "model"}

    app.dependency_overrides[checkups.get_report_model] = lambda: ReportOnlyModel()
    app.dependency_overrides[checkups.get_report_search] = lambda: BrokenSearch()
    response = _ask(client, completed_checkup, {"question": "上网查一下最新政策", "mode": "web"})

    assert response.status_code == 200
    body = response.json()
    assert body["retrieval"]["status"] == "failed"
    assert "quota" in body["retrieval"]["error"]
    assert body["web_search_used"] is False


def test_model_answer_with_unknown_evidence_becomes_a_limited_report_answer(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: StubModel({
        "answer": "引用了不存在来源的回答",
        "evidence_ids": ["poi:poi:childcare_0_3:in"],
        "mode": "model",
    })
    before = _stored_report(client, completed_checkup)

    response = _ask(client, completed_checkup, "解释一下最近的托育点")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "report_facts"
    assert "未通过证据或来源校验" in body["answer"]
    assert _stored_report(client, completed_checkup) == before


def test_model_answer_without_evidence_becomes_a_limited_report_answer(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: StubModel({
        "answer": "没有任何依据",
        "evidence_ids": [],
        "mode": "model",
    })

    response = _ask(client, completed_checkup, "解释一下")

    assert response.status_code == 200
    assert response.json()["mode"] == "report_facts"


def test_model_failure_does_not_change_the_report_or_block_later_questions(client, completed_checkup):
    failing = StubModel(None)
    failing.generate_object = lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("model timeout"))
    app.dependency_overrides[checkups.get_report_model] = lambda: failing
    before = _stored_report(client, completed_checkup)

    assert _ask(client, completed_checkup, "超时试试").status_code == 502
    assert _stored_report(client, completed_checkup) == before

    # A later, healthy question on the same report still works.
    app.dependency_overrides[checkups.get_report_model] = lambda: StubModel({
        "answer": "恢复后的回答",
        "evidence_ids": ["report"],
        "mode": "model",
    })
    recovered = _ask(client, completed_checkup, "再问一次")
    assert recovered.status_code == 200
    assert recovered.json()["mode"] == "model"


def test_model_answer_cannot_smuggle_point_level_evidence(client, completed_checkup):
    """Point evidence is accepted only when the application put that point in focus."""
    app.dependency_overrides[checkups.get_report_model] = lambda: StubModel({
        "answer": "该托育点步行约9分钟。",
        "evidence_ids": ["facility:childcare_0_3", "poi:poi:childcare_0_3:in"],
        "mode": "model",
    })

    response = _ask(client, completed_checkup, "最近的托育点走多久？")

    assert response.status_code == 200
    assert response.json()["mode"] == "report_facts"
    assert "未通过证据或来源校验" in response.json()["answer"]


def test_report_id_and_question_id_stay_bound_to_the_same_checkup(client, completed_checkup):
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    other_id = client.post("/api/v1/checkups", json=CHECKUP_REQUEST).json()["id"]

    # The second checkup is still queued, so the completed one is the only reportable job,
    # and the answer always belongs to the checkup named in the path.
    answer = _ask(client, completed_checkup, "婴幼儿托育情况？")
    assert answer.status_code == 200
    assert answer.json()["evidence_ids"] == ["facility:childcare_0_3"]

    assert _ask(client, other_id, "婴幼儿托育情况？").status_code == 409


def test_stream_endpoint_emits_stages_and_final_compatible_answer(client, completed_checkup):
    import json
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    app.dependency_overrides[checkups.get_report_search] = lambda: None
    response = client.post(QUESTION_URL.format(checkup_id=completed_checkup) + '/stream', json={'question': '报告如何', 'mode': 'report'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]['stage'] == 'report'
    assert events[-1]['type'] == 'result'
    assert ReportAnswer.model_validate(events[-1]['result']).mode == 'report_facts'
    assert not checkups._questions_running


def test_stream_failure_clears_draft_and_releases_shared_question_lock(client, completed_checkup):
    import json
    class BrokenStream:
        def stream_object(self, *, on_answer, **kwargs):
            on_answer('不完整草稿')
            raise RuntimeError('offline stream failure')
        def generate_object(self, **kwargs):
            raise RuntimeError('offline retry failure')
    app.dependency_overrides[checkups.get_report_model] = lambda: BrokenStream()
    app.dependency_overrides[checkups.get_report_search] = lambda: None
    response = client.post(QUESTION_URL.format(checkup_id=completed_checkup) + '/stream', json={'question': '说明', 'mode': 'report'})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]['type'] == 'error'
    assert events[-2]['type'] == 'draft_reset'
    assert not any(event['type'] == 'result' for event in events)
    assert not checkups._questions_running
    app.dependency_overrides[checkups.get_report_model] = lambda: None
    assert _ask(client, completed_checkup, '再试').status_code == 200
