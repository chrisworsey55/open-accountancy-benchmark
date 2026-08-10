"""Deterministic, scoped implementation of the WP-07 simulated tool suite."""

from __future__ import annotations

import ast
import csv
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Account,
    Action,
    AfterEntity,
    Approval,
    ApprovalDecisionPayload,
    BankAccount,
    BankReconWorkpaper,
    BankTransaction,
    ClassificationSummaryWorkpaper,
    Client,
    ClientReplyPayload,
    ClosePeriodDescriptor,
    Document,
    Engagement,
    Event,
    InformationRequest,
    Journal,
    JournalLine,
    Message,
    Mutation,
    NewBankFeedPayload,
    Person,
    PostJournalDescriptor,
    PostToClosedPeriodDescriptor,
    Practice,
    ProvenanceRecord,
    QueryLogWorkpaper,
    ReviewerNotePayload,
    ReviewNote,
    SendMessageDescriptor,
    Task,
    Thread,
    VersionedModel,
    Workpaper,
    WorldManifest,
)
from mirrorfirm.core.models.domain import AccountingPeriod, AtTime, DeadlinePayload
from mirrorfirm.jurisdictions import JurisdictionPack, get_pack

from .documents import parse_document_text
from .errors import ToolExecutionError
from .registry import DEFAULT_REGISTRY, ToolRegistry
from .schemas import ToolCallResult

ModelT = TypeVar("ModelT", bound=VersionedModel)
MutationChange = Literal["created", "updated", "status_changed"]
ProvenanceRelation = Literal[
    "supported_by", "derived_from", "authorized_by", "contradicted_by"
]


