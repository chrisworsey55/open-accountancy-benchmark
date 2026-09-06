"""Versioned, jurisdiction-neutral Pydantic domain schemas for Franklin & McGrath.

The field names in this module are the stable WP-02 public interface defined in
``SPEC.md`` §E.  Later packets add persistence and state-transition behaviour;
these models deliberately contain no database or tool-layer dependencies.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from ..digest import logical_state_digest

AmountMinor: TypeAlias = int
"""An amount represented in currency minor units."""

Role: TypeAlias = Literal[
    "client_contact",
    "bookkeeper",
    "staff_accountant",
    "reviewer",
    "partner",
    "admin",
    "agent",
]

ApprovalKind: TypeAlias = Literal[
    "post_journal",
    "post_to_closed_period",
    "send_external_message",
    "close_period",
]

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReferenceField:
    """Pydantic metadata marking a value as an identifier-bearing relationship.

    The marker deliberately has no JSON-schema rendering effect.  It lets internal
    contract consumers distinguish a relationship from otherwise free-form text
    without changing the stable public field shape.
    """


ReferenceString = Annotated[str, ReferenceField()]
ReferenceList = Annotated[list[str], ReferenceField()]
OptionalReferenceString = Annotated[str | None, ReferenceField()]
OptionalReferenceList = Annotated[list[str] | None, ReferenceField()]


def _validate_utc(value: datetime) -> datetime:
    """Require stored datetimes to be timezone-aware UTC values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be UTC")
    return value.astimezone(timezone.utc)


UTCAwareDatetime = Annotated[AwareDatetime, AfterValidator(_validate_utc)]


class VersionedModel(BaseModel):
    """Common configuration and version field shared by all public schemas."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = "0.1"


class WorldManifest(VersionedModel):
    """The authored world manifest stored in ``worlds/<id>/world.yaml``."""

    world_id: str
    world_version: str
    jurisdiction: Literal["uk", "us"]
    timezone: str
    title: str
    description: str
    seed: int
    start_world_time: UTCAwareDatetime
    practice_file: str
    client_files: list[str]
    events_file: str
    trap_register_file: str
    license: Literal["CC-BY-4.0"]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Ensure the manifest uses an IANA time-zone identifier."""

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value


class PracticePolicies(VersionedModel):
    """Practice-wide policy settings."""

    approval_required_for: list[ApprovalKind]
    outbound_comms: Literal["agent_may_send", "agent_drafts_only"]
    materiality_minor: int
    posting_to_closed_requires: Role = "reviewer"


class Practice(VersionedModel):
    """An accounting practice in a world."""

    id: str
    name: str
    jurisdiction: Literal["uk", "us"]
    people: list[str]
    policies: PracticePolicies


class Person(VersionedModel):
    """A person with a practice role."""

    id: str
    name: str
    role: Role
    client_id: str | None = None
    acting_as: Role | None = None

    @model_validator(mode="after")
    def validate_role_relationships(self) -> Person:
        """Enforce the role-dependent fields declared in §E.3."""

        if (self.role == "client_contact") != (self.client_id is not None):
            raise ValueError(
                "client_id must be set if and only if role is client_contact"
            )
        if (self.role == "agent") != (self.acting_as is not None):
            raise ValueError("acting_as must be set if and only if role is agent")
        return self


class UkTax(VersionedModel):
    """UK VAT registration details."""

    kind: Literal["uk"]
    vat_registered: bool
    vat_number: str | None
    vat_scheme: Literal["standard", "flat_rate", "none"]

    @model_validator(mode="after")
    def validate_registration_details(self) -> UkTax:
        """Keep VAT registration and the selected VAT scheme consistent."""

        if self.vat_registered:
            if self.vat_number is None:
                raise ValueError("vat_number is required for a VAT-registered entity")
            if self.vat_scheme == "none":
                raise ValueError(
                    "vat_scheme cannot be none for a VAT-registered entity"
                )
        elif self.vat_number is not None or self.vat_scheme != "none":
            raise ValueError(
                "unregistered entities require vat_number=None and vat_scheme=none"
            )
        return self


class UsTax(VersionedModel):
    """US sales-tax nexus details."""

    kind: Literal["us"]
    sales_tax_nexus: list[str]


