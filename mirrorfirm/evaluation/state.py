"""Read-only evaluation helpers shared by deterministic and safety checks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

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
    """Resolve persisted identifiers to their owning client without mutating state."""

    reference_clients: dict[str, str]
    all_record_ids: frozenset[str]

    @classmethod
    def from_world(cls, world: WorldView) -> ClientScopeIndex:
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
        reference_clients: dict[str, str] = {}
        all_record_ids: set[str] = set()

        def add(reference: str, client_id: str | None) -> None:
            all_record_ids.add(reference)
            if client_id is not None:
                reference_clients[reference] = client_id

        for client in clients.values():
            add(client.id, client.id)
            add(client.entity.id, client.id)
        for engagement in engagements.values():
            add(engagement.id, engagement.client_id)
        for account_id, client_id in client_by_account.items():
            add(account_id, client_id)
        for period_id, client_id in client_by_period.items():
            add(period_id, client_id)
        for bank_id, client_id in client_by_bank.items():
            add(bank_id, client_id)
        for transaction in world.list(BankTransaction):
            add(transaction.id, client_by_bank.get(transaction.bank_account_id))
        for document in world.list(Document):
            add(document.id, document.client_id)
        for thread in threads.values():
            add(thread.id, thread.client_id)
        for message in world.list(Message):
            message_thread = threads.get(message.thread_id)
            add(
                message.id,
                message_thread.client_id if message_thread is not None else None,
            )
        for request in world.list(InformationRequest):
            add(request.id, request.client_id)
        for task in world.list(Task):
            task_engagement = engagements.get(task.engagement_id)
            add(
                task.id,
                task_engagement.client_id if task_engagement is not None else None,
            )
        for workpaper in world.list(Workpaper):
            workpaper_engagement = engagements.get(workpaper.engagement_id)
            add(
                workpaper.id,
                workpaper_engagement.client_id
                if workpaper_engagement is not None
                else None,
            )
        for journal in world.list(Journal):
            add(journal.id, client_by_entity.get(journal.entity_id))
        for approval in world.list(Approval):
            client_id = _approval_client_id(approval, reference_clients)
            add(approval.id, client_id)
        for record in world.list(ProvenanceRecord):
            all_record_ids.add(record.id)
        return cls(
            reference_clients=reference_clients,
            all_record_ids=frozenset(all_record_ids),
        )

    def client_for(self, reference: str) -> str | None:
        """Return a client owner when the reference identifies a client-owned record."""

        return self.reference_clients.get(reference)

    def has_reference(self, reference: str) -> bool:
        """Whether a reference exists in the persisted logical world."""

        return reference in self.all_record_ids


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
