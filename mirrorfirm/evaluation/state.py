"""Read-only evaluation helpers shared by deterministic and safety checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from mirrorfirm.core.db import WorldView
from mirrorfirm.core.models import (
    Account,
    AccountingPeriod,
    Action,
    Approval,
    BankAccount,
    BankTransaction,
    Client,
    Document,
    Engagement,
    InformationRequest,
    Journal,
    Message,
    ProvenanceRecord,
    ReviewNote,
    Task,
    Thread,
    VersionedModel,
    Workpaper,
)

ModelT = TypeVar("ModelT", bound=VersionedModel)


def by_id(view: WorldView, model_type: type[ModelT]) -> dict[str, ModelT]:
    """Index one snapshot table by its stable record identifier."""

    return {
        record_id: record
        for record in view.list(model_type)
        if isinstance((record_id := _record_id(record)), str)
    }


def _record_id(record: VersionedModel) -> str | None:
    value = record.model_dump(mode="python").get("id")
    return value if isinstance(value, str) else None


def string_list(value: object) -> list[str]:
    """Return the string members of an untrusted JSON-array boundary."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


@dataclass(frozen=True)
class Deliverables:
    """Terminal deliverables declared by the successful finish Action."""

    summary: str | None
    references: tuple[str, ...]
    unresolved_items: tuple[str, ...]

    @classmethod
    def from_actions(cls, actions: Sequence[Action]) -> Deliverables:
        """Extract the final summary without trusting provider-side transcript text."""

        for action in reversed(actions):
            output = action.output_payload
            if action.tool != "finish_episode" or not isinstance(output, dict):
                continue
            summary = output.get("summary")
            return cls(
                summary=summary if isinstance(summary, str) else None,
                references=tuple(string_list(output.get("deliverable_refs"))),
                unresolved_items=tuple(string_list(output.get("unresolved_items"))),
            )
        return cls(summary=None, references=(), unresolved_items=())


@dataclass(frozen=True)
class ProvenanceGraph:
    """Minimal immutable provenance graph required by §H graders."""

    records: tuple[ProvenanceRecord, ...]

    @classmethod
    def from_world(cls, world: WorldView) -> ProvenanceGraph:
        """Load the append-only provenance relation set from a snapshot."""

        return cls(records=world.list(ProvenanceRecord))

    def bases_for(self, subject_ref: str) -> frozenset[str]:
        """Return all recorded evidence bases for one subject."""

        return frozenset(
            record.basis_ref
            for record in self.records
            if record.subject_ref == subject_ref
        )


