"""Additive point-checkup contract; the six original tool contracts stay unchanged."""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import CenterPoint, ContractModel, ErrorDetail, WarningItem
from app.schemas.isochrone import IsochroneData
from app.schemas.location import LocationInput
from app.schemas.routing import RouteResult


class CheckupRequest(ContractModel):
    location: LocationInput
    facility_ids: list[str] | None = Field(default=None, max_length=45)

    @model_validator(mode="after")
    def facility_selection_is_usable(self) -> "CheckupRequest":
        if self.facility_ids is None:
            return self
        if not self.facility_ids:
            raise ValueError("至少选择一个设施类别；不选类别不能提交分析。")
        if len(set(self.facility_ids)) != len(self.facility_ids):
            raise ValueError("设施类别不能重复选择。")
        if any(not item or not item.strip() for item in self.facility_ids):
            raise ValueError("设施类别编号不能为空。")
        return self


class CheckupPOI(ContractModel):
    poi_id: str
    name: str
    facility_id: str
    location: CenterPoint
    address: str | None = None
    in_isochrone: bool


class CategoryAssessment(ContractModel):
    facility_id: str
    name_zh: str
    group_id: str
    search_status: Literal["complete", "partial", "failed"]
    count: int | None = Field(ge=0)
    known_count: int = Field(ge=0)
    reachable_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    nearest_duration_s: int | None = Field(ge=0)
    nearest_distance_m: int | None = Field(ge=0)
    access_status: Literal["reachable", "not_found", "unreachable", "unverified", "unknown"]
    score: float | None = Field(ge=0, le=100)
    score_provisional: bool = False
    score_profile: Literal["A", "B", "C"] | None = None
    score_basis_count: int | None = Field(default=None, ge=0)
    score_band: str | None = None
    next_band: str | None = None
    covered_ratio: float | None = Field(ge=0, le=1)
    blind_ratio: float | None = Field(ge=0, le=1)
    unknown_ratio: float | None = Field(ge=0, le=1)
    data_notes: str

    @model_validator(mode="after")
    def consistent_evidence(self):
        if (self.search_status == "complete") != (self.count is not None):
            raise ValueError("Only complete searches have a known count")
        if self.count is not None and self.count != self.known_count:
            raise ValueError("Count must match observed in-domain POIs")
        if self.reachable_count + self.unresolved_count > self.known_count:
            raise ValueError("Route counts exceed in-domain POIs")
        if self.score is None:
            if self.score_basis_count is not None:
                raise ValueError("A missing score must not carry a count basis")
        elif self.score_profile is not None:
            if self.score_basis_count is None:
                raise ValueError("A scored category needs a profile and count basis")
            if self.score_provisional:
                if self.count is not None or self.score_basis_count != self.known_count:
                    raise ValueError("Provisional scores use the known count of an incomplete search")
            elif self.count is None or self.score_basis_count != self.count:
                raise ValueError("Complete scores use the complete search count")
        ratios = (self.covered_ratio, self.blind_ratio, self.unknown_ratio)
        if any(v is not None for v in ratios) and (
            any(v is None for v in ratios) or abs(sum(ratios) - 1) > 1e-6
        ):
            raise ValueError("Coverage ratios must be all absent or sum to one")
        return self


class GroupAssessment(ContractModel):
    id: str
    name_zh: str
    score: float | None = Field(ge=0, le=100)
    score_provisional: bool = False
    assessed_categories: int = Field(ge=0)
    total_categories: int = Field(ge=1)
    summary: str


class CheckupReport(ContractModel):
    report_id: str
    created_at: str
    center: CenterPoint
    location_label: str
    catalog_version: str
    isochrone: IsochroneData
    search_radius_m: int = Field(gt=0)
    categories: list[CategoryAssessment] = Field(min_length=1, max_length=45)
    groups: list[GroupAssessment] = Field(min_length=1, max_length=7)
    selected_facility_ids: list[str] = Field(default_factory=list)
    score: float | None = Field(ge=0, le=100)
    score_provisional: bool = False
    score_method: str
    summary: str
    pois: list[CheckupPOI]
    routes: list[RouteResult]
    blindspot: dict
    warnings: list[WarningItem]
    quality: dict

    @model_validator(mode="after")
    def complete_catalog(self):
        category_ids = [c.facility_id for c in self.categories]
        if len(set(category_ids)) != len(category_ids) or len(category_ids) > 45:
            raise ValueError("Report must contain at most 45 distinct categories")
        group_ids = [g.id for g in self.groups]
        if len(set(group_ids)) != len(group_ids) or len(group_ids) > 7:
            raise ValueError("Report contains duplicate or unknown groups")
        if {c.group_id for c in self.categories} != set(group_ids):
            raise ValueError("Unknown group")
        if self.selected_facility_ids and set(self.selected_facility_ids) != set(category_ids):
            raise ValueError("Selected facility ids must match the report categories")
        for score in [self.score, *(g.score for g in self.groups)]:
            if score is not None and abs(score / 2.5 - round(score / 2.5)) > 1e-8:
                raise ValueError("Scores use 2.5-point steps")
        return self


