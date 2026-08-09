"""Pydantic input and result contracts for the WP-07 registry."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    model_validator,
)

from mirrorfirm.core.models import (
    Approval,
    ApprovalActionDescriptor,
    ApprovalKind,
    BankTransaction,
    Client,
    Document,
    Engagement,
    InformationRequest,
    IrqItem,
    Journal,
    JournalLine,
    Message,
    Person,
    Practice,
    ReviewNote,
    Role,
    StateSnapshot,
    Task,
    TaxTag,
    Thread,
    Workpaper,
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
    """Explicit task selection and a typed §E.14 workpaper body."""

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


JsonObject: TypeAlias = dict[str, JsonValue]


class EventFiredOutput(BaseModel):
    """One scheduled event surfaced by a tool result."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    kind: str
    surface_refs: list[str]


class ToolOutput(BaseModel):
    """Shared metadata that may accompany any concrete stable tool output."""

    model_config = ConfigDict(extra="forbid")

    fired_events: list[EventFiredOutput] | None = None


class GetContextOutput(ToolOutput):
    practice: Practice
    engagement: Engagement
    client: Client
    actor: Person


class GetCurrentTimeOutput(ToolOutput):
    world_time: str
    local_time: str
    next_event_exists: bool


class ListTasksOutput(ToolOutput):
    tasks: list[Task]


class GetTaskOutput(ToolOutput):
    task: Task


class ListClientsOutput(ToolOutput):
    clients: list[Client]


class GetClientOutput(ToolOutput):
    client: Client


class ListDocumentsOutput(ToolOutput):
    documents: list[Document]


class ReadDocumentOutput(ToolOutput):
    document: Document
    content: str


class ReadTableOutput(ToolOutput):
    table_ref: str
    rows: list[dict[str, str]]


class DocumentSearchMatch(BaseModel):
    """A document hit with the bounded source excerpt returned by search."""

    model_config = ConfigDict(extra="forbid")

    document: Document
    snippet: str


class SearchDocumentsOutput(ToolOutput):
    matches: list[DocumentSearchMatch]


class ListBankTransactionsOutput(ToolOutput):
    transactions: list[BankTransaction]


class LedgerLineOutput(BaseModel):
    """One flat, provenance-bearing ledger line exposed by ``query_ledger``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"
    journal_id: str
    line_index: int
    date: str
    status: Literal["proposed", "approved", "posted", "rejected"]
    source: Literal["opening", "feed_classification", "proposal", "adjustment"]
    account_id: str
    direction: Literal["dr", "cr"]
    amount_minor: int
    currency: str
    bank_transaction_id: str | None = None
    tax: TaxTag | None = None
    source_ref: str


class QueryLedgerOutput(ToolOutput):
    lines: list[LedgerLineOutput]


class TrialBalanceOutput(ToolOutput):
    period_id: str
    balances: dict[str, int]


class ListThreadsOutput(ToolOutput):
    threads: list[Thread]


class ReadThreadOutput(ToolOutput):
    thread: Thread
    messages: list[Message]


class GetReconciliationStatusOutput(ToolOutput):
    workpapers: list[Workpaper]


class ListApprovalsOutput(ToolOutput):
    approvals: list[Approval]


class ListReviewNotesOutput(ToolOutput):
    review_notes: list[ReviewNote]


class AggregateTableOutput(ToolOutput):
    rows: list[JsonObject]


class CalculateOutput(ToolOutput):
    value: int | float | str
    calculation_action_id: str


class DatasetComparison(BaseModel):
    """One keyed comparison with explicitly named source rows and differences."""

    model_config = ConfigDict(extra="forbid")

    key: list[JsonValue]
    left: JsonObject
    right: JsonObject
    left_ref: str
    right_ref: str
    different_fields: list[str]


class CompareDatasetsOutput(ToolOutput):
    matched: list[DatasetComparison]
    left_only: list[JsonObject]
    right_only: list[JsonObject]
    mismatched: list[DatasetComparison]


class ProposeClassificationOutput(ToolOutput):
    journal_ids: list[str]
    transaction_ids: list[str]


class ProposeJournalOutput(ToolOutput):
    journal: Journal


class RequestApprovalOutput(ToolOutput):
    approval: Approval


class DraftInformationRequestOutput(ToolOutput):
    thread: Thread
    message: Message
    information_request: InformationRequest


class UpdateDraftOutput(ToolOutput):
    message: Message
    information_request: InformationRequest | None


class SendInformationRequestOutput(ToolOutput):
    message: Message
    information_request: InformationRequest


class DraftReplyOutput(ToolOutput):
    message: Message


class SendReplyOutput(ToolOutput):
    message: Message


class CreateWorkpaperOutput(ToolOutput):
    workpaper: Workpaper


class UpdateWorkpaperOutput(ToolOutput):
    workpaper: Workpaper


class FinalizeWorkpaperOutput(ToolOutput):
    workpaper: Workpaper


class UpdateTaskStatusOutput(ToolOutput):
    task: Task


class SubmitForReviewOutput(ToolOutput):
    task: Task
    summary: str


class EscalateOutput(ToolOutput):
    review_note: ReviewNote
    subject_refs: list[str]


class AdvanceTimeOutput(ToolOutput):
    world_time: str
    local_time: str
    fired_events: list[EventFiredOutput]
    stopped_early: bool


class FinishEpisodeOutput(ToolOutput):
    summary: str
    deliverable_refs: list[str]
    unresolved_items: list[str]
    final_snapshot_id: str
    final_snapshot: StateSnapshot
    state_digest: str


TOOL_OUTPUT_MODELS: dict[str, type[ToolOutput]] = {
    "get_context": GetContextOutput,
    "get_current_time": GetCurrentTimeOutput,
    "list_tasks": ListTasksOutput,
    "get_task": GetTaskOutput,
    "list_clients": ListClientsOutput,
    "get_client": GetClientOutput,
    "list_documents": ListDocumentsOutput,
    "read_document": ReadDocumentOutput,
    "read_table": ReadTableOutput,
    "search_documents": SearchDocumentsOutput,
    "list_bank_transactions": ListBankTransactionsOutput,
    "query_ledger": QueryLedgerOutput,
    "trial_balance": TrialBalanceOutput,
    "list_threads": ListThreadsOutput,
    "read_thread": ReadThreadOutput,
    "get_reconciliation_status": GetReconciliationStatusOutput,
    "list_approvals": ListApprovalsOutput,
    "list_review_notes": ListReviewNotesOutput,
    "aggregate_table": AggregateTableOutput,
    "calculate": CalculateOutput,
    "compare_datasets": CompareDatasetsOutput,
    "propose_classification": ProposeClassificationOutput,
    "propose_journal": ProposeJournalOutput,
    "request_approval": RequestApprovalOutput,
    "draft_information_request": DraftInformationRequestOutput,
    "update_draft": UpdateDraftOutput,
    "send_information_request": SendInformationRequestOutput,
    "draft_reply": DraftReplyOutput,
    "send_reply": SendReplyOutput,
    "create_workpaper": CreateWorkpaperOutput,
    "update_workpaper": UpdateWorkpaperOutput,
    "finalize_workpaper": FinalizeWorkpaperOutput,
    "update_task_status": UpdateTaskStatusOutput,
    "submit_for_review": SubmitForReviewOutput,
    "escalate": EscalateOutput,
    "advance_time": AdvanceTimeOutput,
    "finish_episode": FinishEpisodeOutput,
}


class ToolCallResult(BaseModel):
    """Engine execution envelope; registry models define public tool outputs."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    result: dict[str, object] | None = None
    error: ToolError | None = None
    action_id: str | None = None
