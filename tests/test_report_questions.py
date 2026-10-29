"""Work line 3 tests: the model-visible evidence package and its rejection rules.

These tests pin the boundary documented in the handoff package (03_reference
interface agreement): what a report question is allowed to see, and which model
answers must be refused instead of being shown to a user.

All offline: no map provider, no DeepSeek call, no HTTP request.
"""

import json

import pytest

from app.modules.checkup.catalog import load_catalog
from app.modules.checkup.questions import answer_report
from app.schemas.checkup import ReportAnswer, ReportFocus, ReportQuestion, RetrievalInfo
from tests.report_fixtures import (
    EMPTY_FACILITY,
    FACILITY_AREA_FACILITY,
    PARTIAL_FACILITY,
    REACHABLE_FACILITY,
    SERVICE_AREA_FACILITY,
    build_report,
)

# Keys the question service may expose. Anything else is out of contract today.
EXPECTED_SHARED_KEYS = {"report", *[f"group:{g['id']}" for g in load_catalog()["groups"]],
                        *[f"facility:{f['id']}" for f in load_catalog()["facilities"]]}


class RecordingModel:
    """Fake model that records the prompt it received and replays one payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_object(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    @property
    def last_user_message(self):
        return json.loads(self.calls[-1]["user_message"])

    @property
    def last_system_prompt(self):
        return self.calls[-1]["system_prompt"]


class SequenceModel(RecordingModel):
    """Fake model that replays a different payload per call (for retry tests)."""

    def __init__(self, payloads):
        super().__init__(payloads[0])
        self.payloads = list(payloads)

    def generate_object(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        if isinstance(payload, Exception):
            raise payload
        return payload


def accepting_model(evidence_ids=("report",)):
    return RecordingModel({
        "answer": "按当前报告，该类别有可解释的统计口径。",
        "evidence_ids": list(evidence_ids),
        "mode": "model",
    })


@pytest.fixture
def report():
    return build_report()


# --- evidence package scope -------------------------------------------------

def test_model_sees_every_report_group_and_category_key(report):
    model = accepting_model()
    answer_report(report, "这份报告整体如何？", model)

    evidence = model.last_user_message["evidence"]
    assert set(evidence) == EXPECTED_SHARED_KEYS
    assert len(evidence) == 1 + 7 + 45
    assert evidence["report"]["location"] == report.location_label
    assert evidence["report"]["score"] == report.score
    assert model.last_user_message["report_id"] == report.report_id
    assert model.last_user_message["question"] == "这份报告整体如何？"


def test_point_and_route_level_evidence_never_reach_the_model(report):
    model = accepting_model()
    answer_report(report, "最近的托育点在哪里？", model)

    serialized = model.calls[-1]["user_message"]
    assert "poi:" not in serialized
    assert "poi_id" not in serialized
    assert "address" not in serialized
    assert report.pois[0].name not in serialized
    assert "routes" not in serialized
    assert "target_id" not in serialized
    assert '"distance_m"' not in serialized and '"duration_s"' not in serialized
    # Category-level nearest figures are a report statistic, not a route record, and are
    # allowed to stay; they are distinguishable by their nearest_ prefix.
    assert "nearest_distance_m" in serialized
    # The circle-outside candidate and the two synthetic routes must not leak.
    assert report.pois[1].poi_id not in serialized


def test_evidence_package_carries_no_credentials_or_provider_call_plumbing(report):
    model = accepting_model()
    answer_report(report, "这次体检完整吗？", model)

    serialized = model.calls[-1]["user_message"].lower()
    for forbidden in ("api_key", "apikey", "authorization", "ak=", "baidu_map_ak", "sk-"):
        assert forbidden not in serialized
    # The budget counter is a report quality fact and stays visible; the transport
    # itself (URLs, quota keys, provider payloads) must not.
    assert "provider_requests" in serialized
    for forbidden in ("http://", "https://", "quota", "url=", "search_terms"):
        assert forbidden not in serialized


def test_system_prompt_keeps_the_documented_limits(report):
    model = accepting_model()
    answer_report(report, "这里适合养老吗？", model)

    prompt = model.last_system_prompt
    assert "不调用任何工具" in prompt
    assert "证据" in prompt
    for expectation in ("0", "null", "blind", "unknown", "圈内", "可达"):
        assert expectation in prompt
    for expectation in ("自然资源和规划主管部门", "前期基础排查", "不是覆盖率、法定达标率", "不代表完整行政社区", "不能直接据此提出新建学校"):
        assert expectation in prompt


# --- model answer validation ------------------------------------------------

@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"answer": "无依据", "evidence_ids": [], "mode": "model"}, "empty evidence list"),
        ({"answer": "无依据", "evidence_ids": ["poi:poi:childcare_0_3:in"], "mode": "model"}, "point-level key not provided"),
        ({"answer": "无依据", "evidence_ids": ["route:poi:childcare_0_3:in"], "mode": "model"}, "route-level key not provided"),
        ({"answer": "无依据", "evidence_ids": ["facility:not-in-report"], "mode": "model"}, "unknown facility"),
        ({"answer": "无依据", "evidence_ids": ["group:not-a-group"], "mode": "model"}, "unknown group"),
        ({"answer": "无依据", "evidence_ids": ["facility:childcare_0_3", "poi:in"], "mode": "model"}, "mixed valid and invalid"),
        ({"answer": "无依据", "evidence_ids": ["report"], "mode": "report_facts"}, "wrong mode"),
    ],
)
def test_unsupported_model_answers_become_a_limited_report_facts_answer(report, payload, reason):
    """An answer that cannot be grounded is replaced, never shown with swapped links."""
    answer = answer_report(report, "解释一下", RecordingModel(payload))

    assert answer.mode == "report_facts"
    assert answer.web_search_used is False
    assert answer.sources == []
    assert "未通过证据或来源校验" in answer.answer
    assert answer.evidence_ids  # the limited answer still cites report facts


@pytest.mark.parametrize(
    "payload",
    [
        ({"answer": "无依据", "evidence_ids": ["report"], "mode": "model", "extra": 1}),
        ({"answer": "无依据", "evidence_ids": ["report"], "mode": "model", "confidence": 0.9}),
        ({"answer": "", "evidence_ids": ["report"], "mode": "model"}),
        ({"answer": "无依据", "evidence_ids": "report", "mode": "model"}),
        ({"answer": "无依据", "evidence_ids": [f"facility:x{i}" for i in range(60)], "mode": "model"}),
    ],
)
def test_schema_invalid_model_payload_is_refused_not_shown(report, payload):
    with pytest.raises(Exception):
        answer_report(report, "解释一下", RecordingModel(payload))


def test_valid_model_answer_is_returned_as_the_contract_object(report):
    answer = answer_report(report, "托育情况如何？", accepting_model(["facility:childcare_0_3", "report"]))

    assert isinstance(answer, ReportAnswer)
    assert answer.mode == "model"
    assert answer.evidence_ids == ["facility:childcare_0_3", "report"]
    assert ReportAnswer.model_validate(answer.model_dump()) == answer


def test_field_name_evidence_triggers_one_corrective_retry(report):
    """A model that cites field names (report_id/score) gets exactly one re-ask."""
    model = SequenceModel([
        {"answer": "综合分 62.5。", "evidence_ids": ["report_id", "score"], "mode": "model"},
        {"answer": "综合分 62.5。", "evidence_ids": ["report"], "mode": "model"},
    ])

    answer = answer_report(report, "综合分是多少？", model)

    assert answer.evidence_ids == ["report"]
    assert len(model.calls) == 2
    correction = model.last_user_message
    assert correction["previous_evidence_ids"] == ["report_id", "score"]
    assert "顶层键" in correction["correction"]


def test_still_invalid_answer_is_replaced_by_a_limited_report_answer(report):
    model = SequenceModel([
        {"answer": "无依据", "evidence_ids": ["report_id"], "mode": "model"},
        {"answer": "无依据", "evidence_ids": ["report_id"], "mode": "model"},
    ])

    answer = answer_report(report, "综合分是多少？", model)

    assert answer.mode == "report_facts"
    assert answer.sources == [] and answer.web_search_used is False
    assert "未通过证据或来源校验" in answer.answer
    assert len(model.calls) == 2


def test_limited_answer_names_no_credential_or_provider_detail(report):
    answer = answer_report(report, "解释一下", accepting_model(["facility:not-in-report"]))

    assert answer.mode == "report_facts"
    assert "not-in-report" not in answer.answer
    assert "未通过证据或来源校验" in answer.answer


# --- report_facts fallback --------------------------------------------------

def test_report_facts_matches_category_by_name_and_by_id(report):
    by_name = answer_report(report, "婴幼儿托育有多少个？", None)
    by_id = answer_report(report, f"{REACHABLE_FACILITY} 情况如何？", None)

    assert by_name.mode == by_id.mode == "report_facts"
    assert by_name.evidence_ids == [f"facility:{REACHABLE_FACILITY}"]
    assert by_id.evidence_ids == [f"facility:{REACHABLE_FACILITY}"]
    assert "未启用模型解读" in by_name.answer


def test_report_facts_matches_inside_a_longer_question(report):
    answer = answer_report(report, "我们小区附近的幼儿园情况怎么样，够用吗？", None)

    assert answer.evidence_ids == [f"facility:{EMPTY_FACILITY}"]


def test_report_facts_does_not_invent_an_answer_for_an_unknown_category(report):
    answer = answer_report(report, "请介绍一下量子传送站的情况。", None)

    assert answer.evidence_ids == ["report"]
    assert "量子传送站" not in answer.answer
    assert report.summary in answer.answer
    assert report.score_method in answer.answer
    assert "不能由本报告直接证明" in answer.answer


def test_report_facts_exposes_the_null_count_not_a_fake_zero(report):
    answer = answer_report(report, "小学够不够？", None)

    assert answer.evidence_ids == [f"facility:{PARTIAL_FACILITY}"]
    assert "未知（已知 2 个）" in answer.answer
    assert "本次圈内检索数 0" not in answer.answer


def test_report_facts_reports_a_confirmed_zero_as_zero(report):
    answer = answer_report(report, "幼儿园有几个？", None)

    assert answer.evidence_ids == [f"facility:{EMPTY_FACILITY}"]
    assert "本次圈内检索数 0" in answer.answer


def test_report_facts_group_question_expands_to_that_group(report):
    answer = answer_report(report, "养老与助残这一组怎么样？", None)

    group_id = next(g["id"] for g in load_catalog()["groups"] if g["name_zh"] == "养老与助残")
    members = {c.facility_id for c in report.categories if c.group_id == group_id}
    referenced = {e.split(":", 1)[1] for e in answer.evidence_ids if e.startswith("facility:")}
    assert referenced == members
    assert len(referenced) >= 1


def test_report_facts_keeps_point_reachability_separate_from_coverage(report):
    answer = answer_report(report, f"{SERVICE_AREA_FACILITY} 覆盖得怎么样？", None)

    assert answer.evidence_ids == [f"facility:{SERVICE_AREA_FACILITY}"]
    # A reachable point is reported, and the answer must not turn it into full coverage.
    assert "已确认中心出发15分钟可达 1 个" in answer.answer
    assert "覆盖率" not in answer.answer


def test_report_facts_requires_a_real_question_text():
    from app.schemas.checkup import ReportQuestion

    for question in ("", "   ", "x" * 2001):
        with pytest.raises(Exception):
            ReportQuestion(question=question)


def test_report_facts_unknown_area_is_not_described_as_blind(report):
    answer = answer_report(report, "公园与公共绿地这块是全盲区吗？", None)

    assert answer.evidence_ids == [f"facility:{FACILITY_AREA_FACILITY}"]
    assert "盲区" not in answer.answer


def test_report_facts_ignores_catalog_aliases(report):
    """Documented gap: matching uses name_zh / facility_id only, not catalog aliases.

    The catalog ships aliases such as ``公园`` for ``公园与公共绿地``. Until the
    shared question contract adds alias matching, a question phrased with an alias
    falls back to the report summary instead of asserting a wrong statistic. The
    fallback direction is safe, so this test pins the current behaviour rather than
    a desired one, and must be revisited when aliases are supported.
    """
    catalog = load_catalog()
    aliased = next(f for f in catalog["facilities"] if f["id"] == FACILITY_AREA_FACILITY)
    answer = answer_report(report, f"{aliased['aliases'][1]}这块是全盲区吗？", None)

    assert answer.evidence_ids == ["report"]
    assert report.summary in answer.answer


# --- web research: planner, bounded search, page reads, truthful status ------


class _FakeSearch:
    def __init__(self, results=None, error=None, page=None, fetch_error=None):
        self.results = results or []
        self.error = error
        self.page = page
        self.fetch_error = fetch_error
        self.queries = []
        self.fetched = []

    def search(self, query, *, count=5, need_content=False):
        self.queries.append((query, count, need_content))
        if self.error:
            raise self.error
        return self.results

    def fetch(self, url):
        self.fetched.append(url)
        if self.fetch_error:
            raise self.fetch_error
        return self.page or {"title": "页面标题", "content": "页面正文"}


class _Planner:
    def __init__(self, needs_web, query="上海 镇坪路 小学 特色"):
        self.needs_web = needs_web
        self.query = query
        self.calls = []

    def generate_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"needs_web": self.needs_web, "query": self.query, "reason": "test"}


def _web_model(payload=None):
    return RecordingModel(payload or {
        "answer": "先给结论，再给依据。",
        "evidence_ids": ["report", "web"],
        "mode": "model",
        "web_search_used": True,
        "sources": [{
            "title": "上海15分钟社区生活圈行动方案",
            "url": "https://example.gov.cn/plan",
            "published_at": "2026-09-01T00:00:00+00:00",
        }],
    })


def _result(url="https://example.gov.cn/plan", title="上海15分钟社区生活圈行动方案"):
    return [{
        "title": title,
        "url": url,
        "summary": "官方公开的行动方案摘要。",
        "date": "2026-09-01T00:00:00+00:00",
        "site_name": "example.gov.cn",
        "authority": 3,
    }]


def test_web_search_sources_and_page_content_reach_the_model(report):
    search = _FakeSearch(_result(), page={"title": "行动方案", "content": "正文提到社区服务设施配置。"})
    model = _web_model()
    answer = answer_report(report, "最新的15分钟生活圈政策怎么说？", model, search,
                           planner=_Planner(True))

    assert answer.mode == "model"
    assert answer.web_search_used is True
    assert [source.url for source in answer.sources] == ["https://example.gov.cn/plan"]
    assert answer.retrieval.status == "searched"
    assert answer.retrieval.requested is True
    assert answer.retrieval.pages_read == 1
    assert answer.retrieval.attempts == 2
    evidence = model.last_user_message["evidence"]
    assert evidence["web"]["sources"][0]["url"] == "https://example.gov.cn/plan"
    assert evidence["web"]["pages"][0]["content"].startswith("正文提到")
    assert search.fetched == ["https://example.gov.cn/plan"]


def test_planner_query_is_used_and_source_metadata_comes_from_the_search(report):
    search = _FakeSearch(_result())
    model = _web_model({"answer": "回答。", "evidence_ids": ["report", "web"], "mode": "model",
                        "web_search_used": True,
                        "sources": [{"title": "编造标题", "url": "https://example.gov.cn/plan"}]})
    answer = answer_report(report, "最新的小学政策？", model, search,
                           planner=_Planner(True, "上海 镇坪路 小学 政策"))

    assert search.queries and search.queries[0][0] == "上海 镇坪路 小学 政策"
    assert answer.retrieval.query == "上海 镇坪路 小学 政策"
    assert [source.title for source in answer.sources] == ["上海15分钟社区生活圈行动方案"]


def test_planner_can_skip_the_search_even_when_the_question_has_a_keyword(report):
    search = _FakeSearch(_result())
    model = accepting_model(["report"])
    answer = answer_report(report, "最新政策是什么？", model, search, planner=_Planner(False))

    assert search.queries == []
    assert answer.retrieval.status == "not_requested"
    assert answer.web_search_used is False
    assert "web" not in model.last_user_message["evidence"]


def test_auto_mode_without_a_search_client_never_claims_web(report):
    model = accepting_model(["report"])
    answer = answer_report(report, "最新的政策是什么？", model, None)

    assert answer.retrieval.requested is False
    assert answer.retrieval.status == "not_requested"
    assert answer.web_search_used is False


def test_report_mode_never_calls_the_search_client(report):
    search = _FakeSearch(_result())
    model = accepting_model(["report"])
    answer = answer_report(report, "最新的政策是什么？", model, search, mode="report")

    assert search.queries == []
    assert answer.retrieval.status == "not_requested"


def test_web_mode_without_a_configured_search_reports_failure(report):
    model = accepting_model(["report"])
    answer = answer_report(report, "上网查一下这所学校", model, None, mode="web")

    assert answer.retrieval.requested is True
    assert answer.retrieval.status == "failed"
    assert "未启用联网检索" in answer.retrieval.error
    assert answer.web_search_used is False and answer.sources == []


def test_search_failure_is_visible_in_retrieval_and_never_fakes_sources(report):
    search = _FakeSearch(error=RuntimeError("network down"))
    model = accepting_model(["report"])
    answer = answer_report(report, "最新政策是什么？", model, search, planner=_Planner(True))

    assert answer.web_search_used is False
    assert answer.sources == []
    assert answer.retrieval.status == "failed"
    assert "network down" in answer.retrieval.error
    assert "web" not in model.last_user_message["evidence"]


def test_no_results_has_its_own_status(report):
    search = _FakeSearch([])
    model = accepting_model(["report"])
    answer = answer_report(report, "最新政策是什么？", model, search, planner=_Planner(True))

    assert answer.retrieval.status == "no_results"
    assert answer.retrieval.error is None
    assert answer.web_search_used is False


def test_fabricated_sources_are_dropped_with_the_unsupported_answer(report):
    search = _FakeSearch(_result())
    model = RecordingModel({
        "answer": "公开信息。",
        "evidence_ids": ["report", "web"],
        "mode": "model",
        "web_search_used": True,
        "sources": [{"title": "编造", "url": "https://fake.example/url"}],
    })
    answer = answer_report(report, "最新公开信息是什么？", model, search, planner=_Planner(True))

    assert answer.mode == "report_facts"
    assert answer.web_search_used is False
    assert answer.sources == []
    assert "未通过证据或来源校验" in answer.answer
    assert answer.retrieval.status == "searched"


def test_planner_failure_falls_back_to_the_keyword_heuristic(report):
    class BrokenPlanner:
        def generate_object(self, **_kwargs):
            raise TimeoutError("planner timeout")

    search = _FakeSearch(_result())
    model = _web_model()
    answer = answer_report(report, "最新的政策是什么？", model, search, planner=BrokenPlanner())

    assert search.queries  # the keyword heuristic still asked for public information
    assert answer.retrieval.status == "searched"


# --- follow-up context and focus --------------------------------------------


def test_history_is_forwarded_to_planner_and_model(report):
    history = [
        {"role": "user", "content": "报告里小学多少分？"},
        {"role": "assistant", "content": "报告里小学是90分。"},
    ]
    model = accepting_model(["report"])
    planner = _Planner(False)
    answer_report(report, "那它够不够？", model, _FakeSearch(_result()), history=history, planner=planner)

    assert model.last_user_message["history"] == history
    planner_message = json.loads(planner.calls[0]["user_message"])
    assert planner_message["recent_turns"] == history


def test_focus_allows_the_related_point_and_route_evidence(report):
    poi = report.pois[0]
    model = RecordingModel({
        "answer": "这个点位步行约9分钟。",
        "evidence_ids": [f"poi:{poi.poi_id}", f"route:{poi.poi_id}", "report"],
        "mode": "model",
    })
    answer = answer_report(report, "这个点位怎么样？", model, None,
                           focus=ReportFocus(poi_id=poi.poi_id))
    evidence = model.last_user_message["evidence"]

    assert answer.mode == "model"
    assert answer.focus_applied.poi_id == poi.poi_id
    assert f"poi:{poi.poi_id}" in evidence and f"route:{poi.poi_id}" in evidence
    assert add_other_poi(report) not in json.dumps(evidence, ensure_ascii=False)


def add_other_poi(report):
    return report.pois[1].poi_id


def test_focus_facility_adds_only_that_facilitys_points(report):
    model = RecordingModel({
        "answer": "该类别最近点位约9分钟。",
        "evidence_ids": [f"poi:{report.pois[0].poi_id}", "report"],
        "mode": "model",
    })
    answer_report(report, "这一类最近的点在哪？", model, None,
                  focus=ReportFocus(facility_id=report.pois[0].facility_id))
    serialized = model.last_user_message["evidence"]

    assert f"poi:{report.pois[0].poi_id}" in serialized
    assert report.pois[1].poi_id not in serialized


def test_a_question_naming_a_point_can_cite_it_without_focus(report):
    poi = report.pois[0]
    model = RecordingModel({
        "answer": "这个点位步行约9分钟。",
        "evidence_ids": [f"poi:{poi.poi_id}"],
        "mode": "model",
    })
    answer = answer_report(report, f"{poi.name}怎么样？", model, None)

    assert answer.mode == "model"
    assert answer.evidence_ids == [f"poi:{poi.poi_id}"]


def test_unknown_focus_ids_are_ignored_defensively(report):
    model = accepting_model(["report"])
    answer = answer_report(report, "解释一下", model, None,
                           focus=ReportFocus(facility_id="not-in-report"))

    assert answer.mode == "model"
    assert answer.focus_applied is None


# --- request/response schema --------------------------------------------------


def test_request_schema_defaults_and_bounds():
    ok = ReportQuestion(question="正常问题")
    assert ok.mode == "auto" and ok.history == [] and ok.focus is None

    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", mode="model")
    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", history=[{"role": "system", "content": "x"}])
    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", history=[{"role": "user", "content": "x"}] * 13)
    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", history=[{"role": "user", "content": "x" * 4001}])
    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", history=[{"role": "user", "content": "x" * 1100}] * 12)
    with pytest.raises(Exception):
        ReportQuestion(question="正常问题", extra=1)


def test_retrieval_info_rejects_inconsistent_states():
    assert RetrievalInfo(requested=False, status="not_requested").status == "not_requested"

    with pytest.raises(Exception):
        RetrievalInfo(requested=False, status="searched", attempts=1)
    with pytest.raises(Exception):
        RetrievalInfo(requested=True, status="failed")
    with pytest.raises(Exception):
        RetrievalInfo(requested=True, status="not_requested", error="不该有原因")
    with pytest.raises(Exception):
        RetrievalInfo(requested=True, status="searched", attempts=0)


def test_report_facts_mode_still_lists_web_sources_without_a_model(report):
    search = _FakeSearch(_result())
    answer = answer_report(report, "最新政策是什么？", None, search)

    assert answer.mode == "report_facts"
    assert answer.web_search_used is True
    assert answer.sources and answer.sources[0].url == "https://example.gov.cn/plan"
    assert answer.retrieval.status == "searched"


def test_location_hint_is_derived_from_saved_addresses_not_a_geocoder(report):
    from app.modules.checkup.questions import _location_hint

    poi = report.pois[0].model_copy(update={"address": "上海市杨浦区彰武路1号"})
    localized = report.model_copy(update={"pois": [poi, *report.pois[1:]]})

    assert _location_hint(localized) == "上海市 杨浦区"
    # A report without usable addresses falls back to its own label, never a guess.
    assert _location_hint(report) == report.location_label


def test_location_hint_prefers_the_point_named_in_the_question(report):
    from app.modules.checkup.questions import _location_hint

    named = report.pois[0].model_copy(update={"address": "上海市杨浦区彰武路1号"})
    other = report.pois[1].model_copy(update={"address": "上海市虹口区赤峰路1号"})
    localized = report.model_copy(update={"pois": [other, named]})

    # Without a named point the first saved address provides the district …
    assert _location_hint(localized, "随便问问") == "上海市 虹口区"
    # … but a question naming a specific point must use that point's district.
    assert _location_hint(localized, f"{named.name}的招生政策") == "上海市 杨浦区"