class CheckupJob(ContractModel):
    id: str
    status: Literal["queued", "running", "paused", "cancelled", "completed", "failed"]
    stage: str
    progress: int = Field(ge=0, le=100)
    result: CheckupReport | None = None
    error: ErrorDetail | None = None
    # Process results are separate from the immutable final-report contract.
    partial_result: dict | None = None
    partial_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def progress_belongs_to_this_task(self):
        if self.status == 'cancelled' and (self.stage != 'cancelled' or self.result is not None or self.error is not None):
            raise ValueError('Cancelled tasks have no final report or failure error')
        if self.partial_result is not None and (
            self.partial_result.get('report_id') != self.id
            or self.partial_revision < 1 or self.status == 'completed'
        ):
            raise ValueError('Process evidence must identify its active, failed or cancelled task')
        return self


class ReportQuestionTurn(ContractModel):
    """One bounded turn of the recent conversation, used only to resolve references."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ReportFocus(ContractModel):
    """What the user is currently looking at; every identifier must exist in the report."""

    facility_id: str | None = Field(default=None, min_length=1)
    group_id: str | None = Field(default=None, min_length=1)
    poi_id: str | None = Field(default=None, min_length=1)
    selected_facility_ids: list[str] | None = Field(default=None, max_length=45)


class ReportQuestion(ContractModel):
    question: str = Field(min_length=1, max_length=2000)
    # Additive v1.1 fields. An older client that only sends ``question`` keeps working.
    history: list[ReportQuestionTurn] = Field(default_factory=list, max_length=12)
    focus: ReportFocus | None = None
    mode: Literal["auto", "web", "report"] = "auto"

    @model_validator(mode="after")
    def bounded_context(self) -> "ReportQuestion":
        if sum(len(turn.content) for turn in self.history) > 12000:
            raise ValueError("对话历史过长，请只保留最近几轮。")
        return self


class RetrievalInfo(ContractModel):
    """Truthful record of what this question actually did on the network.

    ``requested`` is what the request asked for; ``status`` is what happened. A
    configured key alone never makes ``status`` become ``searched``.
    """

    requested: bool
    status: Literal["not_requested", "searched", "no_results", "failed"]
    query: str | None = Field(default=None, max_length=256)
    error: str | None = Field(default=None, max_length=500)
    pages_read: int = Field(default=0, ge=0, le=10)
    attempts: int = Field(default=0, ge=0, le=10)
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def status_matches_activity(self) -> "RetrievalInfo":
        if not self.requested and self.status != "not_requested":
            raise ValueError("未发起联网检索时状态必须是 not_requested")
        if self.requested and self.status == "searched" and self.attempts < 1:
            raise ValueError("已检索状态必须记录实际尝试次数")
        if self.status == "failed" and not self.error:
            raise ValueError("联网失败必须给出可读原因")
        if self.status != "failed" and self.error is not None:
            raise ValueError("非失败状态不能携带错误原因")
        return self


class ExternalSource(ContractModel):
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    published_at: str | None = None
    snippet: str | None = Field(default=None, max_length=1000)
    site_name: str | None = None
    authority: int | None = None


class ReportAnswer(ContractModel):
    answer: str = Field(min_length=1, max_length=12000)
    evidence_ids: list[str] = Field(max_length=53)
    mode: Literal["model", "report_facts"]
    web_search_used: bool = False
    sources: list[ExternalSource] = Field(default_factory=list)
    # Additive v1.1 fields: older servers omit them and older clients ignore them.
    retrieval: RetrievalInfo | None = None
    focus_applied: ReportFocus | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def sources_match_web_flag(self) -> "ReportAnswer":
        if self.sources and not self.web_search_used:
            raise ValueError("外部来源必须标记 web_search_used")
        return self