class WorldToolEngine:
    """Run registry-defined tools against one writable, engagement-scoped world copy."""

    def __init__(
        self,
        store: WorldStore,
        *,
        world_root: str | Path,
        actor_id: str,
        engagement_id: str,
        registry: ToolRegistry = DEFAULT_REGISTRY,
        episode_run_id: str = "run-r000001",
        snapshot_dir: str | Path | None = None,
        active_event_ids: Iterable[str] | None = None,
    ) -> None:
        self.store = store
        self.world_root = Path(world_root).resolve()
        self.registry = registry
        self.episode_run_id = episode_run_id
        self.snapshot_dir = (
            Path(snapshot_dir).resolve()
            if snapshot_dir is not None
            else store.path.parent / "snapshots"
        )
        self._active_event_ids = (
            frozenset(active_event_ids) if active_event_ids is not None else None
        )
        self.actor = self._must_load(Person, actor_id)
        self.engagement = self._must_load(Engagement, engagement_id)
        self.client_id = self.engagement.client_id
        self.manifest = self._find_manifest()
        self.practice = self._find_practice()
        self.pack: JurisdictionPack = get_pack(self.manifest.jurisdiction)
        self._tables: dict[str, list[dict[str, Any]]] = {}
        actions = self._list(Action)
        self._finished = any(
            self._is_successful_finish_action(action) for action in actions
        )
        self._time_override: datetime | None = None
        self._runtime_sequence = self._initial_runtime_sequence()
        self._action_sequence = len(actions)
        self._pending_fired_event_ids: set[str] = set()
        self._visible_action_ids: set[str] = set()

    def call(
        self, name: str, payload: dict[str, object] | None = None
    ) -> ToolCallResult:
        """Validate, permission-check, execute, and audit one public tool call."""

        raw_input = payload or {}
        if self._finished:
            return ToolCallResult(
                ok=False,
                error=ToolExecutionError(
                    "VALIDATION_ERROR", "episode has already finished"
                ).error,
            )
        definition = self.registry.get(name)
        if definition is None:
            return self._failure(
                name,
                raw_input,
                ToolExecutionError(
                    "TOOL_NOT_FOUND", f"tool {name!r} is not registered"
                ),
            )

        try:
            input_value = definition.input_model.model_validate(raw_input)
        except ValidationError as error:
            return self._failure(
                name,
                raw_input,
                ToolExecutionError(
                    "VALIDATION_ERROR",
                    "tool input does not match the registered schema",
                    {"errors": error.errors(include_url=False)},
                ),
            )

        if self.actor.role not in definition.roles:
            return self._failure(
                name,
                raw_input,
                ToolExecutionError(
                    "PERMISSION_DENIED",
                    f"{self.actor.role} may not call {name}",
                ),
            )

        call_started = self.now
        try:
            with self.store.transaction():
                if name == "advance_time":
                    result, mutations = self._advance_time(input_value)
                else:
                    target = self.now + timedelta(minutes=definition.duration_minutes)
                    fired_events = self._advance_to(target)
                    self._time_override = target
                    result, mutations = self._dispatch(name, input_value)
                    fired_events.extend(self._advance_to(target))
                    if fired_events:
                        result = {**result, "fired_events": fired_events}
                action = self._record(
                    name,
                    self._json_value(input_value),
                    self._json_value(result),
                    mutations,
                    before=call_started,
                )
                if name == "finish_episode":
                    try:
                        snapshot = self.store.snapshot(
                            self.snapshot_dir / f"{result['final_snapshot_id']}.db",
                            snapshot_id=str(result["final_snapshot_id"]),
                            episode_run_id=self.episode_run_id,
                            phase="final",
                            transactional=True,
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        raise ToolExecutionError(
                            "VALIDATION_ERROR", "final snapshot could not be created"
                        ) from error
            if name == "finish_episode":
                self._finished = True
                result = {
                    **result,
                    "final_snapshot": self._dump(snapshot),
                    "state_digest": snapshot.state_digest,
                }
            self._visible_action_ids.add(action.id)
            self._pending_fired_event_ids.clear()
            return ToolCallResult(ok=True, result=result, action_id=action.id)
        except ToolExecutionError as error:
            self._pending_fired_event_ids.clear()
            self._action_sequence = len(self._list(Action))
            return self._failure(name, raw_input, error, before=call_started)
        finally:
            self._time_override = None

    @property
    def now(self) -> datetime:
        """Return world time derived solely from the append-only action log."""

        if self._time_override is not None:
            return self._time_override
        actions = self._list(Action)
        return (
            actions[-1].world_time_after if actions else self.manifest.start_world_time
        )

    @property
    def finished(self) -> bool:
        """Whether a successful terminal ``finish_episode`` Action was committed."""

        return self._finished

    def _dispatch(
        self, name: str, input_value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        handler = cast(
            Callable[[BaseModel], tuple[dict[str, Any], list[Mutation]]],
            getattr(self, f"_tool_{name}"),
        )
        return handler(input_value)

    def _tool_get_context(self, _: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        client = self._must_load(Client, self.client_id)
        return (
            {
                "practice": self._dump(self.practice),
                "engagement": self._dump(self.engagement),
                "client": self._dump(client),
                "actor": self._dump(self.actor),
            },
            [],
        )

    def _tool_get_current_time(
        self, _: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        next_event = self._next_event(self.now, None)
        return (
            {
                "world_time": self.now.isoformat().replace("+00:00", "Z"),
                "local_time": self.pack.render_local(self.now),
                "next_event_exists": next_event is not None,
            },
            [],
        )

    def _tool_list_tasks(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        status = getattr(value, "status")
        tasks = [
            task
            for task in self._list(Task)
            if task.engagement_id == self.engagement.id
            and (status is None or task.status == status)
        ]
        return {"tasks": [self._dump(task) for task in tasks]}, []

    def _tool_get_task(self, value: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        task = self._must_load(Task, getattr(value, "task_id"))
        self._require_task_scope(task)
        return {"task": self._dump(task)}, []

    def _tool_list_clients(self, _: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        return {"clients": [self._dump(self._must_load(Client, self.client_id))]}, []

    def _tool_get_client(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        client_id = getattr(value, "client_id")
        self._require_scope(client_id)
        return {"client": self._dump(self._must_load(Client, client_id))}, []

    def _tool_list_documents(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        kind = getattr(value, "kind")
        documents = [
            document
            for document in self._list(Document)
            if document.client_id == self.client_id
            and self._document_is_available(document)
            and (kind is None or document.kind == kind)
        ]
        return {"documents": [self._dump(document) for document in documents]}, []

    def _tool_read_document(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        document = self._available_document(getattr(value, "doc_id"))
        text = self._read_document_text(document)
        page_range = getattr(value, "page_range")
        if page_range:
            text = self._slice_pages(text, page_range)
        return {"document": self._dump(document), "content": text}, []

    def _tool_read_table(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        document = self._available_document(getattr(value, "doc_id"))
        text = self._read_document_text(document)
        try:
            raw_rows = list(csv.reader(text.splitlines()))
        except csv.Error as error:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "document is not a readable table"
            ) from error
        header_row = getattr(value, "header_row") or 1
        if header_row < 1 or len(raw_rows) <= header_row:
            raise ToolExecutionError("VALIDATION_ERROR", "table has no data rows")
        header = raw_rows[header_row - 1]
        normalized = [
            {
                column: row[index] if index < len(row) else ""
                for index, column in enumerate(header)
            }
            for row in raw_rows[header_row:]
        ]
        normalized = self._slice_table_range(normalized, getattr(value, "range"))
        table_ref = f"tbl-{len(self._tables) + 1:06d}"
        self._tables[table_ref] = normalized
        return {"table_ref": table_ref, "rows": normalized}, []

    def _tool_search_documents(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        query = getattr(value, "query").casefold()
        kind = getattr(value, "kind")
        matches: list[dict[str, Any]] = []
        for document in self._list(Document):
            if document.client_id != self.client_id or not self._document_is_available(
                document
            ):
                continue
            if kind is not None and document.kind != kind:
                continue
            text = self._read_document_text(document)
            if query in text.casefold():
                matches.append(
                    {
                        "document": self._dump(document),
                        "snippet": self._snippet(text, query),
                    }
                )
        return {"matches": matches}, []

    def _tool_list_bank_transactions(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        bank_account = self._must_load(BankAccount, getattr(value, "bank_account_id"))
        self._require_scope(self._client_for_entity(bank_account.entity_id))
        period_id = getattr(value, "period_id")
        period = self._scoped_period(period_id) if period_id else None
        status = getattr(value, "status")
        transactions = [
            transaction
            for transaction in self._list(BankTransaction)
            if transaction.bank_account_id == bank_account.id
            and self._bank_transaction_is_available(transaction.id)
            and (period is None or period.start <= transaction.date <= period.end)
            and (status is None or transaction.classification_status == status)
        ]
        return {
            "transactions": [self._dump(transaction) for transaction in transactions]
        }, []

    def _tool_query_ledger(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        account_id = getattr(value, "account")
        if account_id is not None:
            account = self._must_load(Account, account_id)
            self._require_scope(self._client_for_entity(account.entity_id))
        period_id = getattr(value, "period")
        period = self._scoped_period(period_id) if period_id else None
        status = getattr(value, "status")
        source = getattr(value, "source")
        lines: list[dict[str, Any]] = []
        for journal in self._list(Journal):
            if self._client_for_entity(journal.entity_id) != self.client_id:
                continue
            if period and not (period.start <= journal.date <= period.end):
                continue
            if status and journal.status != status:
                continue
            if source and journal.source != source:
                continue
            for index, line in enumerate(journal.lines):
                if account_id is not None and line.account_id != account_id:
                    continue
                lines.append(
                    {
                        "journal_id": journal.id,
                        "line_index": index,
                        "date": journal.date.isoformat(),
                        "status": journal.status,
                        "source": journal.source,
                        **self._dump(line),
                        "source_ref": journal.id,
                    }
                )
        return {"lines": lines}, []

    def _tool_trial_balance(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        period = self._scoped_period(getattr(value, "period_id"))
        balances: dict[str, int] = {}
        for journal in self._list(Journal):
            if journal.status != "posted" or journal.entity_id != period.entity_id:
                continue
            if not period.start <= journal.date <= period.end:
                continue
            for line in journal.lines:
                signed = (
                    line.amount_minor if line.direction == "dr" else -line.amount_minor
                )
                balances[line.account_id] = balances.get(line.account_id, 0) + signed
        return {"period_id": period.id, "balances": balances}, []

    def _tool_list_threads(self, _: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        threads = [
            thread
            for thread in self._list(Thread)
            if thread.client_id == self.client_id
        ]
        return {"threads": [self._dump(thread) for thread in threads]}, []

    def _tool_read_thread(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        thread = self._must_load(Thread, getattr(value, "thread_id"))
        self._require_scope(thread.client_id)
        messages = [
            self._must_load(Message, message_id) for message_id in thread.message_ids
        ]
        return {
            "thread": self._dump(thread),
            "messages": [self._dump(message) for message in messages],
        }, []

    def _tool_get_reconciliation_status(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        bank_account = self._must_load(BankAccount, getattr(value, "bank_account_id"))
        period = self._scoped_period(getattr(value, "period_id"))
        self._require_scope(self._client_for_entity(bank_account.entity_id))
        workpapers = [
            workpaper
            for workpaper in self._list(Workpaper)
            if workpaper.engagement_id == self.engagement.id
            and isinstance(workpaper.body, BankReconWorkpaper)
            and workpaper.body.bank_account_id == bank_account.id
            and workpaper.body.period_id == period.id
        ]
        return {"workpapers": [self._dump(workpaper) for workpaper in workpapers]}, []

    def _tool_list_approvals(
        self, _: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        approvals = [
            approval
            for approval in self._list(Approval)
            if self._is_scoped_approval(approval)
        ]
        return {"approvals": [self._dump(approval) for approval in approvals]}, []

    def _tool_list_review_notes(
        self, _: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        notes = [
            note
            for note in self._list(ReviewNote)
            if note.engagement_id == self.engagement.id
        ]
        return {"review_notes": [self._dump(note) for note in notes]}, []

    def _tool_aggregate_table(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        rows = self._source_rows(getattr(value, "source"))
        filtered = [
            row
            for row in rows
            if self._matches_predicates(row, getattr(value, "filter"))
        ]
        group_by = getattr(value, "group_by")
        aggregations = getattr(value, "aggregations")
        groups: dict[tuple[object, ...], list[dict[str, Any]]] = {}
        for row in filtered:
            key = tuple(row.get(field) for field in group_by)
            groups.setdefault(key, []).append(row)
        output: list[dict[str, Any]] = []
        for key, members in sorted(groups.items(), key=lambda item: repr(item[0])):
            row = {field: key[index] for index, field in enumerate(group_by)}
            for aggregation in aggregations:
                field = aggregation.get("field")
                function = aggregation.get("fn")
                alias = aggregation.get("as", f"{function}_{field}")
                values = [member.get(field) for member in members] if field else []
                row[str(alias)] = self._aggregate(function, values)
            row["contributing_source_refs"] = [
                str(member.get("source_ref", member.get("id", "")))
                for member in members
            ]
            output.append(row)
        return {"rows": output}, []

    def _tool_calculate(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        bindings = {
            name: self._resolve_binding(binding)
            for name, binding in getattr(value, "bindings").items()
        }
        calculated = self._evaluate_expression(getattr(value, "expression"), bindings)
        action_id = self._next_action_id()
        return {"value": calculated, "calculation_action_id": action_id}, []

    def _tool_compare_datasets(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        left = self._dataset_rows(getattr(value, "left"))
        right = self._dataset_rows(getattr(value, "right"))
        keys = getattr(value, "keys")
        fields = getattr(value, "compare_fields")
        tolerance = getattr(value, "tolerance_minor")
        left_index = self._index_rows(left, keys)
        right_index = self._index_rows(right, keys)
        matched: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        for key in sorted(set(left_index) & set(right_index), key=repr):
            left_row, right_row = left_index[key], right_index[key]
            differences = [
                field
                for field in fields
                if not self._equal_with_tolerance(
                    left_row.get(field), right_row.get(field), tolerance
                )
            ]
            target = mismatched if differences else matched
            target.append(
                {
                    "key": key,
                    "left": left_row,
                    "right": right_row,
                    "left_ref": left_row.get("source_ref", repr(key)),
                    "right_ref": right_row.get("source_ref", repr(key)),
                    "different_fields": differences,
                }
            )
        return (
            {
                "matched": matched,
                "left_only": [
                    left_index[key]
                    for key in sorted(set(left_index) - set(right_index), key=repr)
                ],
                "right_only": [
                    right_index[key]
                    for key in sorted(set(right_index) - set(left_index), key=repr)
                ],
                "mismatched": mismatched,
            },
            [],
        )

    def _tool_propose_classification(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        prepared: list[tuple[BankTransaction, Journal, list[str]]] = []
        for item in getattr(value, "items"):
            transaction = self._must_load(BankTransaction, item.bank_transaction_id)
            self._require_scope(self._client_for_bank_transaction(transaction))
            if not self._bank_transaction_is_available(transaction.id):
                raise ToolExecutionError(
                    "NOT_FOUND",
                    "bank transaction is not available at current world time",
                )
            if transaction.classification_status != "unclassified":
                raise ToolExecutionError(
                    "ALREADY_CLASSIFIED", f"{transaction.id} is already classified"
                )
            self._require_provenance(item.provenance_refs)
            bank_account = self._must_load(BankAccount, transaction.bank_account_id)
            period = self._period_for_date(bank_account.entity_id, transaction.date)
            self._guard_period(period)
            splits = item.splits
            if splits is None:
                splits = [
                    type(
                        "SingleSplit",
                        (),
                        {
                            "account_id": item.account_id,
                            "gross_minor": abs(transaction.amount_minor),
                            "tax": item.tax,
                        },
                    )()
                ]
            if sum(split.gross_minor for split in splits) != abs(
                transaction.amount_minor
            ):
                raise ToolExecutionError(
                    "SPLIT_MISMATCH",
                    f"splits for {transaction.id} do not equal its gross bank amount",
                )
            lines = self._classification_lines(transaction, bank_account, splits)
            journal = Journal(
                id=self._new_id("jnl"),
                entity_id=bank_account.entity_id,
                date=transaction.date,
                memo=item.note
                or f"Proposed classification for {transaction.reference}",
                source="feed_classification",
                status="proposed",
                lines=lines,
                proposed_by=self.actor.id,
                approval_id=None,
            )
            prepared.append((transaction, journal, item.provenance_refs))

        mutations: list[Mutation] = []
        journal_ids: list[str] = []
        for transaction, journal, provenance_refs in prepared:
            self.store.save(journal)
            self.store.save(
                transaction.model_copy(update={"classification_status": "proposed"})
            )
            for basis_ref in provenance_refs:
                provenance = self._save_provenance(journal.id, basis_ref)
                mutations.append(
                    self._mutation(
                        "ProvenanceRecord",
                        provenance.id,
                        "created",
                        "classification provenance recorded",
                    )
                )
            journal_ids.append(journal.id)
            mutations.extend(
                (
                    self._mutation(
                        "Journal",
                        journal.id,
                        "created",
                        "proposed classification journal",
                    ),
                    self._mutation(
                        "BankTransaction",
                        transaction.id,
                        "status_changed",
                        "classification proposed",
                    ),
                )
            )
        return {
            "journal_ids": journal_ids,
            "transaction_ids": [item[0].id for item in prepared],
        }, mutations

    def _tool_propose_journal(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        lines = list(getattr(value, "lines"))
        entity_ids = {
            self._must_load(Account, line.account_id).entity_id for line in lines
        }
        if len(entity_ids) != 1:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "journal lines must belong to one entity"
            )
        entity_id = entity_ids.pop()
        self._require_scope(self._client_for_entity(entity_id))
        period = self._period_for_date(entity_id, getattr(value, "date"))
        if period.status == "locked":
            self._guard_period(period)
        self._require_provenance(getattr(value, "provenance_refs"))
        try:
            journal = Journal(
                id=self._new_id("jnl"),
                entity_id=entity_id,
                date=getattr(value, "date"),
                memo=getattr(value, "memo"),
                source="proposal",
                status="proposed",
                lines=lines,
                proposed_by=self.actor.id,
                approval_id=None,
            )
        except ValidationError as error:
            raise ToolExecutionError(
                "UNBALANCED", "journal does not balance", {"errors": error.errors()}
            ) from error
        self.store.save(journal)
        mutations = [
            self._mutation("Journal", journal.id, "created", "proposed journal")
        ]
        for basis_ref in getattr(value, "provenance_refs"):
            provenance = self._save_provenance(journal.id, basis_ref)
            mutations.append(
                self._mutation(
                    "ProvenanceRecord",
                    provenance.id,
                    "created",
                    "journal provenance recorded",
                )
            )
        return {"journal": self._dump(journal)}, mutations

    def _tool_request_approval(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        descriptor = getattr(value, "action_descriptor")
        kind = getattr(value, "kind")
        if descriptor.kind != kind:
            raise ToolExecutionError(
                "DESCRIPTOR_MISMATCH", "descriptor kind does not match approval kind"
            )
        self._require_provenance(getattr(value, "provenance_refs"))
        self._validate_approval_target(descriptor)
        for approval in self._list(Approval):
            if (
                approval.status == "requested"
                and approval.action_descriptor == descriptor
            ):
                raise ToolExecutionError(
                    "DUPLICATE_REQUEST", "an identical approval is already requested"
                )
        approval = Approval(
            id=self._new_id("apv"),
            kind=kind,
            requested_by=self.actor.id,
            approver_role="reviewer",
            action_descriptor=descriptor,
            rationale=getattr(value, "rationale"),
            provenance_refs=getattr(value, "provenance_refs"),
            status="requested",
        )
        self.store.save(approval)
        return {"approval": self._dump(approval)}, [
            self._mutation("Approval", approval.id, "created", "approval requested")
        ]

    def _tool_draft_information_request(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        client_id = getattr(value, "client_id")
        self._require_scope(client_id)
        client = self._must_load(Client, client_id)
        contacts = [
            self._must_load(Person, contact_id) for contact_id in client.contacts
        ]
        if not contacts:
            raise ToolExecutionError("NO_CONTACT", "client has no available contact")
        self._require_attachments(getattr(value, "attachments"))
        for item in getattr(value, "items"):
            for reference in item.refs:
                self._require_scoped_reference(reference)
        thread = Thread(
            id=self._new_id("thr"),
            client_id=client_id,
            subject="Information request",
            message_ids=[],
        )
        message = Message(
            id=self._new_id("msg"),
            thread_id=thread.id,
            sender=self.actor.id,
            recipients=[contacts[0].id],
            status="draft",
            world_time=None,
            body=getattr(value, "body"),
            attachments=getattr(value, "attachments"),
            direction="outbound",
        )
        thread = thread.model_copy(update={"message_ids": [message.id]})
        irq = InformationRequest(
            id=self._new_id("irq"),
            client_id=client_id,
            thread_id=thread.id,
            items=getattr(value, "items"),
            status="draft",
        )
        for record in (thread, message, irq):
            self.store.save(record)
        return (
            {
                "thread": self._dump(thread),
                "message": self._dump(message),
                "information_request": self._dump(irq),
            },
            [
                self._mutation(
                    "Thread", thread.id, "created", "information-request thread"
                ),
                self._mutation(
                    "Message", message.id, "created", "information-request draft"
                ),
                self._mutation(
                    "InformationRequest", irq.id, "created", "draft information request"
                ),
            ],
        )

    def _tool_update_draft(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        message = self._must_load(Message, getattr(value, "message_id"))
        self._require_scope(self._client_for_message(message))
        if message.status != "draft":
            raise ToolExecutionError(
                "VALIDATION_ERROR", "only draft messages may be updated"
            )
        attachments = getattr(value, "attachments")
        if attachments is not None:
            self._require_attachments(attachments)
        updated = message.model_copy(
            update={
                "body": getattr(value, "body")
                if getattr(value, "body") is not None
                else message.body,
                "attachments": attachments
                if attachments is not None
                else message.attachments,
            }
        )
        self.store.save(updated)
        mutations = [self._mutation("Message", message.id, "updated", "draft updated")]
        irq = self._irq_for_thread(message.thread_id)
        items = getattr(value, "items")
        if irq is not None and items is not None:
            irq = irq.model_copy(update={"items": items})
            self.store.save(irq)
            mutations.append(
                self._mutation(
                    "InformationRequest", irq.id, "updated", "request items updated"
                )
            )
        return {
            "message": self._dump(updated),
            "information_request": self._dump(irq) if irq else None,
        }, mutations

    def _tool_send_information_request(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        message = self._must_load(Message, getattr(value, "draft_message_id"))
        self._require_scope(self._client_for_message(message))
        irq = self._irq_for_thread(message.thread_id)
        if irq is None or message.status != "draft" or irq.status != "draft":
            raise ToolExecutionError(
                "NOTHING_TO_APPROVE",
                "no draft information request is available to send",
            )
        self._guard_outbound_policy(message.id)
        sent, sent_irq, mutations = self._send_message(message, irq)
        return {
            "message": self._dump(sent),
            "information_request": self._dump(sent_irq),
        }, mutations

    def _tool_draft_reply(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        thread = self._must_load(Thread, getattr(value, "thread_id"))
        self._require_scope(thread.client_id)
        client = self._must_load(Client, thread.client_id)
        self._require_attachments(getattr(value, "attachments"))
        message = Message(
            id=self._new_id("msg"),
            thread_id=thread.id,
            sender=self.actor.id,
            recipients=list(client.contacts),
            status="draft",
            world_time=None,
            body=getattr(value, "body"),
            attachments=getattr(value, "attachments"),
            direction="outbound",
        )
        self.store.save(message)
        updated_thread = thread.model_copy(
            update={"message_ids": [*thread.message_ids, message.id]}
        )
        self.store.save(updated_thread)
        return {"message": self._dump(message)}, [
            self._mutation("Message", message.id, "created", "reply draft"),
            self._mutation("Thread", thread.id, "updated", "reply draft linked"),
        ]

    def _tool_send_reply(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        message = self._must_load(Message, getattr(value, "draft_message_id"))
        self._require_scope(self._client_for_message(message))
        if message.status != "draft":
            raise ToolExecutionError(
                "NOTHING_TO_APPROVE", "no draft reply is available to send"
            )
        self._guard_outbound_policy(message.id)
        sent, _, mutations = self._send_message(message, None)
        return {"message": self._dump(sent)}, mutations

    def _tool_create_workpaper(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        task = self._must_load(Task, getattr(value, "task_id"))
        self._require_task_scope(task)
        body = getattr(value, "body")
        self._validate_workpaper_body(body)
        workpaper = Workpaper(
            id=self._new_id("wpp"),
            engagement_id=self.engagement.id,
            task_id=task.id,
            body=body,
            status="draft",
            created_by=self.actor.id,
            created_world_time=self.now,
        )
        self.store.save(workpaper)
        provenance = self._save_workpaper_provenance(workpaper)
        return {"workpaper": self._dump(workpaper)}, [
            self._mutation("Workpaper", workpaper.id, "created", "draft workpaper"),
            *[
                self._mutation(
                    "ProvenanceRecord",
                    record.id,
                    "created",
                    "workpaper provenance recorded",
                )
                for record in provenance
            ],
        ]

    def _tool_update_workpaper(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        workpaper = self._must_load(Workpaper, getattr(value, "workpaper_id"))
        self._require_workpaper_scope(workpaper)
        if workpaper.status != "draft":
            raise ToolExecutionError("ALREADY_FINAL", "final workpapers are immutable")
        body = getattr(value, "body")
        self._validate_workpaper_body(body)
        updated = workpaper.model_copy(update={"body": body})
        self.store.save(updated)
        provenance = self._save_workpaper_provenance(updated)
        return {"workpaper": self._dump(updated)}, [
            self._mutation(
                "Workpaper", updated.id, "updated", "draft workpaper updated"
            ),
            *[
                self._mutation(
                    "ProvenanceRecord",
                    record.id,
                    "created",
                    "workpaper provenance recorded",
                )
                for record in provenance
            ],
        ]

    def _tool_finalize_workpaper(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        workpaper = self._must_load(Workpaper, getattr(value, "workpaper_id"))
        self._require_workpaper_scope(workpaper)
        if workpaper.status == "final":
            raise ToolExecutionError("ALREADY_FINAL", "workpaper is already final")
        self._validate_workpaper_body(workpaper.body)
        final = workpaper.model_copy(
            update={"status": "final", "finalized_world_time": self.now}
        )
        self.store.save(final)
        reconciliation_mutations = self._flag_reconciliation_transactions(final.body)
        return {"workpaper": self._dump(final)}, [
            self._mutation(
                "Workpaper", final.id, "status_changed", "workpaper finalized"
            ),
            *reconciliation_mutations,
        ]

    def _tool_update_task_status(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        task = self._must_load(Task, getattr(value, "task_id"))
        self._require_task_scope(task)
        status = getattr(value, "status")
        updated = self._transition_task(
            task,
            status,
            blocked_on=getattr(value, "blocked_on"),
        )
        self.store.save(updated)
        return {"task": self._dump(updated)}, [
            self._mutation("Task", task.id, "status_changed", f"task moved to {status}")
        ]

    def _tool_submit_for_review(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        task = self._must_load(Task, getattr(value, "task_id"))
        self._require_task_scope(task)
        if task.status != "in_progress":
            raise ToolExecutionError(
                "VALIDATION_ERROR",
                "only an in-progress task may be submitted for review",
            )
        workpaper_ids = getattr(value, "workpaper_ids")
        if not workpaper_ids:
            raise ToolExecutionError(
                "EMPTY_SUBMISSION", "at least one workpaper is required"
            )
        for workpaper_id in workpaper_ids:
            workpaper = self._must_load(Workpaper, workpaper_id)
            self._require_workpaper_scope(workpaper)
            if workpaper.task_id != task.id or workpaper.status != "final":
                raise ToolExecutionError(
                    "DRAFT_WORKPAPER",
                    "submitted workpapers must be final and belong to the task",
                )
        updated = self._transition_task(task, "ready_for_review")
        self.store.save(updated)
        return {"task": self._dump(updated), "summary": getattr(value, "summary")}, [
            self._mutation("Task", task.id, "status_changed", "submitted for review")
        ]

    def _tool_escalate(self, value: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        refs = getattr(value, "subject_refs")
        for reference in refs:
            self._require_scoped_reference(reference)
        note = ReviewNote(
            id=self._new_id("rvn"),
            engagement_id=self.engagement.id,
            target_ref=refs[0],
            author_id=self.actor.id,
            body=f"Escalated to {getattr(value, 'to_role')}: {getattr(value, 'reason')}",
            created_world_time=self.now,
            status="open",
        )
        self.store.save(note)
        return {"review_note": self._dump(note), "subject_refs": refs}, [
            self._mutation("ReviewNote", note.id, "created", "escalation recorded")
        ]

    def _tool_finish_episode(
        self, value: BaseModel
    ) -> tuple[dict[str, Any], list[Mutation]]:
        for reference in getattr(value, "deliverable_refs"):
            self._require_scoped_reference(reference)
        snapshot_id = self._new_id("snp")
        return (
            {
                "summary": getattr(value, "summary"),
                "deliverable_refs": getattr(value, "deliverable_refs"),
                "unresolved_items": getattr(value, "unresolved_items"),
                "final_snapshot_id": snapshot_id,
            },
            [],
        )

    def _advance_time(self, value: BaseModel) -> tuple[dict[str, Any], list[Mutation]]:
        requested = getattr(value, "until") or self.now + timedelta(
            minutes=getattr(value, "minutes")
        )
        if requested.tzinfo is None or requested.utcoffset() is None:
            raise ToolExecutionError("VALIDATION_ERROR", "until must be timezone-aware")
        requested = requested.astimezone(UTC)
        if requested <= self.now:
            raise ToolExecutionError(
                "PAST_TIME", "requested time must be after current world time"
            )
        if requested > self.now + timedelta(days=7):
            raise ToolExecutionError(
                "EXCEEDS_EPISODE_HORIZON",
                "one call may advance at most seven world-days",
            )
        next_event = self._next_event(self.now, requested)
        target = (
            next_event[1]
            if next_event is not None and next_event[1] < requested
            else requested
        )
        fired = self._advance_to(target)
        self._time_override = target
        return (
            {
                "world_time": target.isoformat().replace("+00:00", "Z"),
                "local_time": self.pack.render_local(target),
                "fired_events": fired,
                "stopped_early": target < requested,
            },
            [],
        )

    def _advance_to(self, target: datetime) -> list[dict[str, Any]]:
        """Apply every event crossed before ``target`` in deterministic time/id order."""

        fired: list[dict[str, Any]] = []
        while True:
            next_event = self._next_event(self.now, target)
            if next_event is None:
                break
            event, due_time, bound = next_event
            fired.append(self._fire_event(event, due_time, bound))
        return fired

    def _next_event(
        self, current: datetime, ceiling: datetime | None
    ) -> tuple[Event, datetime, BaseModel | None] | None:
        candidates: list[tuple[datetime, Event, BaseModel | None]] = []
        for event in self._list(Event):
            if (
                self._active_event_ids is not None
                and event.id not in self._active_event_ids
            ):
                continue
            if event.fired or event.id in self._pending_fired_event_ids:
                continue
            scheduled = self._event_due_time(event)
            if scheduled is None:
                continue
            due_time, bound = scheduled
            if not self._event_is_eligible(event, bound):
                continue
            if due_time < current or (ceiling is not None and due_time > ceiling):
                continue
            candidates.append((due_time, event, bound))
        if not candidates:
            return None
        due_time, event, bound = min(
            candidates, key=lambda candidate: (candidate[0], candidate[1].id)
        )
        return event, due_time, bound

    def _event_due_time(self, event: Event) -> tuple[datetime, BaseModel | None] | None:
        if isinstance(event.trigger, AtTime):
            return event.trigger.world_time, None
        trigger = event.trigger
        assert isinstance(trigger, AfterEntity)
        bound = self._first_matching_entity(trigger)
        if bound is None:
            return None
        bound_time = self._entity_change_time(bound)
        if bound_time is None:
            return None
        due = self.pack.add_business_days(bound_time, trigger.offset.business_days)
        return due + timedelta(
            hours=trigger.offset.hours, minutes=trigger.offset.minutes
        ), bound

    def _fire_event(
        self, event: Event, due_time: datetime, bound: BaseModel | None
    ) -> dict[str, Any]:
        payload = event.payload
        if not self._event_is_eligible(event, bound):
            raise ToolExecutionError(
                "SCOPE_VIOLATION",
                "event is outside the current engagement/client scope",
            )
        mutations = [self._mutation("Event", event.id, "status_changed", "event fired")]
        surface_refs: list[str] = []
        if isinstance(payload, ClientReplyPayload):
            thread_id = self._expand_template(payload.thread_ref, bound)
            thread = self._must_load(Thread, thread_id)
            client = self._must_load(Client, thread.client_id)
            sender = client.contacts[0] if client.contacts else "per-sys"
            message = Message(
                id=self._new_id("msg"),
                thread_id=thread.id,
                sender=sender,
                recipients=[self.actor.id],
                status="sent",
                world_time=due_time,
                body=payload.body,
                attachments=payload.attachment_fixture_refs,
                direction="inbound",
            )
            self.store.save(message)
            updated_thread = thread.model_copy(
                update={"message_ids": [*thread.message_ids, message.id]}
            )
            self.store.save(updated_thread)
            mutations.append(
                self._mutation(
                    "Message", message.id, "created", "client reply delivered"
                )
            )
            mutations.append(
                self._mutation("Thread", thread.id, "updated", "client reply linked")
            )
            if payload.marks_irq:
                irq = self._irq_for_thread(thread.id, status="sent")
                if irq is not None:
                    self.store.save(
                        irq.model_copy(update={"status": payload.marks_irq})
                    )
                    mutations.append(
                        self._mutation(
                            "InformationRequest",
                            irq.id,
                            "status_changed",
                            payload.marks_irq,
                        )
                    )
            surface_refs.append(thread.id)
        elif isinstance(payload, ApprovalDecisionPayload):
            if not isinstance(bound, Approval):
                raise ToolExecutionError(
                    "VALIDATION_ERROR",
                    "approval decisions must be bound to a scoped approval event",
                )
            updated = bound.model_copy(update={"status": payload.decision})
            self.store.save(updated)
            mutations.append(
                self._mutation(
                    "Approval", updated.id, "status_changed", payload.decision
                )
            )
            surface_refs.append(updated.id)
            if payload.decision == "granted":
                self._execute_granted_approval(updated, due_time)
        elif isinstance(payload, ReviewerNotePayload):
            target = self._expand_template(payload.target_ref, bound)
            note = ReviewNote(
                id=self._new_id("rvn"),
                engagement_id=self.engagement.id,
                target_ref=target,
                author_id="per-sys",
                body=payload.note,
                created_world_time=due_time,
                status="open",
            )
            self.store.save(note)
            mutations.append(
                self._mutation("ReviewNote", note.id, "created", "reviewer event")
            )
            mutations.extend(self._apply_reviewer_effect(payload, target))
            surface_refs.append(target)
        elif isinstance(payload, NewBankFeedPayload):
            surface_refs.extend(payload.txn_fixture_refs)
        elif isinstance(payload, DeadlinePayload):
            if payload.task_id:
                surface_refs.append(payload.task_id)
        self.store.mark_event_fired(event.id)
        self._pending_fired_event_ids.add(event.id)
        output = {
            "event_id": event.id,
            "kind": payload.kind,
            "surface_refs": surface_refs,
        }
        self._record(
            f"event.{payload.kind}",
            {"event_id": event.id},
            output,
            mutations,
            actor="per-sys",
            at=due_time,
        )
        return output

    def _execute_granted_approval(self, approval: Approval, at: datetime) -> None:
        if approval.status != "granted":
            raise ToolExecutionError(
                "NOTHING_TO_APPROVE", "approval has not been granted"
            )
        descriptor = approval.action_descriptor
        mutations: list[Mutation] = []
        output: dict[str, Any] = {
            "approval_id": approval.id,
            "authorized_by": approval.id,
        }
        if isinstance(
            descriptor, (PostJournalDescriptor, PostToClosedPeriodDescriptor)
        ):
            journal = self._must_load(Journal, descriptor.journal_id)
            period = self._period_for_date(journal.entity_id, journal.date)
            if isinstance(descriptor, PostJournalDescriptor):
                if period.status == "closed":
                    raise ToolExecutionError(
                        "DESCRIPTOR_MISMATCH",
                        "closed-period journals require post_to_closed_period approval",
                    )
                self._guard_period(period)
            elif (
                period.status != "closed"
                or descriptor.period_id != period.id
                or descriptor.kind != "post_to_closed_period"
            ):
                raise ToolExecutionError(
                    "DESCRIPTOR_MISMATCH",
                    "closed-period approval does not match the proposed journal",
                )
            posted = journal.model_copy(
                update={"status": "posted", "approval_id": approval.id}
            )
            self.store.save(posted)
            mutations.append(
                self._mutation(
                    "Journal",
                    posted.id,
                    "status_changed",
                    "posted after granted approval",
                )
            )
            for line in posted.lines:
                if line.bank_transaction_id:
                    transaction = self._must_load(
                        BankTransaction, line.bank_transaction_id
                    )
                    self.store.save(
                        transaction.model_copy(
                            update={"classification_status": "classified"}
                        )
                    )
                    mutations.append(
                        self._mutation(
                            "BankTransaction",
                            transaction.id,
                            "status_changed",
                            "classified after posting",
                        )
                    )
            provenance = self._save_provenance(
                posted.id, approval.id, relation="authorized_by"
            )
            mutations.append(
                self._mutation(
                    "ProvenanceRecord",
                    provenance.id,
                    "created",
                    "approval authorization recorded",
                )
            )
            output["journal_id"] = posted.id
        elif isinstance(descriptor, SendMessageDescriptor):
            message = self._must_load(Message, descriptor.draft_message_id)
            sent, _, sent_mutations = self._send_message(
                message, self._irq_for_thread(message.thread_id), at=at
            )
            mutations.extend(sent_mutations)
            provenance = self._save_provenance(
                sent.id, approval.id, relation="authorized_by"
            )
            mutations.append(
                self._mutation(
                    "ProvenanceRecord",
                    provenance.id,
                    "created",
                    "approval authorization recorded",
                )
            )
            output["message_id"] = sent.id
        elif isinstance(descriptor, ClosePeriodDescriptor):
            period = self._must_load(AccountingPeriod, descriptor.period_id)
            self.store.save(period.model_copy(update={"status": "closed"}))
            mutations.append(
                self._mutation(
                    "AccountingPeriod",
                    period.id,
                    "status_changed",
                    "closed after granted approval",
                )
            )
            provenance = self._save_provenance(
                period.id, approval.id, relation="authorized_by"
            )
            mutations.append(
                self._mutation(
                    "ProvenanceRecord",
                    provenance.id,
                    "created",
                    "approval authorization recorded",
                )
            )
            output["period_id"] = period.id
        self._record(
            "system.execute_approval",
            {"approval_id": approval.id},
            output,
            mutations,
            actor="per-sys",
            at=at,
        )

    def _apply_reviewer_effect(
        self, payload: ReviewerNotePayload, target: str
    ) -> list[Mutation]:
        if payload.effect == "reopen_workpaper":
            workpaper = self._must_load(Workpaper, target)
            self._require_workpaper_scope(workpaper)
            self.store.save(
                workpaper.model_copy(
                    update={"status": "draft", "finalized_world_time": None}
                )
            )
            return [
                self._mutation(
                    "Workpaper", workpaper.id, "status_changed", "reopened by reviewer"
                )
            ]
        elif payload.effect == "set_task_status" and payload.effect_arg:
            task = self._must_load(Task, target)
            updated = self._transition_task(task, payload.effect_arg)
            self.store.save(updated)
            return [
                self._mutation(
                    "Task", task.id, "status_changed", "reviewer status change"
                )
            ]
        return []

    def _classification_lines(
        self,
        transaction: BankTransaction,
        bank_account: BankAccount,
        splits: Iterable[Any],
    ) -> list[JournalLine]:
        is_money_in = transaction.amount_minor > 0
        bank_direction: Literal["dr", "cr"] = "dr" if is_money_in else "cr"
        counter_direction: Literal["dr", "cr"] = "cr" if is_money_in else "dr"
        lines = [
            JournalLine(
                account_id=bank_account.ledger_account_id,
                direction=bank_direction,
                amount_minor=abs(transaction.amount_minor),
                currency=bank_account.currency,
                bank_transaction_id=transaction.id,
            )
        ]
        for split in splits:
            account = self._must_load(Account, split.account_id)
            if account.entity_id != bank_account.entity_id:
                raise ToolExecutionError(
                    "SCOPE_VIOLATION",
                    "classification account belongs to another client",
                )
            tax = split.tax
            if self.manifest.jurisdiction == "us" and tax is not None:
                raise ToolExecutionError(
                    "TAX_NOT_SUPPORTED", "US classifications do not accept TaxTag"
                )
            if tax is not None:
                try:
                    self.pack.validate_tax_tag(tax)
                except ValueError as error:
                    raise ToolExecutionError("VALIDATION_ERROR", str(error)) from error
            gross = split.gross_minor
            if tax is None or tax.code in {"0-zero", "exempt", "blocked"}:
                lines.append(
                    JournalLine(
                        account_id=account.id,
                        direction=counter_direction,
                        amount_minor=gross,
                        currency=bank_account.currency,
                        tax=tax,
                    )
                )
                continue
            net = int(
                (
                    Decimal(gross) * Decimal(10000) / Decimal(10000 + tax.rate_bp)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
            )
            vat = gross - net
            control_key = "output" if is_money_in else "input"
            control_account = self._vat_control_account(
                bank_account.entity_id, control_key
            )
            lines.extend(
                (
                    JournalLine(
                        account_id=account.id,
                        direction=counter_direction,
                        amount_minor=net,
                        currency=bank_account.currency,
                        tax=tax,
                    ),
                    JournalLine(
                        account_id=control_account.id,
                        direction=counter_direction,
                        amount_minor=vat,
                        currency=bank_account.currency,
                    ),
                )
            )
        return lines

    def _validate_workpaper_body(self, body: Any) -> None:
        provenance_sets: list[list[str]] = []
        if isinstance(body, BankReconWorkpaper):
            bank_account = self._must_load(BankAccount, body.bank_account_id)
            self._require_scope(self._client_for_entity(bank_account.entity_id))
            self._scoped_period(body.period_id)
            outstanding_total = sum(item.amount_minor for item in body.outstanding)
            difference = body.statement_end_minor - (
                body.ledger_end_minor + outstanding_total
            )
            if difference != 0 and not body.unresolved:
                raise ToolExecutionError(
                    "RECON_DOES_NOT_TIE",
                    "reconciliation does not tie",
                    {"difference_minor": difference},
                )
            provenance_sets.extend(item.provenance_refs for item in body.outstanding)
            provenance_sets.extend(item.provenance_refs for item in body.unresolved)
        elif isinstance(body, ClassificationSummaryWorkpaper):
            self._scoped_period(body.period_id)
            provenance_sets.extend(row.provenance_refs for row in body.rows)
        elif isinstance(body, QueryLogWorkpaper):
            provenance_sets.extend(item.provenance_refs for item in body.items)
        for references in provenance_sets:
            if not references:
                raise ToolExecutionError(
                    "PROVENANCE_REQUIRED", "material workpaper rows require provenance"
                )
            self._require_provenance(references)

    def _flag_reconciliation_transactions(self, body: Any) -> list[Mutation]:
        """Derive reconciliation flags from finalised workpaper provenance refs."""

        if not isinstance(body, BankReconWorkpaper):
            return []
        references = [
            reference for item in body.outstanding for reference in item.provenance_refs
        ]
        references.extend(
            reference for item in body.unresolved for reference in item.provenance_refs
        )
        mutations: list[Mutation] = []
        for transaction_id in sorted(set(references)):
            transaction = next(
                (
                    candidate
                    for candidate in self._list(BankTransaction)
                    if candidate.id == transaction_id
                ),
                None,
            )
            if transaction is None:
                continue
            self._require_scope(self._client_for_bank_transaction(transaction))
            if transaction.reconciliation_status == "flagged":
                continue
            self.store.save(
                transaction.model_copy(update={"reconciliation_status": "flagged"})
            )
            mutations.append(
                self._mutation(
                    "BankTransaction",
                    transaction.id,
                    "status_changed",
                    "flagged by finalised bank reconciliation provenance",
                )
            )
        return mutations

    def _save_workpaper_provenance(
        self, workpaper: Workpaper
    ) -> list[ProvenanceRecord]:
        body = workpaper.body
        references: list[str] = []
        if isinstance(body, BankReconWorkpaper):
            references.extend(
                reference
                for item in body.outstanding
                for reference in item.provenance_refs
            )
            references.extend(
                reference
                for item in body.unresolved
                for reference in item.provenance_refs
            )
        elif isinstance(body, ClassificationSummaryWorkpaper):
            references.extend(
                reference for row in body.rows for reference in row.provenance_refs
            )
        elif isinstance(body, QueryLogWorkpaper):
            references.extend(
                reference for item in body.items for reference in item.provenance_refs
            )
        return [
            self._save_provenance(workpaper.id, reference) for reference in references
        ]

    def _guard_period(self, period: AccountingPeriod) -> None:
        if period.status == "locked":
            raise ToolExecutionError("PERIOD_LOCKED", f"{period.id} is locked")
        if period.status == "closed":
            raise ToolExecutionError(
                "PERIOD_CLOSED_NEEDS_APPROVAL", f"{period.id} requires approval"
            )

    def _validate_approval_target(self, descriptor: Any) -> None:
        if isinstance(
            descriptor, (PostJournalDescriptor, PostToClosedPeriodDescriptor)
        ):
            journal = self._must_load(Journal, descriptor.journal_id)
            self._require_scope(self._client_for_entity(journal.entity_id))
            if journal.status != "proposed":
                raise ToolExecutionError(
                    "NOTHING_TO_APPROVE", "journal is not proposed"
                )
            period = self._period_for_date(journal.entity_id, journal.date)
            if isinstance(descriptor, PostJournalDescriptor):
                if period.status == "closed":
                    raise ToolExecutionError(
                        "DESCRIPTOR_MISMATCH",
                        "closed-period journals require post_to_closed_period approval",
                    )
                self._guard_period(period)
            else:
                period = self._scoped_period(descriptor.period_id)
                if period.status != "closed":
                    raise ToolExecutionError(
                        "DESCRIPTOR_MISMATCH", "period is not closed"
                    )
                if (
                    period.entity_id != journal.entity_id
                    or not period.start <= journal.date <= period.end
                ):
                    raise ToolExecutionError(
                        "DESCRIPTOR_MISMATCH",
                        "closed-period descriptor does not match journal date/entity",
                    )
        elif isinstance(descriptor, SendMessageDescriptor):
            message = self._must_load(Message, descriptor.draft_message_id)
            self._require_scope(self._client_for_message(message))
            if message.status != "draft":
                raise ToolExecutionError("NOTHING_TO_APPROVE", "message is not a draft")
        elif isinstance(descriptor, ClosePeriodDescriptor):
            period = self._scoped_period(descriptor.period_id)
            if period.status != "in_close":
                raise ToolExecutionError("NOTHING_TO_APPROVE", "period is not in close")

    def _guard_outbound_policy(self, draft_message_id: str) -> None:
        if self.practice.policies.outbound_comms == "agent_drafts_only":
            raise ToolExecutionError(
                "POLICY_REQUIRES_APPROVAL",
                "request approval before sending this draft",
                {
                    "hint": {
                        "kind": "send_external_message",
                        "draft_message_id": draft_message_id,
                    }
                },
            )

    def _send_message(
        self,
        message: Message,
        irq: InformationRequest | None,
        *,
        at: datetime | None = None,
    ) -> tuple[Message, InformationRequest | None, list[Mutation]]:
        if message.status != "draft":
            raise ToolExecutionError("NOTHING_TO_APPROVE", "message is not a draft")
        sent = message.model_copy(
            update={"status": "sent", "world_time": at or self.now}
        )
        self.store.save(sent)
        mutations = [
            self._mutation("Message", sent.id, "status_changed", "message sent")
        ]
        sent_irq: InformationRequest | None = None
        if irq is not None:
            sent_irq = irq.model_copy(update={"status": "sent"})
            self.store.save(sent_irq)
            mutations.append(
                self._mutation(
                    "InformationRequest",
                    sent_irq.id,
                    "status_changed",
                    "information request sent",
                )
            )
        return sent, sent_irq, mutations

    def _failure(
        self,
        name: str,
        input_payload: dict[str, object],
        error: ToolExecutionError,
        *,
        before: datetime | None = None,
    ) -> ToolCallResult:
        action = self._record(
            name,
            self._json_value(input_payload),
            {"error": self._dump(error.error)},
            [],
            before=before,
        )
        return ToolCallResult(ok=False, error=error.error, action_id=action.id)

    def _record(
        self,
        tool: str,
        input_payload: Any,
        output_payload: Any,
        mutations: list[Mutation],
        *,
        actor: str | None = None,
        at: datetime | None = None,
        before: datetime | None = None,
    ) -> Action:
        world_time = at or self.now
        action = Action(
            id=self._next_action_id(),
            step=self._action_sequence + 1,
            actor=actor or self.actor.id,
            tool=tool,
            input_digest=logical_state_digest(input_payload),
            output_digest=logical_state_digest(output_payload),
            input_payload=input_payload,
            output_payload=output_payload,
            world_time_before=before or (self.now if at is None else at),
            world_time_after=world_time,
            mutations=mutations,
        )
        self.store.append_action(action)
        self._action_sequence += 1
        return action

    def _must_load(self, model_type: type[ModelT], identifier: str) -> ModelT:
        try:
            return self.store.load(model_type, identifier)
        except KeyError as error:
            raise ToolExecutionError(
                "NOT_FOUND", f"{model_type.__name__} {identifier!r} was not found"
            ) from error

    def _list(self, model_type: type[ModelT]) -> tuple[ModelT, ...]:
        with self.store.view() as view:
            if model_type is Action:
                return cast(tuple[ModelT, ...], view.actions())
            if model_type is Event:
                return cast(tuple[ModelT, ...], view.events())
            return cast(tuple[ModelT, ...], view.list(model_type))

    def _find_manifest(self) -> WorldManifest:
        manifests = self._list(WorldManifest)
        if len(manifests) != 1:
            raise ValueError("tool engine requires exactly one world manifest")
        return manifests[0]

    def _find_practice(self) -> Practice:
        practices = self._list(Practice)
        if len(practices) != 1:
            raise ValueError("tool engine requires exactly one practice")
        return practices[0]

    def _initial_runtime_sequence(self) -> int:
        highest = 0
        state = self.store.logical_state()
        for records in state.values():
            for record in records:
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    identifier = record["id"]
                    if "-r" in identifier and identifier.rsplit("-r", 1)[1].isdigit():
                        highest = max(highest, int(identifier.rsplit("-r", 1)[1]))
        return highest

    def _new_id(self, prefix: str) -> str:
        self._runtime_sequence += 1
        return f"{prefix}-r{self._runtime_sequence:06d}"

    def _next_action_id(self) -> str:
        return f"act-r{self._action_sequence + 1:06d}"

    @staticmethod
    def _is_successful_finish_action(action: Action) -> bool:
        """Identify the committed terminal Action, not a failed finish attempt."""

        output = action.output_payload
        return (
            action.tool == "finish_episode"
            and isinstance(output, dict)
            and isinstance(output.get("final_snapshot_id"), str)
        )

    def _mutation(
        self, kind: str, identifier: str, change: MutationChange, summary: str
    ) -> Mutation:
        return Mutation(
            entity_kind=kind, entity_id=identifier, change=change, summary=summary
        )

    def _dump(self, value: Any) -> Any:
        return value.model_dump(mode="json") if isinstance(value, BaseModel) else value

    def _json_value(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): self._json_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._json_value(item) for item in value]
        return value

    def _require_scope(self, client_id: str | None) -> None:
        if client_id != self.client_id:
            raise ToolExecutionError(
                "SCOPE_VIOLATION", "reference is outside the current client scope"
            )

    def _require_task_scope(self, task: Task) -> None:
        self._require_scope(self._client_for_task(task))
        if task.engagement_id != self.engagement.id:
            raise ToolExecutionError(
                "SCOPE_VIOLATION",
                "task is outside the current engagement scope",
            )

    def _require_workpaper_scope(self, workpaper: Workpaper) -> None:
        self._require_scope(self._client_for_workpaper(workpaper))
        if workpaper.engagement_id != self.engagement.id:
            raise ToolExecutionError(
                "SCOPE_VIOLATION",
                "workpaper is outside the current engagement scope",
            )
        self._require_task_scope(self._must_load(Task, workpaper.task_id))

    def _task_is_scoped(self, task: Task) -> bool:
        return (
            task.engagement_id == self.engagement.id
            and self._client_for_task(task) == self.client_id
        )

    def _workpaper_is_scoped(self, workpaper: Workpaper) -> bool:
        return (
            workpaper.engagement_id == self.engagement.id
            and self._client_for_workpaper(workpaper) == self.client_id
            and self._task_is_scoped(self._must_load(Task, workpaper.task_id))
        )

    def _transition_task(
        self,
        task: Task,
        target_status: str,
        *,
        blocked_on: str | None = None,
    ) -> Task:
        """Apply the one shared §D.2 lifecycle transition policy."""

        self._require_task_scope(task)
        allowed = {
            "open": {"in_progress"},
            "in_progress": {"blocked", "ready_for_review"},
            "blocked": {"in_progress"},
            "ready_for_review": {"done"},
            "done": set(),
        }
        if target_status not in allowed[task.status]:
            if target_status == "done":
                raise ToolExecutionError(
                    "REVIEW_REQUIRED", "task must be submitted for review before done"
                )
            raise ToolExecutionError(
                "VALIDATION_ERROR",
                f"illegal task lifecycle transition {task.status} -> {target_status}",
            )
        if target_status == "blocked":
            if not blocked_on:
                raise ToolExecutionError(
                    "VALIDATION_ERROR", "blocked tasks require blocked_on"
                )
            self._require_scoped_reference(blocked_on)
        return task.model_copy(
            update={
                "status": target_status,
                "blocked_on": blocked_on if target_status == "blocked" else None,
            }
        )

    def _client_for_entity(self, entity_id: str) -> str:
        for client in self._list(Client):
            if client.entity.id == entity_id:
                return client.id
        raise ToolExecutionError("NOT_FOUND", f"entity {entity_id!r} was not found")

    def _client_for_task(self, task: Task) -> str:
        return self._must_load(Engagement, task.engagement_id).client_id

    def _client_for_workpaper(self, workpaper: Workpaper) -> str:
        return self._must_load(Engagement, workpaper.engagement_id).client_id

    def _client_for_message(self, message: Message) -> str:
        return self._must_load(Thread, message.thread_id).client_id

    def _client_for_bank_transaction(self, transaction: BankTransaction) -> str:
        bank_account = self._must_load(BankAccount, transaction.bank_account_id)
        return self._client_for_entity(bank_account.entity_id)

    def _bank_transaction_is_available(self, transaction_id: str) -> bool:
        for event in self._list(Event):
            if (
                not event.fired
                and isinstance(event.payload, NewBankFeedPayload)
                and transaction_id in event.payload.txn_fixture_refs
                and self._event_is_eligible(event, None)
            ):
                return False
        return True

    def _scoped_period(self, period_id: str) -> AccountingPeriod:
        period = self._must_load(AccountingPeriod, period_id)
        self._require_scope(self._client_for_entity(period.entity_id))
        return period

    def _period_for_date(self, entity_id: str, value: date) -> AccountingPeriod:
        for period in self._list(AccountingPeriod):
            if period.entity_id == entity_id and period.start <= value <= period.end:
                return period
        raise ToolExecutionError(
            "VALIDATION_ERROR", "journal date is not in an accounting period"
        )

    def _vat_control_account(self, entity_id: str, key: str) -> Account:
        expected_id = self.pack.vat_control_accounts[key]
        try:
            account = self.store.load(Account, expected_id)
        except KeyError:
            account = None
        if account is not None and account.entity_id == entity_id:
            return account
        code = "2201" if key == "output" else "2202"
        for candidate in self._list(Account):
            if candidate.entity_id == entity_id and candidate.code == code:
                return candidate
        raise ToolExecutionError(
            "NOT_FOUND", f"{key} VAT control account is unavailable"
        )

    def _available_document(self, doc_id: str) -> Document:
        document = self._must_load(Document, doc_id)
        self._require_scope(document.client_id)
        if not self._document_is_available(document):
            raise ToolExecutionError(
                "NOT_FOUND", "document is not available at current world time"
            )
        return document

    def _document_is_available(self, document: Document) -> bool:
        delayed_by_reply = [
            event
            for event in self._list(Event)
            if isinstance(event.payload, ClientReplyPayload)
            and document.id in event.payload.attachment_fixture_refs
        ]
        if delayed_by_reply:
            return any(event.fired for event in delayed_by_reply)
        return document.received_world_time <= self.now

    def _event_is_eligible(self, event: Event, bound: BaseModel | None) -> bool:
        """Whether an event may affect this engine's one engagement/client scope."""

        if isinstance(event.trigger, AfterEntity):
            if bound is None or not self._entity_is_scoped(bound):
                return False
            if (
                event.trigger.match.client_id is not None
                and event.trigger.match.client_id != self.client_id
            ):
                return False
        payload = event.payload
        if isinstance(payload, ClientReplyPayload):
            thread_id = self._expand_template(payload.thread_ref, bound)
            try:
                return self._must_load(Thread, thread_id).client_id == self.client_id
            except ToolExecutionError:
                return False
        if isinstance(payload, NewBankFeedPayload):
            try:
                bank = self._must_load(BankAccount, payload.bank_account_id)
                return self._client_for_entity(bank.entity_id) == self.client_id
            except ToolExecutionError:
                return False
        if isinstance(payload, DeadlinePayload):
            if payload.task_id is None:
                return False
            try:
                return self._task_is_scoped(self._must_load(Task, payload.task_id))
            except ToolExecutionError:
                return False
        if isinstance(payload, ApprovalDecisionPayload):
            return isinstance(bound, Approval) and self._is_scoped_approval(bound)
        if isinstance(payload, ReviewerNotePayload):
            target = self._expand_template(payload.target_ref, bound)
            try:
                self._require_scoped_reference(target)
                return True
            except ToolExecutionError:
                return False
        return False

    def _entity_is_scoped(self, entity: BaseModel) -> bool:
        if isinstance(entity, InformationRequest):
            return entity.client_id == self.client_id
        if isinstance(entity, Approval):
            return self._is_scoped_approval(entity)
        if isinstance(entity, Message):
            return self._client_for_message(entity) == self.client_id
        if isinstance(entity, Workpaper):
            return self._workpaper_is_scoped(entity)
        if isinstance(entity, Task):
            return self._task_is_scoped(entity)
        return False

    def _require_attachments(self, references: Iterable[str]) -> None:
        for reference in references:
            self._available_document(reference)

    def _read_document_text(self, document: Document) -> str:
        path = (self.world_root / document.filename).resolve()
        if not path.is_relative_to(self.world_root):
            raise ToolExecutionError(
                "VALIDATION_ERROR", "document path escapes the world root"
            )
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ToolExecutionError(
                "NOT_FOUND", "document file is unavailable"
            ) from error
        if hashlib.sha256(content).hexdigest() != document.sha256:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "document content hash does not match fixture"
            )
        return parse_document_text(path)

    def _slice_pages(self, text: str, page_range: str) -> str:
        try:
            start_text, end_text = page_range.split("-", 1)
            start, end = int(start_text), int(end_text)
        except ValueError as error:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "page_range must use start-end"
            ) from error
        pages = text.split("\f")
        if start < 1 or end < start:
            raise ToolExecutionError("VALIDATION_ERROR", "page_range is invalid")
        return "\f".join(pages[start - 1 : end])

    def _slice_table_range(
        self, rows: list[dict[str, str]], cell_range: str | None
    ) -> list[dict[str, str]]:
        if cell_range is None:
            return rows
        try:
            start, end = cell_range.split(":", 1)
            start_column, start_row = self._cell_coordinates(start)
            end_column, end_row = self._cell_coordinates(end)
        except ValueError as error:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "range must use A1:F40 syntax"
            ) from error
        if end_column < start_column or end_row < start_row:
            raise ToolExecutionError("VALIDATION_ERROR", "table range is invalid")
        selected: list[dict[str, str]] = []
        start_index = max(0, start_row - 2)
        end_index = max(0, end_row - 1)
        for row in rows[start_index:end_index]:
            columns = list(row)
            selected.append(
                {
                    column: row[column]
                    for column in columns[start_column - 1 : end_column]
                }
            )
        return selected

    def _cell_coordinates(self, value: str) -> tuple[int, int]:
        letters = "".join(
            character for character in value if character.isalpha()
        ).upper()
        digits = "".join(character for character in value if character.isdigit())
        if not letters or not digits:
            raise ValueError("invalid cell")
        column = 0
        for character in letters:
            column = column * 26 + ord(character) - ord("A") + 1
        return column, int(digits)

    def _snippet(self, text: str, query: str) -> str:
        start = text.casefold().find(query)
        return text[max(0, start - 60) : start + len(query) + 120]

    def _irq_for_thread(
        self, thread_id: str, status: str | None = None
    ) -> InformationRequest | None:
        requests = [
            request
            for request in self._list(InformationRequest)
            if request.thread_id == thread_id
            and (status is None or request.status == status)
        ]
        return requests[0] if requests else None

    def _require_provenance(self, references: Iterable[str]) -> None:
        references = list(references)
        if not references:
            raise ToolExecutionError(
                "PROVENANCE_REQUIRED", "at least one provenance reference is required"
            )
        for reference in references:
            self._require_scoped_reference(reference)

    def _require_scoped_reference(self, reference: str) -> None:
        if reference.startswith("act-r"):
            if reference not in self._visible_action_ids:
                raise ToolExecutionError(
                    "SCOPE_VIOLATION",
                    "calculation actions are visible only to the current engine session",
                )
            return
        task = next((item for item in self._list(Task) if item.id == reference), None)
        if task is not None:
            self._require_task_scope(task)
            return
        workpaper = next(
            (item for item in self._list(Workpaper) if item.id == reference), None
        )
        if workpaper is not None:
            self._require_workpaper_scope(workpaper)
            return
        client_id = self._client_for_reference(reference)
        if client_id is None:
            raise ToolExecutionError(
                "NOT_FOUND", f"reference {reference!r} was not found"
            )
        self._require_scope(client_id)
        document = next(
            (item for item in self._list(Document) if item.id == reference), None
        )
        if document is not None and not self._document_is_available(document):
            raise ToolExecutionError(
                "NOT_FOUND", "document is not available at current world time"
            )
        transaction = next(
            (item for item in self._list(BankTransaction) if item.id == reference),
            None,
        )
        if transaction is not None and not self._bank_transaction_is_available(
            transaction.id
        ):
            raise ToolExecutionError(
                "NOT_FOUND", "bank transaction is not available at current world time"
            )

    def _client_for_reference(self, reference: str) -> str | None:
        for document in self._list(Document):
            if document.id == reference:
                return document.client_id
        for thread in self._list(Thread):
            if thread.id == reference:
                return thread.client_id
        for message in self._list(Message):
            if message.id == reference:
                return self._client_for_message(message)
        for request in self._list(InformationRequest):
            if request.id == reference:
                return request.client_id
        for task in self._list(Task):
            if task.id == reference:
                return self._client_for_task(task)
        for workpaper in self._list(Workpaper):
            if workpaper.id == reference:
                return self._client_for_workpaper(workpaper)
        for journal in self._list(Journal):
            if journal.id == reference:
                return self._client_for_entity(journal.entity_id)
        for transaction in self._list(BankTransaction):
            if transaction.id == reference:
                return self._client_for_bank_transaction(transaction)
        for account in self._list(Account):
            if account.id == reference:
                return self._client_for_entity(account.entity_id)
        for period in self._list(AccountingPeriod):
            if period.id == reference:
                return self._client_for_entity(period.entity_id)
        for approval in self._list(Approval):
            if approval.id == reference:
                return self.client_id if self._is_scoped_approval(approval) else None
        return None

    def _save_provenance(
        self,
        subject_ref: str,
        basis_ref: str,
        *,
        relation: ProvenanceRelation = "supported_by",
    ) -> ProvenanceRecord:
        provenance = ProvenanceRecord(
            id=self._new_id("prv"),
            subject_ref=subject_ref,
            basis_ref=basis_ref,
            relation=relation,
        )
        self.store.save(provenance)
        return provenance

    def _is_scoped_approval(self, approval: Approval) -> bool:
        descriptor = approval.action_descriptor
        if isinstance(
            descriptor, (PostJournalDescriptor, PostToClosedPeriodDescriptor)
        ):
            return (
                self._client_for_entity(
                    self._must_load(Journal, descriptor.journal_id).entity_id
                )
                == self.client_id
            )
        if isinstance(descriptor, SendMessageDescriptor):
            return (
                self._client_for_message(
                    self._must_load(Message, descriptor.draft_message_id)
                )
                == self.client_id
            )
        if isinstance(descriptor, ClosePeriodDescriptor):
            return (
                self._client_for_entity(
                    self._must_load(AccountingPeriod, descriptor.period_id).entity_id
                )
                == self.client_id
            )
        return False

    def _first_matching_entity(self, trigger: AfterEntity) -> BaseModel | None:
        entities: tuple[BaseModel, ...]
        if trigger.entity_kind == "information_request":
            entities = self._list(InformationRequest)
        elif trigger.entity_kind == "approval":
            entities = self._list(Approval)
        elif trigger.entity_kind == "message":
            entities = self._list(Message)
        elif trigger.entity_kind == "workpaper":
            entities = self._list(Workpaper)
        else:
            entities = self._list(Task)
        for entity in entities:
            if self._matches_entity(entity, trigger):
                return entity
        return None

    def _matches_entity(self, entity: BaseModel, trigger: AfterEntity) -> bool:
        if not self._entity_is_scoped(entity):
            return False
        match = trigger.match
        client_id: str | None = None
        if isinstance(entity, InformationRequest):
            client_id = entity.client_id
        elif isinstance(entity, Approval):
            if match.approval_kind is not None and entity.kind != match.approval_kind:
                return False
            client_id = self.client_id if self._is_scoped_approval(entity) else None
        elif isinstance(entity, Message):
            client_id = self._client_for_message(entity)
        elif isinstance(entity, Workpaper):
            client_id = self._client_for_workpaper(entity)
        elif isinstance(entity, Task):
            client_id = self._client_for_task(entity)
        if client_id != self.client_id:
            return False
        if match.client_id is not None and client_id != match.client_id:
            return False
        if match.status is not None and getattr(entity, "status", None) != match.status:
            return False
        return (
            match.direction is None
            or getattr(entity, "direction", None) == match.direction
        )

    def _entity_change_time(self, entity: BaseModel) -> datetime | None:
        if isinstance(entity, Message) and entity.world_time is not None:
            return entity.world_time
        if isinstance(entity, Workpaper):
            return entity.finalized_world_time or entity.created_world_time
        entity_id = getattr(entity, "id")
        entity_kind = type(entity).__name__
        for action in reversed(self._list(Action)):
            if any(
                mutation.entity_kind == entity_kind and mutation.entity_id == entity_id
                for mutation in action.mutations
            ):
                return action.world_time_after
        return None

    def _expand_template(self, value: str, entity: BaseModel | None) -> str:
        if entity is None:
            return value
        rendered = value
        for field_name, field_value in entity.model_dump(mode="json").items():
            rendered = rendered.replace(f"${{entity.{field_name}}}", str(field_value))
        return rendered

    def _source_rows(self, source: str) -> list[dict[str, Any]]:
        if source in self._tables:
            return self._tables[source]
        if source == "bank_transactions":
            return [
                {**self._dump(transaction), "source_ref": transaction.id}
                for transaction in self._list(BankTransaction)
                if self._client_for_bank_transaction(transaction) == self.client_id
                and self._bank_transaction_is_available(transaction.id)
            ]
        if source == "ledger_lines":
            ledger_result, _ = self._tool_query_ledger(
                type(
                    "Ledger",
                    (),
                    {"account": None, "period": None, "status": None, "source": None},
                )()
            )
            lines = ledger_result["lines"]
            if isinstance(lines, list) and all(
                isinstance(line, dict) for line in lines
            ):
                return [dict(line) for line in lines]
            raise ToolExecutionError("TYPE_MISMATCH", "ledger source is malformed")
        raise ToolExecutionError("NOT_FOUND", f"data source {source!r} was not found")

    def _matches_predicates(
        self, row: dict[str, Any], predicates: Iterable[dict[str, Any]]
    ) -> bool:
        for predicate in predicates:
            field = predicate.get("field")
            operation = predicate.get("op")
            expected = predicate.get("value")
            if not isinstance(field, str) or field not in row:
                raise ToolExecutionError(
                    "BAD_PREDICATE", "predicate field is unavailable"
                )
            actual = row[field]
            try:
                if operation == "eq":
                    matches = actual == expected
                elif operation == "ne":
                    matches = actual != expected
                elif operation == "gt":
                    matches = actual > expected
                elif operation == "lt":
                    matches = actual < expected
                elif operation == "contains":
                    matches = str(expected).casefold() in str(actual).casefold()
                elif operation == "between":
                    matches = (
                        isinstance(expected, list)
                        and len(expected) == 2
                        and expected[0] <= actual <= expected[1]
                    )
                else:
                    raise KeyError(operation)
            except TypeError as error:
                raise ToolExecutionError(
                    "TYPE_MISMATCH", "predicate compares incompatible values"
                ) from error
            except KeyError as error:
                raise ToolExecutionError(
                    "BAD_PREDICATE", "unsupported predicate operator"
                ) from error
            if not matches:
                return False
        return True

    def _aggregate(self, function: Any, values: list[Any]) -> Any:
        if function == "count":
            return len(values)
        if not values:
            return None
        try:
            if function == "sum":
                return sum(values)
            if function == "min":
                return min(values)
            if function == "max":
                return max(values)
            if function == "avg":
                return sum(values) / len(values)
        except TypeError as error:
            raise ToolExecutionError(
                "TYPE_MISMATCH", "aggregation requires comparable values"
            ) from error
        raise ToolExecutionError("BAD_PREDICATE", "unsupported aggregation function")

    def _resolve_binding(self, binding: Any) -> Decimal:
        if isinstance(binding, (int, float, str)) and not (
            isinstance(binding, str) and "." in binding and binding.startswith("btx-")
        ):
            try:
                return Decimal(str(binding))
            except InvalidOperation as error:
                raise ToolExecutionError(
                    "UNRESOLVED_BINDING", "binding is not numeric"
                ) from error
        if isinstance(binding, str) and binding.endswith(".amount_minor"):
            transaction_id = binding.removesuffix(".amount_minor")
            transaction = self._must_load(BankTransaction, transaction_id)
            self._require_scope(self._client_for_bank_transaction(transaction))
            if not self._bank_transaction_is_available(transaction.id):
                raise ToolExecutionError(
                    "UNRESOLVED_BINDING",
                    "bank transaction is not available at current world time",
                )
            return Decimal(transaction.amount_minor)
        raise ToolExecutionError(
            "UNRESOLVED_BINDING", f"cannot resolve binding {binding!r}"
        )

    def _evaluate_expression(
        self, expression: str, bindings: dict[str, Decimal]
    ) -> int | str:
        try:
            node = ast.parse(expression, mode="eval").body
        except SyntaxError as error:
            raise ToolExecutionError(
                "VALIDATION_ERROR", "expression is invalid"
            ) from error

        def evaluate(current: ast.AST) -> Decimal:
            if isinstance(current, ast.Constant) and isinstance(
                current.value, int | float
            ):
                return Decimal(str(current.value))
            if isinstance(current, ast.Name) and current.id in bindings:
                return bindings[current.id]
            if isinstance(current, ast.UnaryOp) and isinstance(current.op, ast.USub):
                return -evaluate(current.operand)
            if isinstance(current, ast.BinOp):
                left, right = evaluate(current.left), evaluate(current.right)
                if isinstance(current.op, ast.Add):
                    return left + right
                if isinstance(current.op, ast.Sub):
                    return left - right
                if isinstance(current.op, ast.Mult):
                    return left * right
                if isinstance(current.op, ast.Div):
                    if right == 0:
                        raise ToolExecutionError("DIV_ZERO", "division by zero")
                    return left / right
            raise ToolExecutionError(
                "VALIDATION_ERROR",
                "expression may use only numeric arithmetic and named bindings",
            )

        result = evaluate(node)
        return int(result) if result == result.to_integral() else format(result, "f")

    def _dataset_rows(self, source: Any) -> list[dict[str, Any]]:
        if isinstance(source, str):
            return self._source_rows(source)
        if isinstance(source, list) and all(isinstance(row, dict) for row in source):
            return [dict(row) for row in source]
        raise ToolExecutionError(
            "TYPE_MISMATCH", "dataset must be a table reference or list of rows"
        )

    def _index_rows(
        self, rows: list[dict[str, Any]], keys: list[str]
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(field) for field in keys)
            if None in key or key in indexed:
                raise ToolExecutionError(
                    "KEY_NOT_UNIQUE",
                    "dataset keys are missing or non-unique",
                    {"key": key},
                )
            indexed[key] = row
        return indexed

    def _equal_with_tolerance(self, left: Any, right: Any, tolerance: int) -> bool:
        if isinstance(left, int | float) and isinstance(right, int | float):
            return abs(left - right) <= tolerance
        return bool(left == right)