TaxRegistration = Annotated[UkTax | UsTax, Field(discriminator="kind")]


class Entity(VersionedModel):
    """A client legal entity."""

    id: str
    legal_form: str
    basis: Literal["accrual", "cash"]
    currency: CurrencyCode
    tax: TaxRegistration


class Client(VersionedModel):
    """A practice client and its primary legal entity."""

    id: str
    name: str
    status: Literal["active", "onboarding", "disengaged"]
    entity: Entity
    contacts: list[str]


class Engagement(VersionedModel):
    """A scoped bookkeeping engagement."""

    id: str
    client_id: str
    scope: Literal["bookkeeping"]
    period_ids: list[str]
    status: Literal["active", "paused", "closed"]


class AccountingPeriod(VersionedModel):
    """An accounting period lifecycle record."""

    id: str
    entity_id: str
    start: date
    end: date
    status: Literal["open", "in_close", "closed", "locked"]

    @model_validator(mode="after")
    def validate_date_range(self) -> AccountingPeriod:
        """A period cannot end before it starts."""

        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self


class Account(VersionedModel):
    """A chart-of-accounts account."""

    id: str
    entity_id: str
    code: str
    name: str
    type: Literal["asset", "liability", "equity", "income", "expense"]
    tax_dimension: str | None
    active: bool


class TaxTag(VersionedModel):
    """A typed UK VAT classification tag.

    Jurisdiction packs validate the code and rate through their public pack hook;
    this core model remains deliberately jurisdiction-neutral.
    """

    kind: Literal["uk_vat"]
    code: str
    rate_bp: int


class JournalLine(VersionedModel):
    """One debit or credit in a journal."""

    account_id: str
    direction: Literal["dr", "cr"]
    amount_minor: PositiveInt
    currency: CurrencyCode
    bank_transaction_id: str | None = None
    tax: TaxTag | None = None


class Journal(VersionedModel):
    """A journal whose lines must balance per currency in minor units."""

    id: str
    entity_id: str
    date: date
    memo: str
    source: Literal["opening", "feed_classification", "proposal", "adjustment"]
    status: Literal["proposed", "approved", "posted", "rejected"]
    lines: list[JournalLine] = Field(min_length=2)
    proposed_by: str | None
    approval_id: str | None
    # A journal is owned by one bookkeeping engagement once it is authored.  Legacy
    # fixture journals can remain unassigned until a client has multiple engagements.
    engagement_id: str | None = None

    @model_validator(mode="after")
    def validate_balanced(self) -> Journal:
        """Implement invariant I-1 for every currency represented in the journal."""

        balances: dict[str, int] = {}
        for line in self.lines:
            signed_amount = (
                line.amount_minor if line.direction == "dr" else -line.amount_minor
            )
            balances[line.currency] = balances.get(line.currency, 0) + signed_amount

        unbalanced = {
            currency: amount for currency, amount in balances.items() if amount != 0
        }
        if unbalanced:
            rendered = ", ".join(
                f"{currency}={amount}"
                for currency, amount in sorted(unbalanced.items())
            )
            raise ValueError(f"journal lines must balance per currency: {rendered}")
        return self


class BankAccount(VersionedModel):
    """A bank account linked to its ledger control account."""

    id: str
    entity_id: str
    name: str
    ledger_account_id: str
    currency: CurrencyCode
    engagement_id: str | None = None


class BankTransaction(VersionedModel):
    """A single imported bank-feed transaction."""

    id: str
    bank_account_id: str
    date: date
    amount_minor: AmountMinor
    counterparty: str
    reference: str
    classification_status: Literal["unclassified", "proposed", "classified"]
    reconciliation_status: Literal["unmatched", "matched", "flagged"]


class Document(VersionedModel):
    """An immutable, SHA-256-addressed source document."""

    id: str
    sha256: Sha256
    filename: str
    mime: str
    kind: Literal[
        "invoice",
        "bill",
        "receipt",
        "statement",
        "engagement_letter",
        "payroll_export",
        "memo",
        "other",
    ]
    client_id: str | None
    source: Literal["fixture", "client_response"]
    received_world_time: UTCAwareDatetime
    engagement_id: str | None = None


class Thread(VersionedModel):
    """A client communication thread."""

    id: str
    client_id: str
    subject: str
    message_ids: list[str]
    engagement_id: str | None = None


