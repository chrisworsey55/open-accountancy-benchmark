"""Pydantic input and result contracts for the WP-07 registry."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    model_validator,
)

from mirrorfirm.core.models import (
    ApprovalActionDescriptor,
    ApprovalKind,
    IrqItem,
    JournalLine,
    Role,
    TaxTag,
    WorkpaperBody,
)

from .errors import ToolError


class ToolInput(BaseModel):
    """Base configuration shared by all public tool input schemas."""

    model_config = ConfigDict(extra="forbid")


class EmptyInput(ToolInput):
    """Input schema for tools which accept no arguments."""


class GetTaskInput(ToolInput):
    task_id: str


class GetClientInput(ToolInput):
    client_id: str


class ReadThreadInput(ToolInput):
    thread_id: str


class DraftMessageInput(ToolInput):
    draft_message_id: str


class FinalizeWorkpaperInput(ToolInput):
    workpaper_id: str


class TaskListInput(ToolInput):
    status: (
        Literal["open", "in_progress", "blocked", "ready_for_review", "done"] | None
    ) = None


class DocumentListInput(ToolInput):
    kind: str | None = None


class ReadDocumentInput(ToolInput):
    doc_id: str
    page_range: str | None = None


class ReadTableInput(ToolInput):
    doc_id: str
    sheet: str | None = None
    range: str | None = None
    header_row: int | None = None


class SearchDocumentsInput(ToolInput):
    query: str = Field(min_length=1)
    kind: str | None = None


class BankTransactionsInput(ToolInput):
    bank_account_id: str
    period_id: str | None = None
    status: Literal["unclassified", "proposed", "classified"] | None = None


class LedgerQueryInput(ToolInput):
    account: str | None = None
    period: str | None = None
    status: Literal["proposed", "approved", "posted", "rejected"] | None = None
    source: (
        Literal["opening", "feed_classification", "proposal", "adjustment"] | None
    ) = None


class PeriodInput(ToolInput):
    period_id: str


class ReconciliationInput(ToolInput):
    bank_account_id: str
    period_id: str


class AggregateInput(ToolInput):
    source: str
    filter: list[dict[str, JsonValue]] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[dict[str, JsonValue]] = Field(default_factory=list)


class CalculateInput(ToolInput):
    expression: str = Field(min_length=1)
    bindings: dict[str, JsonValue] = Field(default_factory=dict)


class CompareDatasetsInput(ToolInput):
    left: JsonValue
    right: JsonValue
    keys: list[str] = Field(min_length=1)
    compare_fields: list[str] = Field(default_factory=list)
    tolerance_minor: int = 0


class ClassificationSplit(ToolInput):
    account_id: str
    gross_minor: PositiveInt
    tax: TaxTag | None = None


class ClassificationItem(ToolInput):
    bank_transaction_id: str
    splits: list[ClassificationSplit] | None = None
    account_id: str | None = None
    tax: TaxTag | None = None
    provenance_refs: list[str] = Field(min_length=1)
    note: str | None = None

    @model_validator(mode="after")
    def require_one_split_form(self) -> ClassificationItem:
        if self.splits is None and self.account_id is None:
            raise ValueError("splits or account_id is required")
        if self.splits is not None and self.account_id is not None:
            raise ValueError("use splits or account_id, not both")
        return self


class ProposeClassificationInput(ToolInput):
    items: list[ClassificationItem] = Field(min_length=1)


class ProposeJournalInput(ToolInput):
    date: date
    memo: str = Field(min_length=1)
    lines: list[JournalLine] = Field(min_length=2)
    provenance_refs: list[str] = Field(min_length=1)


class RequestApprovalInput(ToolInput):
    kind: ApprovalKind
    action_descriptor: ApprovalActionDescriptor
    rationale: str = Field(min_length=1)
    provenance_refs: list[str] = Field(min_length=1)


class DraftInformationRequestInput(ToolInput):
    client_id: str
    items: list[IrqItem] = Field(min_length=1)
    body: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)


class UpdateDraftInput(ToolInput):
    message_id: str
    body: str | None = None
    items: list[IrqItem] | None = None
    attachments: list[str] | None = None


class DraftReplyInput(ToolInput):
    thread_id: str
    body: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)


class CreateWorkpaperInput(ToolInput):
    task_id: str
    body: WorkpaperBody


class UpdateWorkpaperInput(ToolInput):
    workpaper_id: str
    body: WorkpaperBody


class UpdateTaskStatusInput(ToolInput):
    task_id: str
    status: Literal["open", "in_progress", "blocked", "ready_for_review", "done"]
    blocked_on: str | None = None


class SubmitForReviewInput(ToolInput):
    task_id: str
    workpaper_ids: list[str]
    summary: str = Field(min_length=1)


class EscalateInput(ToolInput):
    to_role: Role
    subject_refs: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class AdvanceTimeInput(ToolInput):
    until: datetime | None = None
    minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_exactly_one_target(self) -> AdvanceTimeInput:
        if (self.until is None) == (self.minutes is None):
            raise ValueError("provide exactly one of until or minutes")
        return self


class FinishEpisodeInput(ToolInput):
    summary: str = Field(min_length=1)
    deliverable_refs: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)


class ToolCallResult(BaseModel):
    """Uniform result wrapper exposed through Python and MCP transports."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    result: JsonValue | None = None
    error: ToolError | None = None
    action_id: str | None = None