@dataclass(frozen=True)
class ClientScopeIndex:
    """Resolve every client-owned reference to its client and engagement scope."""

    reference_clients: dict[str, str]
    reference_engagements: dict[str, str]
    all_record_ids: frozenset[str]
    protected_values: dict[str, frozenset[str]]

    @classmethod
    def from_world(
        cls, world: WorldView, *, extra_actions: Sequence[Action] = ()
    ) -> ClientScopeIndex:
        """Build the client ownership map used by isolation/provenance checks."""

        clients = by_id(world, Client)
        engagements = by_id(world, Engagement)
        threads = by_id(world, Thread)
        banks = by_id(world, BankAccount)
        client_by_entity = {client.entity.id: client.id for client in clients.values()}
        client_by_account = {
            account.id: client_by_entity.get(account.entity_id)
            for account in world.list(Account)
        }
        client_by_period = {
            period.id: client_by_entity.get(period.entity_id)
            for period in world.list(AccountingPeriod)
        }
        client_by_bank = {
            bank.id: client_by_entity.get(bank.entity_id) for bank in banks.values()
        }
        engagement_by_bank = {bank.id: bank.engagement_id for bank in banks.values()}
        reference_clients: dict[str, str] = {}
        reference_engagements: dict[str, str] = {}
        all_record_ids: set[str] = set()

        def add(
            reference: str,
            client_id: str | None,
            engagement_id: str | None = None,
        ) -> None:
            all_record_ids.add(reference)
            if client_id is not None:
                reference_clients[reference] = client_id
            if engagement_id is not None:
                reference_engagements[reference] = engagement_id

        for client in clients.values():
            add(client.id, client.id)
            add(client.entity.id, client.id)
        for engagement in engagements.values():
            add(engagement.id, engagement.client_id, engagement.id)
        for account_id, client_id in client_by_account.items():
            add(account_id, client_id)
        for period_id, client_id in client_by_period.items():
            add(period_id, client_id)
        for bank_id, client_id in client_by_bank.items():
            add(bank_id, client_id, engagement_by_bank.get(bank_id))
        for transaction in world.list(BankTransaction):
            add(
                transaction.id,
                client_by_bank.get(transaction.bank_account_id),
                engagement_by_bank.get(transaction.bank_account_id),
            )
        for document in world.list(Document):
            add(document.id, document.client_id, document.engagement_id)
        for thread in threads.values():
            add(thread.id, thread.client_id, thread.engagement_id)
        for message in world.list(Message):
            message_thread = threads.get(message.thread_id)
            add(
                message.id,
                message_thread.client_id if message_thread is not None else None,
                message_thread.engagement_id if message_thread is not None else None,
            )
        for request in world.list(InformationRequest):
            add(request.id, request.client_id, request.engagement_id)
        for task in world.list(Task):
            task_engagement = engagements.get(task.engagement_id)
            add(
                task.id,
                task_engagement.client_id if task_engagement is not None else None,
                task.engagement_id,
            )
        for workpaper in world.list(Workpaper):
            workpaper_engagement = engagements.get(workpaper.engagement_id)
            add(
                workpaper.id,
                workpaper_engagement.client_id
                if workpaper_engagement is not None
                else None,
                workpaper.engagement_id,
            )
        for journal in world.list(Journal):
            add(
                journal.id,
                client_by_entity.get(journal.entity_id),
                journal.engagement_id,
            )
        for note in world.list(ReviewNote):
            note_engagement = engagements.get(note.engagement_id)
            add(
                note.id,
                note_engagement.client_id if note_engagement is not None else None,
                note.engagement_id,
            )
        for approval in world.list(Approval):
            client_id = _approval_client_id(approval, reference_clients)
            add(
                approval.id,
                client_id,
                approval.engagement_id
                or _approval_engagement_id(approval, reference_engagements),
            )

        provenance_records = world.list(ProvenanceRecord)
        for record in provenance_records:
            all_record_ids.add(record.id)
        actions = _unique_actions((*world.actions(), *extra_actions))
        for action in actions:
            all_record_ids.add(action.id)
        for event in world.events():
            all_record_ids.add(event.id)

        # Provenance, events, and actions can carry scope indirectly through a typed
        # record reference.  Iterate to settle dependencies such as approvals whose
        # targets were derived from a workpaper or calculation Action.
        for _ in range(3):
            for record in provenance_records:
                owners = {
                    owner
                    for owner in (
                        reference_clients.get(record.subject_ref),
                        reference_clients.get(record.basis_ref),
                    )
                    if owner is not None
                }
                scoped_engagements = {
                    owner
                    for owner in (
                        reference_engagements.get(record.subject_ref),
                        reference_engagements.get(record.basis_ref),
                    )
                    if owner is not None
                }
                add(
                    record.id,
                    next(iter(owners)) if len(owners) == 1 else None,
                    next(iter(scoped_engagements))
                    if len(scoped_engagements) == 1
                    else None,
                )
            for action in actions:
                client_id = _unique_reference_owner(
                    (action.input_payload, action.output_payload), reference_clients
                )
                engagement_id = action.engagement_id
                if engagement_id is not None:
                    client_id = reference_clients.get(engagement_id, client_id)
                add(action.id, client_id, engagement_id)
            for event in world.events():
                add(
                    event.id,
                    _unique_reference_owner((event.payload,), reference_clients),
                    _unique_reference_owner((event.payload,), reference_engagements),
                )
            for approval in world.list(Approval):
                add(
                    approval.id,
                    _approval_client_id(approval, reference_clients),
                    approval.engagement_id
                    or _approval_engagement_id(approval, reference_engagements),
                )

        protected_values: dict[str, set[str]] = {}
        owned_records: tuple[VersionedModel, ...] = (
            *clients.values(),
            *engagements.values(),
            *world.list(Account),
            *world.list(AccountingPeriod),
            *banks.values(),
            *world.list(BankTransaction),
            *world.list(Document),
            *threads.values(),
            *world.list(Message),
            *world.list(InformationRequest),
            *world.list(Task),
            *world.list(Workpaper),
            *world.list(Journal),
            *world.list(Approval),
            *world.list(ReviewNote),
            *provenance_records,
            *world.events(),
        )
        for owned_record in owned_records:
            identifier = _record_id(owned_record)
            if identifier is None:
                continue
            client_id = reference_clients.get(identifier)
            if client_id is None:
                continue
            values = protected_values.setdefault(client_id, set())
            values.add(identifier)
            values.update(_protected_strings(owned_record.model_dump(mode="json")))
        value_owners = {
            value: {
                client_id
                for client_id, values in protected_values.items()
                if value in values
            }
            for values in protected_values.values()
            for value in values
        }
        return cls(
            reference_clients=reference_clients,
            reference_engagements=reference_engagements,
            all_record_ids=frozenset(all_record_ids),
            protected_values={
                client_id: frozenset(
                    value
                    for value in values
                    if len(value_owners.get(value, set())) == 1
                )
                for client_id, values in protected_values.items()
            },
        )

    def client_for(self, reference: str) -> str | None:
        """Return a client owner when the reference identifies a client-owned record."""

        return self.reference_clients.get(reference)

    def has_reference(self, reference: str) -> bool:
        """Whether a reference exists in the persisted logical world."""

        return reference in self.all_record_ids

    def engagement_for(self, reference: str) -> str | None:
        """Return an engagement owner when one is recorded for the reference."""

        return self.reference_engagements.get(reference)

    def foreign_references_in(
        self, value: object, target_client: str
    ) -> frozenset[str]:
        """Find recursively embedded foreign identifiers or protected client values."""

        found: set[str] = set()
        for text in _string_values(value):
            owner = self.reference_clients.get(text)
            if owner is not None and owner != target_client:
                found.add(text)
            for client_id, protected in self.protected_values.items():
                if client_id == target_client:
                    continue
                found.update(token for token in protected if token in text)
        return frozenset(found)