class Message(VersionedModel):
    """An immutable-on-send message."""

    id: str
    thread_id: str
    sender: str
    recipients: list[str]
    status: Literal["draft", "sent"]
    world_time: UTCAwareDatetime | None
    body: str
    attachments: ReferenceList
    direction: Literal["inbound", "outbound"]

    @model_validator(mode="after")
    def validate_sent_time(self) -> Message:
        """Sent messages are timestamped, while drafts are not."""

        if (self.status == "sent") != (self.world_time is not None):
            raise ValueError("world_time must be set if and only if status is sent")
        return self


class IrqItem(VersionedModel):
    """One requested item in an information request."""

    description: str
    refs: ReferenceList


class InformationRequest(VersionedModel):
    """An information-request lifecycle record."""

    id: str
    client_id: str
    thread_id: str | None
    items: list[IrqItem]
    status: Literal["draft", "sent", "responded_partial", "responded", "closed"]
    engagement_id: str | None = None


class Task(VersionedModel):
    """A bookkeeping work item."""

    id: str
    engagement_id: str
    assignee: str
    title: str
    description: str
    due: date
    status: Literal["open", "in_progress", "blocked", "ready_for_review", "done"]
    blocked_on: OptionalReferenceString = None

    @model_validator(mode="after")
    def validate_blocked_reference(self) -> Task:
        """Invariant D.2 requires a reference whenever a task is blocked."""

        if self.status == "blocked" and self.blocked_on is None:
            raise ValueError("blocked tasks require blocked_on")
        return self


class EntityMatch(VersionedModel):
    """An AND-combined predicate used by events and state assertions."""

    client_id: str | None = None
    approval_kind: ApprovalKind | None = None
    status: str | None = None
    direction: str | None = None


class Offset(VersionedModel):
    """A deterministic, pack-calendar-aware event offset."""

    business_days: int = 0
    hours: int = 0
    minutes: int = 0


class AtTime(VersionedModel):
    """An event trigger at an absolute world time."""

    kind: Literal["at_time"]
    world_time: UTCAwareDatetime


class AfterEntity(VersionedModel):
    """An event trigger offset from the first matching entity."""

    kind: Literal["after_entity"]
    entity_kind: Literal[
        "information_request", "approval", "message", "workpaper", "task"
    ]
    match: EntityMatch
    offset: Offset


EventTrigger = Annotated[AtTime | AfterEntity, Field(discriminator="kind")]


class ClientReplyPayload(VersionedModel):
    """An event payload delivering a fictional client reply."""

    kind: Literal["client_reply"]
    thread_ref: str
    body: str
    attachment_fixture_refs: list[str]
    marks_irq: Literal["responded", "responded_partial"] | None = None


class NewBankFeedPayload(VersionedModel):
    """An event payload introducing bank-feed transactions."""

    kind: Literal["new_bank_feed"]
    bank_account_id: str
    txn_fixture_refs: list[str]


class ApprovalDecisionPayload(VersionedModel):
    """An event payload granting or rejecting a requested approval."""

    kind: Literal["approval_decision"]
    decision: Literal["granted", "rejected"]
    note: str


class ReviewerNotePayload(VersionedModel):
    """An event payload carrying a reviewer note and optional effect."""

    kind: Literal["reviewer_note"]
    target_ref: str
    note: str
    effect: Literal["none", "reopen_workpaper", "set_task_status"]
    effect_arg: str | None = None


class DeadlinePayload(VersionedModel):
    """An event payload announcing a deadline."""

    kind: Literal["deadline"]
    description: str
    task_id: str | None = None


EventPayload = Annotated[
    ClientReplyPayload
    | NewBankFeedPayload
    | ApprovalDecisionPayload
    | ReviewerNotePayload
    | DeadlinePayload,
    Field(discriminator="kind"),
]


class Event(VersionedModel):
    """A scheduled world event."""

    id: str
    # Fixtures written before engagement scoping may omit this field, but compiled
    # worlds must validate an explicit owner before events are executable.
    engagement_id: str | None = None
    trigger: EventTrigger
    payload: EventPayload
    fired: bool = False


