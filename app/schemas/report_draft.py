"""P0 draft is separate from the immutable spatial analysis contract."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, BaseModel, ConfigDict, model_validator

class DraftRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['generate', 'revise']
    expected_version: int = Field(ge=0)
    instruction: str = Field(default='', max_length=2000)
    context: str = Field(default='', max_length=4000)
    session_id: UUID | None = None

    @model_validator(mode='after')
    def revision_needs_instruction(self):
        if self.action == 'revise' and not self.instruction.strip():
            raise ValueError('请输入修改要求。')
        return self

class DraftExport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1)
    session_id: UUID | None = None

class DraftSection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Literal['findings', 'planning', 'limitations']
    paragraphs: list[Annotated[str, Field(min_length=1, max_length=1600)]] | None = Field(default=None, min_length=1, max_length=16)
    remove: bool = False

    @model_validator(mode='after')
    def one_change(self):
        if self.remove == (self.paragraphs is not None):
            raise ValueError('章节需提供替换正文，或仅标记删除。')
        return self

class DraftText(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sections: list[DraftSection] = Field(min_length=1, max_length=3)

class DraftChapter(BaseModel):
    id: str
    title: str
    paragraphs: list[str]
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)

class ReportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    report_id: str
    source_hash: str
    version: int = Field(ge=1)
    title: str
    mode: Literal['model', 'report_facts']
    notice: str
    updated_at: str
    facts: dict
    chapters: list[DraftChapter]
    editorial_state: dict[str, dict] = Field(default_factory=dict)