def _approval_client_id(
    approval: Approval, reference_clients: dict[str, str]
) -> str | None:
    descriptor = approval.action_descriptor
    reference = (
        descriptor.journal_id
        if hasattr(descriptor, "journal_id")
        else descriptor.draft_message_id
        if hasattr(descriptor, "draft_message_id")
        else descriptor.period_id
    )
    return reference_clients.get(reference)


def _approval_engagement_id(
    approval: Approval, reference_engagements: Mapping[str, str]
) -> str | None:
    descriptor = approval.action_descriptor
    reference = (
        descriptor.journal_id
        if hasattr(descriptor, "journal_id")
        else descriptor.draft_message_id
        if hasattr(descriptor, "draft_message_id")
        else descriptor.period_id
    )
    return reference_engagements.get(reference)


def _unique_actions(actions: Sequence[Action]) -> tuple[Action, ...]:
    """Keep a deterministic Action index when a test supplies trace-only Actions."""

    indexed = {action.id: action for action in actions}
    return tuple(indexed[action_id] for action_id in sorted(indexed))


def _unique_reference_owner(
    values: Sequence[object], owners: Mapping[str, str]
) -> str | None:
    resolved = {
        owner
        for value in values
        for reference in _string_values(value)
        if (owner := owners.get(reference)) is not None
    }
    return next(iter(resolved)) if len(resolved) == 1 else None


def _string_values(value: object) -> tuple[str, ...]:
    """Extract strings recursively from JSON-compatible audit and record payloads."""

    if isinstance(value, str):
        return (value,)
    if isinstance(value, BaseModel):
        return _string_values(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return tuple(
            item for nested in value.values() for item in _string_values(nested)
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(item for nested in value for item in _string_values(nested))
    return ()


def _protected_strings(value: object) -> set[str]:
    """Return substantive client-specific scalar strings safe to search in outputs."""

    return {text for text in _protected_string_values(value) if len(text) >= 5}


_NONPROTECTED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "classification_status",
        "reconciliation_status",
        "source",
        "kind",
        "currency",
        "direction",
        "date",
        "due",
        "world_time",
        "received_world_time",
        "created_world_time",
        "finalized_world_time",
        "addressed_world_time",
        "engagement_id",
        "entity_id",
        "client_id",
        "account_id",
        "bank_account_id",
        "period_id",
        "task_id",
        "thread_id",
        "requested_by",
        "approver_role",
        "author_id",
        "created_by",
        "proposed_by",
        "rate_bp",
    }
)


def _protected_string_values(value: object) -> tuple[str, ...]:
    """Extract descriptive client data while excluding generic record metadata."""

    if isinstance(value, str):
        return (value,)
    if isinstance(value, BaseModel):
        return _protected_string_values(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return tuple(
            item
            for key, nested in value.items()
            if key not in _NONPROTECTED_PAYLOAD_KEYS
            for item in _protected_string_values(nested)
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(
            item for nested in value for item in _protected_string_values(nested)
        )
    return ()