class Budget(VersionedModel):
    """An episode's deterministic resource budget."""

    max_steps: int
    max_tokens: int
    max_world_days: int


class DeliverableSpec(VersionedModel):
    """A required episode deliverable."""

    kind: Literal["workpaper", "message", "summary"]
    workpaper_kind: str | None = None
    description: str
    acceptance: list[str]


class JudgeCriterion(VersionedModel):
    """A qualitative episode criterion."""

    id: str
    prompt: str
    target: Literal["outbound_messages", "escalations", "final_summary"]
    scale: Literal["binary", "one_to_five"]
    weight: float


class AccountBalance(VersionedModel):
    """An expected account balance assertion."""

    kind: Literal["account_balance"]
    account_id: str
    period_id: str
    expected_minor: AmountMinor
    tolerance_minor: AmountMinor = 0


class TxnStatus(VersionedModel):
    """An expected classification state for bank transactions."""

    kind: Literal["txn_status"]
    btx_ids: list[str]
    classification_status: Literal["unclassified", "proposed", "classified"]


class EntityCount(VersionedModel):
    """An expected number of matching entity records."""

    kind: Literal["entity_count"]
    entity_kind: str
    match: EntityMatch
    op: Literal["eq", "gte", "lte"]
    n: int


class WorkpaperState(VersionedModel):
    """An expected workpaper lifecycle state."""

    kind: Literal["workpaper_state"]
    workpaper_kind: str
    status: Literal["draft", "final"]
    must_tie: bool | None = None


class ApprovalRequested(VersionedModel):
    """An expected minimum number of approval requests."""

    kind: Literal["approval_requested"]
    approval_kind: ApprovalKind
    min_count: int


class TaskState(VersionedModel):
    """An expected task lifecycle state."""

    kind: Literal["task_state"]
    task_id: str
    status: Literal["open", "in_progress", "blocked", "ready_for_review", "done"]


class NoMutationOf(VersionedModel):
    """An assertion that named records remain unchanged."""

    kind: Literal["no_mutation_of"]
    entity_kind: str
    ids: list[str]


class UnresolvedFlagged(VersionedModel):
    """An assertion that unresolved items are made explicit."""

    kind: Literal["unresolved_flagged"]
    workpaper_kind: str
    min_items: int


StateAssertion = Annotated[
    AccountBalance
    | TxnStatus
    | EntityCount
    | WorkpaperState
    | ApprovalRequested
    | TaskState
    | NoMutationOf
    | UnresolvedFlagged,
    Field(discriminator="kind"),
]


class ExpectedState(VersionedModel):
    """A collection of deterministic expected-state assertions."""

    assertions: list[StateAssertion]


class DeterministicCriterion(VersionedModel):
    """A configuration record for a deterministic grader."""

    id: str
    grader_id: str
    params: dict[str, JsonValue]
    layer: Literal[
        "task_completion", "accounting", "state", "safety", "provenance", "efficiency"
    ]
    weight: float = 1.0


class ReferenceResult(VersionedModel):
    """The result metadata for a scripted reference trajectory."""

    reference_script: str
    final_state_digest: str
    note: Literal["reference-scripted-run; not model performance"]


class EpisodeManifest(VersionedModel):
    """An authored episode manifest."""

    episode_id: str
    title: str
    world_id: str
    world_version: str
    jurisdiction: Literal["uk", "us"]
    engagement_id: str
    agent_person_id: str
    instruction: str
    allowed_tools: list[str]
    event_ids: list[str]
    budget: Budget
    runs_for_reliability: int = 3
    deliverables: list[DeliverableSpec]
    deterministic_criteria: list[DeterministicCriterion]
    qualitative_criteria: list[JudgeCriterion]
    critical_failures_active: list[str]
    expected_state: ExpectedState
    reference: ReferenceResult
    commercial_rationale: str


class OutstandingItem(VersionedModel):
    """A reconciling item outstanding at the statement date."""

    ref: str
    amount_minor: AmountMinor
    reason: str
    provenance_refs: list[str]


class UnresolvedItem(VersionedModel):
    """A material item that has not been silently resolved."""

    description: str
    amount_minor: AmountMinor | None
    provenance_refs: list[str]


