"""The single registry from which the WP-07 tool and MCP schemas are exposed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pydantic import BaseModel

from mirrorfirm.core.models import Role

from .schemas import (
    AdvanceTimeInput,
    AggregateInput,
    BankTransactionsInput,
    CalculateInput,
    CompareDatasetsInput,
    CreateWorkpaperInput,
    DocumentListInput,
    DraftInformationRequestInput,
    DraftMessageInput,
    DraftReplyInput,
    EmptyInput,
    EscalateInput,
    FinalizeWorkpaperInput,
    FinishEpisodeInput,
    GetClientInput,
    GetTaskInput,
    LedgerQueryInput,
    PeriodInput,
    ProposeClassificationInput,
    ProposeJournalInput,
    ReadDocumentInput,
    ReadTableInput,
    ReadThreadInput,
    ReconciliationInput,
    RequestApprovalInput,
    SearchDocumentsInput,
    SubmitForReviewInput,
    TaskListInput,
    ToolCallResult,
    UpdateDraftInput,
    UpdateTaskStatusInput,
    UpdateWorkpaperInput,
)

ALL_INTERNAL_ROLES: frozenset[Role] = frozenset(
    {
        "bookkeeper",
        "staff_accountant",
        "reviewer",
        "partner",
        "admin",
        "agent",
    }
)
BOOKKEEPING_ROLES: frozenset[Role] = frozenset(
    {"bookkeeper", "staff_accountant", "agent"}
)


@dataclass(frozen=True)
class ToolDefinition:
    """One immutable public tool contract required by SPEC.md §F."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    roles: frozenset[Role]
    duration_minutes: int
    mutations: tuple[str, ...]
    approval_required: bool
    errors: tuple[str, ...]

    def mcp_schema(self) -> dict[str, object]:
        """Render the tool definition in the JSON shape used by MCP ``tools/list``."""

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_model.model_json_schema(),
        }


def _definition(
    name: str,
    description: str,
    input_model: type[BaseModel],
    *,
    roles: frozenset[Role] = ALL_INTERNAL_ROLES,
    duration_minutes: int = 0,
    mutations: tuple[str, ...] = (),
    approval_required: bool = False,
    errors: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_model=input_model,
        output_model=ToolCallResult,
        roles=roles,
        duration_minutes=duration_minutes,
        mutations=mutations,
        approval_required=approval_required,
        errors=(
            "PERMISSION_DENIED",
            "NOT_FOUND",
            "VALIDATION_ERROR",
            "SCOPE_VIOLATION",
            *errors,
        ),
    )