class BankReconWorkpaper(VersionedModel):
    """Typed body for a bank-reconciliation workpaper."""

    kind: Literal["bank_reconciliation"]
    bank_account_id: str
    period_id: str
    statement_end_minor: AmountMinor
    ledger_end_minor: AmountMinor
    outstanding: list[OutstandingItem]
    unresolved: list[UnresolvedItem]


class ClassificationRow(VersionedModel):
    """A classification row and its evidence provenance."""

    bank_transaction_id: str
    account_id: str
    tax: TaxTag | None
    provenance_refs: list[str]
    confidence: Literal["clear", "judgement", "needs_client"]


class ClassificationSummaryWorkpaper(VersionedModel):
    """Typed body for a classification-summary workpaper."""

    kind: Literal["classification_summary"]
    period_id: str
    rows: list[ClassificationRow]


class QueryLogWorkpaper(VersionedModel):
    """Typed body for a query-log workpaper."""

    kind: Literal["query_log"]
    items: list[UnresolvedItem]


WorkpaperBody = Annotated[
    BankReconWorkpaper | ClassificationSummaryWorkpaper | QueryLogWorkpaper,
    Field(discriminator="kind"),
]


class Workpaper(VersionedModel):
    """A typed workpaper whose finalisation time follows its lifecycle state."""

    id: str
    engagement_id: str
    task_id: str
    body: WorkpaperBody
    status: Literal["draft", "final"]
    created_by: str
    created_world_time: UTCAwareDatetime
    finalized_world_time: UTCAwareDatetime | None = None

    @model_validator(mode="after")
    def validate_finalized_time(self) -> Workpaper:
        """Final workpapers have a finalisation time; drafts do not."""

        if (self.status == "final") != (self.finalized_world_time is not None):
            raise ValueError(
                "finalized_world_time must be set if and only if status is final"
            )
        return self


class ReviewNote(VersionedModel):
    """A reviewer note and its optional addressed state."""

    id: str
    engagement_id: str
    target_ref: str
    author_id: str
    body: str
    created_world_time: UTCAwareDatetime
    status: Literal["open", "addressed"]
    addressed_by: str | None = None
    addressed_world_time: UTCAwareDatetime | None = None

    @model_validator(mode="after")
    def validate_addressed_fields(self) -> ReviewNote:
        """Addressed notes require both addressing fields; open notes require neither."""

        addressed_fields_present = (
            self.addressed_by is not None and self.addressed_world_time is not None
        )
        if (self.status == "addressed") != addressed_fields_present:
            raise ValueError(
                "addressed_by and addressed_world_time must be set if and only if "
                "status is addressed"
            )
        return self


class PostJournalDescriptor(VersionedModel):
    """Descriptor for a post-journal approval."""

    kind: Literal["post_journal"]
    journal_id: str


class PostToClosedPeriodDescriptor(VersionedModel):
    """Descriptor for a post-to-closed-period approval."""

    kind: Literal["post_to_closed_period"]
    journal_id: str
    period_id: str


class SendMessageDescriptor(VersionedModel):
    """Descriptor for a send-message approval."""

    kind: Literal["send_external_message"]
    draft_message_id: str


class ClosePeriodDescriptor(VersionedModel):
    """Descriptor for a close-period approval."""

    kind: Literal["close_period"]
    period_id: str


ApprovalActionDescriptor = Annotated[
    PostJournalDescriptor
    | PostToClosedPeriodDescriptor
    | SendMessageDescriptor
    | ClosePeriodDescriptor,
    Field(discriminator="kind"),
]


class Approval(VersionedModel):
    """A requested, granted, rejected, or expired approval."""

    id: str
    kind: ApprovalKind
    requested_by: str
    approver_role: Role
    action_descriptor: ApprovalActionDescriptor
    rationale: str
    provenance_refs: list[str]
    status: Literal["requested", "granted", "rejected", "expired"]
    # Send approvals bind a canonical digest of every message field which can affect
    # delivery.  Non-message approvals retain ``None`` for this optional field.
    approved_draft_digest: Sha256 | None = None
    engagement_id: str | None = None


class Mutation(VersionedModel):
    """A summary of one logical entity mutation caused by an action."""

    entity_kind: str
    entity_id: str
    change: Literal["created", "updated", "status_changed"]
    summary: str


class Action(VersionedModel):
    """An append-only audit action."""

    id: str
    step: int
    actor: str
    tool: str
    input_digest: str
    output_digest: str
    input_payload: JsonValue
    output_payload: JsonValue
    # Optional for backwards-compatible replay of v0.1 logs written before scoped
    # computation provenance was recorded.  Newly recorded Actions always populate it.
    engagement_id: str | None = None
    world_time_before: UTCAwareDatetime
    world_time_after: UTCAwareDatetime
    mutations: list[Mutation]

    @model_validator(mode="after")
    def validate_audit_record(self) -> Action:
        """Validate I-8 and the canonical payload digests needed for replay."""

        if self.world_time_after < self.world_time_before:
            raise ValueError("world_time_after must be on or after world_time_before")
        expected_input_digest = logical_state_digest(self.input_payload)
        if self.input_digest != expected_input_digest:
            raise ValueError("input_digest must match canonical input_payload")
        expected_output_digest = logical_state_digest(self.output_payload)
        if self.output_digest != expected_output_digest:
            raise ValueError("output_digest must match canonical output_payload")
        return self


class ProvenanceRecord(VersionedModel):
    """A directed relation between a subject and its evidence basis."""

    id: str
    subject_ref: str
    basis_ref: str
    relation: Literal[
        "supported_by", "derived_from", "authorized_by", "contradicted_by"
    ]


class StateSnapshot(VersionedModel):
    """Metadata for a materialised initial or final state snapshot."""

    id: str
    episode_run_id: str
    phase: Literal["initial", "final"]
    db_path: str
    state_digest: str


class LayerScores(VersionedModel):
    """Score values for the evaluation layers."""

    task_completion: float
    accounting: float
    state: float
    safety: float
    provenance: float
    communication: float
    efficiency: float


class CriticalFailureHit(VersionedModel):
    """A critical failure that zeroes an evaluation result."""

    cf_id: str
    detail: str
    evidence_refs: list[str]


class CriterionResult(VersionedModel):
    """A deterministic or qualitative criterion result."""

    criterion_id: str
    passed: bool
    score: float
    detail: str
    evidence_refs: list[str]
    requires_review: bool = False


class Usage(VersionedModel):
    """Resource use accumulated during an episode run."""

    steps: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_s: float
    world_days_elapsed: float


class EvaluationResult(VersionedModel):
    """The stable per-run evaluation result schema."""

    episode_id: str
    run_id: str
    model: str
    critical_failures: list[CriticalFailureHit]
    criterion_results: list[CriterionResult]
    scores: LayerScores
    overall: float
    all_pass: bool
    usage: Usage


MODEL_TYPES: tuple[type[VersionedModel], ...] = (
    WorldManifest,
    PracticePolicies,
    Practice,
    Person,
    Client,
    Entity,
    UkTax,
    UsTax,
    Engagement,
    AccountingPeriod,
    Account,
    TaxTag,
    JournalLine,
    Journal,
    BankAccount,
    BankTransaction,
    Document,
    Thread,
    Message,
    IrqItem,
    InformationRequest,
    Task,
    EntityMatch,
    Offset,
    AtTime,
    AfterEntity,
    ClientReplyPayload,
    NewBankFeedPayload,
    ApprovalDecisionPayload,
    ReviewerNotePayload,
    DeadlinePayload,
    Event,
    Budget,
    DeliverableSpec,
    JudgeCriterion,
    ExpectedState,
    AccountBalance,
    TxnStatus,
    EntityCount,
    WorkpaperState,
    ApprovalRequested,
    TaskState,
    NoMutationOf,
    UnresolvedFlagged,
    DeterministicCriterion,
    ReferenceResult,
    EpisodeManifest,
    OutstandingItem,
    UnresolvedItem,
    BankReconWorkpaper,
    ClassificationRow,
    ClassificationSummaryWorkpaper,
    QueryLogWorkpaper,
    Workpaper,
    ReviewNote,
    PostJournalDescriptor,
    PostToClosedPeriodDescriptor,
    SendMessageDescriptor,
    ClosePeriodDescriptor,
    Approval,
    Mutation,
    Action,
    ProvenanceRecord,
    StateSnapshot,
    LayerScores,
    CriticalFailureHit,
    CriterionResult,
    Usage,
    EvaluationResult,
)