_DEFINITIONS = (
    _definition(
        "get_context", "Return the scoped practice and engagement context.", EmptyInput
    ),
    _definition("get_current_time", "Return UTC and local simulated time.", EmptyInput),
    _definition("list_tasks", "List tasks in the current engagement.", TaskListInput),
    _definition("get_task", "Read one scoped task.", GetTaskInput),
    _definition(
        "list_clients",
        "List clients visible in the current engagement scope.",
        EmptyInput,
    ),
    _definition("get_client", "Read one scoped client.", GetClientInput),
    _definition(
        "list_documents",
        "List currently available scoped documents.",
        DocumentListInput,
    ),
    _definition(
        "read_document", "Read a SHA-verified scoped document.", ReadDocumentInput
    ),
    _definition(
        "read_table",
        "Read a delimited scoped document as a typed grid.",
        ReadTableInput,
    ),
    _definition(
        "search_documents",
        "Search currently available scoped documents.",
        SearchDocumentsInput,
    ),
    _definition(
        "list_bank_transactions",
        "List scoped bank-feed transactions.",
        BankTransactionsInput,
    ),
    _definition("query_ledger", "Query scoped journal lines.", LedgerQueryInput),
    _definition(
        "trial_balance",
        "Return posted balances for one accounting period.",
        PeriodInput,
    ),
    _definition("list_threads", "List scoped communication threads.", EmptyInput),
    _definition(
        "read_thread", "Read a scoped thread and its messages.", ReadThreadInput
    ),
    _definition(
        "get_reconciliation_status",
        "Read a scoped reconciliation workpaper.",
        ReconciliationInput,
    ),
    _definition("list_approvals", "List scoped approvals.", EmptyInput),
    _definition("list_review_notes", "List scoped review notes.", EmptyInput),
    _definition(
        "aggregate_table",
        "Aggregate a table or scoped virtual data source.",
        AggregateInput,
        errors=("BAD_PREDICATE", "TYPE_MISMATCH"),
    ),
    _definition(
        "calculate",
        "Evaluate decimal or integer arithmetic with auditable bindings.",
        CalculateInput,
        errors=("DIV_ZERO", "UNRESOLVED_BINDING"),
    ),
    _definition(
        "compare_datasets",
        "Compare two tabular sources using declared keys.",
        CompareDatasetsInput,
        errors=("KEY_NOT_UNIQUE",),
    ),
    _definition(
        "propose_classification",
        "Derive proposed journals from bank-feed classifications.",
        ProposeClassificationInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=2,
        mutations=("Journal", "BankTransaction", "ProvenanceRecord"),
        errors=(
            "ALREADY_CLASSIFIED",
            "PROVENANCE_REQUIRED",
            "PERIOD_LOCKED",
            "SPLIT_MISMATCH",
            "TAX_NOT_SUPPORTED",
        ),
    ),
    _definition(
        "propose_journal",
        "Create a balanced proposed journal.",
        ProposeJournalInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=5,
        mutations=("Journal", "ProvenanceRecord"),
        errors=("UNBALANCED", "PERIOD_LOCKED", "PERIOD_CLOSED_NEEDS_APPROVAL"),
    ),
    _definition(
        "request_approval",
        "Request a reviewer approval for a constrained descriptor.",
        RequestApprovalInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=1,
        mutations=("Approval",),
        errors=("DUPLICATE_REQUEST", "NOTHING_TO_APPROVE", "DESCRIPTOR_MISMATCH"),
    ),
    _definition(
        "draft_information_request",
        "Draft a client evidence request.",
        DraftInformationRequestInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=5,
        mutations=("Thread", "Message", "InformationRequest"),
        errors=("NO_CONTACT",),
    ),
    _definition(
        "update_draft",
        "Update an unsent draft and its linked information request.",
        UpdateDraftInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=1,
        mutations=("Message", "InformationRequest"),
    ),
    _definition(
        "send_information_request",
        "Send a draft information request subject to practice policy.",
        DraftMessageInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=5,
        mutations=("Message", "InformationRequest"),
        approval_required=True,
        errors=("POLICY_REQUIRES_APPROVAL",),
    ),
    _definition(
        "draft_reply",
        "Draft an outbound reply in a client thread.",
        DraftReplyInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=3,
        mutations=("Thread", "Message"),
    ),
    _definition(
        "send_reply",
        "Send a draft reply subject to practice policy.",
        DraftMessageInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=3,
        mutations=("Message",),
        approval_required=True,
        errors=("POLICY_REQUIRES_APPROVAL",),
    ),
    _definition(
        "create_workpaper",
        "Create a typed draft workpaper for a scoped task.",
        CreateWorkpaperInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=10,
        mutations=("Workpaper", "ProvenanceRecord"),
        errors=("RECON_DOES_NOT_TIE", "PROVENANCE_REQUIRED"),
    ),
    _definition(
        "update_workpaper",
        "Update a typed draft workpaper.",
        UpdateWorkpaperInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=5,
        mutations=("Workpaper", "ProvenanceRecord"),
        errors=("RECON_DOES_NOT_TIE", "PROVENANCE_REQUIRED"),
    ),
    _definition(
        "finalize_workpaper",
        "Finalize an immutable workpaper after invariant checks.",
        FinalizeWorkpaperInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=2,
        mutations=("Workpaper",),
        errors=("ALREADY_FINAL", "RECON_DOES_NOT_TIE", "PROVENANCE_REQUIRED"),
    ),
    _definition(
        "update_task_status",
        "Move a scoped task through its lifecycle.",
        UpdateTaskStatusInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=1,
        mutations=("Task",),
        errors=("REVIEW_REQUIRED",),
    ),
    _definition(
        "submit_for_review",
        "Submit final workpapers for reviewer consideration.",
        SubmitForReviewInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=2,
        mutations=("Task",),
        errors=("DRAFT_WORKPAPER", "EMPTY_SUBMISSION"),
    ),
    _definition(
        "escalate",
        "Create a reviewer-visible escalation for scoped subjects.",
        EscalateInput,
        roles=BOOKKEEPING_ROLES,
        duration_minutes=2,
        mutations=("ReviewNote",),
    ),
    _definition(
        "advance_time",
        "Advance simulated time to a request or the next event.",
        AdvanceTimeInput,
        roles=BOOKKEEPING_ROLES,
        mutations=("Event",),
        errors=("PAST_TIME", "EXCEEDS_EPISODE_HORIZON"),
    ),
    _definition(
        "finish_episode",
        "Record the terminal episode summary and final state digest.",
        FinishEpisodeInput,
        roles=BOOKKEEPING_ROLES,
        mutations=(),
    ),
)


class ToolRegistry:
    """Lookup and MCP-schema facade over the immutable v0.1 definitions."""

    def __init__(self, definitions: tuple[ToolDefinition, ...] = _DEFINITIONS) -> None:
        self._definitions: Mapping[str, ToolDefinition] = {
            definition.name: definition for definition in definitions
        }

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def mcp_tools(self) -> list[dict[str, object]]:
        return [definition.mcp_schema() for definition in self.definitions()]


DEFAULT_REGISTRY = ToolRegistry()
