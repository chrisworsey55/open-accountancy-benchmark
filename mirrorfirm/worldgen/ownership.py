"""Compile-time engagement ownership graph validation for authored worlds."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import get_args, get_origin

from mirrorfirm.core.models import (
    Account,
    AccountingPeriod,
    Action,
    AfterEntity,
    Approval,
    BankAccount,
    BankReconWorkpaper,
    BankTransaction,
    ClassificationSummaryWorkpaper,
    Client,
    Document,
    Engagement,
    Event,
    InformationRequest,
    Journal,
    Message,
    Person,
    ProvenanceRecord,
    QueryLogWorkpaper,
    ReviewNote,
    Task,
    Thread,
    VersionedModel,
    Workpaper,
)

_TEMPLATE_PATTERN = re.compile(
    r"\$\{(?P<binding>[a-z_][a-z0-9_]*)\.(?P<path>[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\}"
)
_ENTITY_TEMPLATE_MODELS: dict[str, type[VersionedModel]] = {
    "information_request": InformationRequest,
    "approval": Approval,
    "message": Message,
    "workpaper": Workpaper,
    "task": Task,
}
_ENTITY_IDENTIFIER_KINDS = {
    "information_request": "irq",
    "approval": "apv",
    "message": "msg",
    "workpaper": "wpp",
    "task": "tsk",
}
_REFERENCE_FIELD_KINDS = {
    "client_id": "cli",
    "engagement_id": "eng",
    "period_id": "prd",
    "account_id": "acc",
    "bank_account_id": "bnk",
    "bank_transaction_id": "btx",
    "document_id": "doc",
    "doc_id": "doc",
    "thread_id": "thr",
    "thread_ref": "thr",
    "message_id": "msg",
    "draft_message_id": "msg",
    "information_request_id": "irq",
    "task_id": "tsk",
    "journal_id": "jnl",
    "workpaper_id": "wpp",
    "approval_id": "apv",
    "event_id": "evt",
    "provenance_id": "prv",
}
_EVENT_DESTINATION_KINDS = {
    ("payload", "thread_ref"): "thr",
    ("payload", "bank_account_id"): "bnk",
    ("payload", "task_id"): "tsk",
    ("payload", "attachment_fixture_refs", "*"): "doc",
    ("payload", "txn_fixture_refs", "*"): "btx",
    ("trigger", "match", "client_id"): "cli",
    ("trigger", "world_time"): "datetime",
}


@dataclass(frozen=True)
class Ownership:
    """The client and, where applicable, engagement which owns a record."""

    client_id: str
    engagement_id: str | None


def event_template_violations(
    event: Event,
    *,
    records: Iterable[VersionedModel] = (),
    owners: dict[str, Ownership] | None = None,
) -> tuple[str, ...]:
    """Return structural errors for templates authored in one event.

    Events may bind only the entity selected by an ``after_entity`` trigger.  The
    compiler can validate that binding's model shape and semantic identifier kind
    before an event is ever eligible to fire; the engine checks the resulting live
    record and scope again when expanding it.
    """

    errors: list[str] = []
    authored_records = tuple(records)
    record_owners = owners or {}
    for path, value in _template_strings(event.model_dump(mode="json")):
        matches = tuple(_TEMPLATE_PATTERN.finditer(value))
        if "${" in value and not _templates_cover_all_markers(value, matches):
            errors.append(
                f"event {event.id!r} has invalid template syntax at {_format_path(path)}"
            )
            continue
        for match in matches:
            binding = match.group("binding")
            source_path = tuple(match.group("path").split("."))
            if not isinstance(event.trigger, AfterEntity):
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} has no declared binding"
                )
                continue
            if path[:2] == ("trigger", "match"):
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} is circular with its trigger match"
                )
                continue
            if binding != "entity":
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} uses unknown binding {binding!r}"
                )
                continue
            source_kind = _template_source_kind(event.trigger.entity_kind, source_path)
            if source_kind is None:
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} references missing bound field "
                    f"{'/'.join(source_path)!r}"
                )
                continue
            destination_kind = _event_destination_kind(path)
            if destination_kind is not None and not _template_kinds_compatible(
                source_kind, destination_kind
            ):
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} resolves "
                    f"{source_kind!r} where {destination_kind!r} is required"
                )
                continue
            for bound in _matching_bound_entities(
                event, authored_records, record_owners
            ):
                resolved = _bound_template_value(bound, source_path)
                if _template_value_is_present(resolved):
                    continue
                errors.append(
                    f"event {event.id!r} template at {_format_path(path)} bound field "
                    f"{'/'.join(source_path)!r} resolves to an empty value on "
                    f"{type(bound).__name__} {getattr(bound, 'id', '<unknown>')!r}"
                )
    return tuple(errors)


def _matching_bound_entities(
    event: Event,
    records: Iterable[VersionedModel],
    owners: dict[str, Ownership],
) -> tuple[VersionedModel, ...]:
    """Return authored records that an ``after_entity`` trigger can bind now.

    Future records are necessarily unavailable during compilation, but an authored
    record that already satisfies a trigger must be safe to expand immediately.  This
    catches nullable and empty values without inventing a value for future events.
    """

    if not isinstance(event.trigger, AfterEntity):
        return ()
    trigger = event.trigger
    model = _ENTITY_TEMPLATE_MODELS[trigger.entity_kind]
    event_owner = owners.get(event.id)
    matches: list[VersionedModel] = []
    for record in records:
        if not isinstance(record, model):
            continue
        if (
            trigger.match.status is not None
            and getattr(record, "status", None) != trigger.match.status
        ):
            continue
        if (
            trigger.match.direction is not None
            and getattr(record, "direction", None) != trigger.match.direction
        ):
            continue
        if (
            trigger.match.approval_kind is not None
            and getattr(record, "kind", None) != trigger.match.approval_kind
        ):
            continue
        record_id = getattr(record, "id", None)
        if not isinstance(record_id, str):
            continue
        owner = owners.get(record_id)
        client_id = getattr(record, "client_id", None)
        if not isinstance(client_id, str) and owner is not None:
            client_id = owner.client_id
        if trigger.match.client_id is not None and client_id != trigger.match.client_id:
            continue
        if event_owner is not None and client_id != event_owner.client_id:
            continue
        if (
            event_owner is not None
            and owner is not None
            and owner.engagement_id != event_owner.engagement_id
        ):
            continue
        matches.append(record)
    return tuple(matches)


def _bound_template_value(
    entity: VersionedModel, path: tuple[str, ...]
) -> object | None:
    """Resolve a validated field path on one actually matching authored entity."""

    value: object = entity
    for part in path:
        if isinstance(value, VersionedModel):
            if part not in type(value).model_fields:
                return None
            value = getattr(value, part)
        elif isinstance(value, dict):
            try:
                value = value[part]
            except KeyError:
                return None
        else:
            return None
    return value


def _template_value_is_present(value: object | None) -> bool:
    """Reject absent, blank, and empty template values before runtime expansion."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | dict):
        return bool(value)
    return True


def _template_strings(
    value: object, path: tuple[str, ...] = ()
) -> Iterable[tuple[tuple[str, ...], str]]:
    """Yield every string containing a template marker, including nested fields."""

    if isinstance(value, str):
        if "${" in value:
            yield path, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _template_strings(item, (*path, str(index)))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield from _template_strings(item, (*path, key))


def _templates_cover_all_markers(
    value: str, matches: tuple[re.Match[str], ...]
) -> bool:
    """Reject incomplete, nested, and otherwise unparsed ``${...}`` expressions."""

    covered = [False] * len(value)
    for match in matches:
        for index in range(match.start(), match.end()):
            covered[index] = True
    return all(
        covered[index] for index in range(len(value)) if value.startswith("${", index)
    )


def _template_source_kind(entity_kind: str, path: tuple[str, ...]) -> str | None:
    """Resolve a declared bound entity field and its semantic scalar kind."""

    model = _ENTITY_TEMPLATE_MODELS[entity_kind]
    annotation: object = model
    for field_name in path:
        models = _model_variants(annotation)
        fields = [candidate.model_fields.get(field_name) for candidate in models]
        if not fields or any(field is None for field in fields):
            return None
        first_field = fields[0]
        assert first_field is not None  # narrowed by the complete-variant check above
        annotation = first_field.annotation
    last = path[-1]
    if last == "id":
        return _ENTITY_IDENTIFIER_KINDS[entity_kind]
    if last in _REFERENCE_FIELD_KINDS:
        return _REFERENCE_FIELD_KINDS[last]
    if last.endswith("_time") or last in {"world_time", "created_world_time"}:
        return "datetime"
    return "scalar"


def _model_variants(annotation: object) -> tuple[type[VersionedModel], ...]:
    """Return Pydantic model members from an annotation, including unions."""

    if isinstance(annotation, type) and issubclass(annotation, VersionedModel):
        return (annotation,)
    origin = get_origin(annotation)
    if origin is None:
        return ()
    return tuple(
        variant
        for member in get_args(annotation)
        for variant in _model_variants(member)
    )


def _event_destination_kind(path: tuple[str, ...]) -> str | None:
    """Return the record family required by a templated event destination."""

    normalised = tuple("*" if item.isdigit() else item for item in path)
    return _EVENT_DESTINATION_KINDS.get(normalised)


def _template_kinds_compatible(source: str, destination: str) -> bool:
    """Require exact record families for event reference destinations."""

    return source == destination or destination == "scalar"


def _format_path(path: tuple[str, ...]) -> str:
    """Render an authored model path in a concise validation diagnostic."""

    return ".".join(path)


def ownership_violations(records: Iterable[VersionedModel]) -> tuple[str, ...]:
    """Return all client/engagement ownership errors in an authored record graph.

    Pydantic keeps optional ownership fields for compatibility with old serialized
    records, but an authored compiled world has no such compatibility escape hatch:
    every client-operational record must either carry a valid engagement or inherit it
    through an unambiguous scoped parent.
    """

    typed = tuple(records)
    clients = {record.id: record for record in typed if isinstance(record, Client)}
    engagements = {
        record.id: record for record in typed if isinstance(record, Engagement)
    }
    entities = {
        record.entity.id: record.id for record in typed if isinstance(record, Client)
    }
    periods = {
        record.id: record for record in typed if isinstance(record, AccountingPeriod)
    }
    accounts = {record.id: record for record in typed if isinstance(record, Account)}
    people = {record.id: record for record in typed if isinstance(record, Person)}
    bank_accounts = {
        record.id: record for record in typed if isinstance(record, BankAccount)
    }
    bank_transactions = {
        record.id: record for record in typed if isinstance(record, BankTransaction)
    }
    documents = {record.id: record for record in typed if isinstance(record, Document)}
    threads = {record.id: record for record in typed if isinstance(record, Thread)}
    messages = {record.id: record for record in typed if isinstance(record, Message)}
    requests = {
        record.id: record for record in typed if isinstance(record, InformationRequest)
    }
    tasks = {record.id: record for record in typed if isinstance(record, Task)}
    workpapers = {
        record.id: record for record in typed if isinstance(record, Workpaper)
    }
    notes = {record.id: record for record in typed if isinstance(record, ReviewNote)}
    approvals = {record.id: record for record in typed if isinstance(record, Approval)}
    events = {record.id: record for record in typed if isinstance(record, Event)}
    actions = {record.id: record for record in typed if isinstance(record, Action)}
    provenance = {
        record.id: record for record in typed if isinstance(record, ProvenanceRecord)
    }
    del events, provenance

    errors: list[str] = []
    owners: dict[str, Ownership] = {}

    def client_for_entity(entity_id: str, context: str) -> str | None:
        client_id = entities.get(entity_id)
        if client_id is None:
            errors.append(f"{context} has unknown entity {entity_id!r}")
        return client_id

    def explicit_owner(
        identifier: str,
        context: str,
        client_id: str | None,
        engagement_id: str | None,
    ) -> Ownership | None:
        if client_id is None:
            if engagement_id is not None:
                errors.append(
                    f"{context} has engagement ownership without a client owner"
                )
            return None
        if client_id not in clients:
            errors.append(f"{context} has unknown client {client_id!r}")
            return None
        if engagement_id is None:
            errors.append(f"{context} is missing required engagement ownership")
            return None
        engagement = engagements.get(engagement_id)
        if engagement is None:
            errors.append(
                f"{context} has dangling engagement ownership {engagement_id!r}"
            )
            return None
        if engagement.client_id != client_id:
            errors.append(
                f"{context} engagement {engagement_id!r} belongs to client "
                f"{engagement.client_id!r}, not {client_id!r}"
            )
            return None
        owner = Ownership(client_id, engagement_id)
        owners[identifier] = owner
        return owner

    def engagement_owner(
        identifier: str, context: str, engagement_id: str | None
    ) -> Ownership | None:
        if engagement_id is None:
            errors.append(f"{context} is missing required engagement ownership")
            return None
        engagement = engagements.get(engagement_id)
        if engagement is None:
            errors.append(
                f"{context} has dangling engagement ownership {engagement_id!r}"
            )
            return None
        if engagement.client_id not in clients:
            errors.append(
                f"{context} engagement {engagement_id!r} has unknown client "
                f"{engagement.client_id!r}"
            )
            return None
        owner = Ownership(engagement.client_id, engagement.id)
        owners[identifier] = owner
        return owner

    def client_owner(
        identifier: str, context: str, client_id: str | None
    ) -> Ownership | None:
        if client_id is None or client_id not in clients:
            errors.append(f"{context} has unknown client ownership {client_id!r}")
            return None
        owner = Ownership(client_id, None)
        owners[identifier] = owner
        return owner

    def compatible(
        context: str,
        owner: Ownership | None,
        reference: str | None,
        *,
        allow_client_level: bool = True,
    ) -> None:
        if owner is None or reference is None:
            return
        if "${" in reference:
            errors.append(f"{context} has unresolved template {reference!r}")
            return
        target = owners.get(reference)
        if target is None:
            errors.append(f"{context} cannot resolve ownership for {reference!r}")
            return
        if target.client_id != owner.client_id:
            errors.append(f"{context} crosses client ownership via {reference!r}")
        elif target.engagement_id is None:
            if not allow_client_level:
                errors.append(
                    f"{context} needs engagement-owned reference {reference!r}"
                )
        elif target.engagement_id != owner.engagement_id:
            errors.append(f"{context} crosses engagement ownership via {reference!r}")

    def compatible_many(
        context: str, owner: Ownership | None, references: Iterable[str]
    ) -> None:
        for reference in references:
            compatible(context, owner, reference)

    # Client and entity records are client-level anchors.  Accounts are intentionally
    # client-level: their ownership is shared across that client's engagement history.
    for client in clients.values():
        client_owner(client.id, f"client {client.id!r}", client.id)
        owners[client.entity.id] = Ownership(client.id, None)
    for account in accounts.values():
        client_id = client_for_entity(account.entity_id, f"account {account.id!r}")
        client_owner(account.id, f"account {account.id!r}", client_id)

    # A period is engagement-derived and cannot be silently shared between scopes.
    for period in periods.values():
        client_id = client_for_entity(period.entity_id, f"period {period.id!r}")
        period_engagements = [
            engagement
            for engagement in engagements.values()
            if period.id in engagement.period_ids
        ]
        if len(period_engagements) != 1:
            errors.append(
                f"period {period.id!r} has ambiguous or missing engagement ownership"
            )
            continue
        engagement = period_engagements[0]
        explicit_owner(period.id, f"period {period.id!r}", client_id, engagement.id)

    for bank_account in bank_accounts.values():
        client_id = client_for_entity(
            bank_account.entity_id, f"bank account {bank_account.id!r}"
        )
        owner = explicit_owner(
            bank_account.id,
            f"bank account {bank_account.id!r}",
            client_id,
            bank_account.engagement_id,
        )
        compatible(
            f"bank account {bank_account.id!r}", owner, bank_account.ledger_account_id
        )
    for transaction in bank_transactions.values():
        bank_owner = owners.get(transaction.bank_account_id)
        if bank_owner is None:
            errors.append(
                f"bank transaction {transaction.id!r} has no owned bank account"
            )
            continue
        owners[transaction.id] = bank_owner

    for document in documents.values():
        explicit_owner(
            document.id,
            f"document {document.id!r}",
            document.client_id,
            document.engagement_id,
        )
    for thread in threads.values():
        explicit_owner(
            thread.id,
            f"thread {thread.id!r}",
            thread.client_id,
            thread.engagement_id,
        )
    for journal in journals_from(typed):
        client_id = client_for_entity(journal.entity_id, f"journal {journal.id!r}")
        owner = explicit_owner(
            journal.id, f"journal {journal.id!r}", client_id, journal.engagement_id
        )
        for line in journal.lines:
            compatible(f"journal {journal.id!r}", owner, line.account_id)
            compatible(f"journal {journal.id!r}", owner, line.bank_transaction_id)
    for task in tasks.values():
        engagement_owner(task.id, f"task {task.id!r}", task.engagement_id)
    for workpaper in workpapers.values():
        engagement_owner(
            workpaper.id, f"workpaper {workpaper.id!r}", workpaper.engagement_id
        )
    for note in notes.values():
        engagement_owner(note.id, f"review note {note.id!r}", note.engagement_id)
    for request in requests.values():
        explicit_owner(
            request.id,
            f"information request {request.id!r}",
            request.client_id,
            request.engagement_id,
        )
    for approval in approvals.values():
        engagement_owner(
            approval.id, f"approval {approval.id!r}", approval.engagement_id
        )
    for event in (record for record in typed if isinstance(record, Event)):
        engagement_owner(event.id, f"event {event.id!r}", event.engagement_id)
    for action in actions.values():
        engagement_owner(action.id, f"action {action.id!r}", action.engagement_id)

    # Child records and embedded references must agree with their owning scope.
    for message in messages.values():
        owner = owners.get(message.thread_id)
        if owner is None:
            errors.append(f"message {message.id!r} has no owned thread")
            continue
        owners[message.id] = owner
        for person_id in (message.sender, *message.recipients):
            person = people.get(person_id)
            if (
                person is not None
                and person.client_id is not None
                and person.client_id != owner.client_id
            ):
                errors.append(
                    f"message {message.id!r} crosses client ownership via person {person_id!r}"
                )
        compatible_many(f"message {message.id!r}", owner, message.attachments)
    for thread in threads.values():
        compatible_many(
            f"thread {thread.id!r}", owners.get(thread.id), thread.message_ids
        )
    for request in requests.values():
        owner = owners.get(request.id)
        compatible(f"information request {request.id!r}", owner, request.thread_id)
        for item in request.items:
            compatible_many(f"information request {request.id!r}", owner, item.refs)
    for task in tasks.values():
        compatible(f"task {task.id!r}", owners.get(task.id), task.blocked_on)
    for workpaper in workpapers.values():
        owner = owners.get(workpaper.id)
        compatible(f"workpaper {workpaper.id!r}", owner, workpaper.task_id)
        body = workpaper.body
        if isinstance(body, BankReconWorkpaper):
            compatible(f"workpaper {workpaper.id!r}", owner, body.bank_account_id)
            compatible(f"workpaper {workpaper.id!r}", owner, body.period_id)
            for recon_item in body.outstanding:
                compatible_many(
                    f"workpaper {workpaper.id!r}", owner, recon_item.provenance_refs
                )
            for unresolved_item in body.unresolved:
                compatible_many(
                    f"workpaper {workpaper.id!r}",
                    owner,
                    unresolved_item.provenance_refs,
                )
        elif isinstance(body, ClassificationSummaryWorkpaper):
            compatible(f"workpaper {workpaper.id!r}", owner, body.period_id)
            for row in body.rows:
                compatible(
                    f"workpaper {workpaper.id!r}", owner, row.bank_transaction_id
                )
                compatible(f"workpaper {workpaper.id!r}", owner, row.account_id)
                compatible_many(
                    f"workpaper {workpaper.id!r}", owner, row.provenance_refs
                )
        elif isinstance(body, QueryLogWorkpaper):
            for query_item in body.items:
                compatible_many(
                    f"workpaper {workpaper.id!r}", owner, query_item.provenance_refs
                )
    for note in notes.values():
        compatible(f"review note {note.id!r}", owners.get(note.id), note.target_ref)
    for journal in journals_from(typed):
        compatible(
            f"journal {journal.id!r}", owners.get(journal.id), journal.approval_id
        )
    for approval in approvals.values():
        owner = owners.get(approval.id)
        compatible_many(f"approval {approval.id!r}", owner, approval.provenance_refs)
        descriptor = approval.action_descriptor
        if hasattr(descriptor, "journal_id"):
            compatible(f"approval {approval.id!r}", owner, descriptor.journal_id)
        if hasattr(descriptor, "period_id"):
            compatible(f"approval {approval.id!r}", owner, descriptor.period_id)
        if hasattr(descriptor, "draft_message_id"):
            compatible(f"approval {approval.id!r}", owner, descriptor.draft_message_id)
    for event in (record for record in typed if isinstance(record, Event)):
        owner = owners.get(event.id)
        errors.extend(event_template_violations(event, records=typed, owners=owners))
        if isinstance(event.trigger, AfterEntity):
            if event.trigger.match.client_id is not None and (
                owner is None or event.trigger.match.client_id != owner.client_id
            ):
                errors.append(f"event {event.id!r} has a cross-client trigger match")
        payload = event.payload
        if hasattr(payload, "thread_ref"):
            if "${" not in payload.thread_ref:
                compatible(f"event {event.id!r}", owner, payload.thread_ref)
        if hasattr(payload, "bank_account_id"):
            if "${" not in payload.bank_account_id:
                compatible(f"event {event.id!r}", owner, payload.bank_account_id)
        if hasattr(payload, "txn_fixture_refs"):
            compatible_many(
                f"event {event.id!r}",
                owner,
                (
                    reference
                    for reference in payload.txn_fixture_refs
                    if "${" not in reference
                ),
            )
        if hasattr(payload, "task_id"):
            if payload.task_id is None or "${" not in payload.task_id:
                compatible(f"event {event.id!r}", owner, payload.task_id)
        if hasattr(payload, "target_ref"):
            if "${" not in payload.target_ref:
                compatible(f"event {event.id!r}", owner, payload.target_ref)
        if hasattr(payload, "attachment_fixture_refs"):
            compatible_many(
                f"event {event.id!r}",
                owner,
                (
                    reference
                    for reference in payload.attachment_fixture_refs
                    if "${" not in reference
                ),
            )
    for action in actions.values():
        owner = owners.get(action.id)
        for mutation in action.mutations:
            compatible(f"action {action.id!r}", owner, mutation.entity_id)
        for action_payload in (action.input_payload, action.output_payload):
            compatible_many(
                f"action {action.id!r}",
                owner,
                _embedded_record_references(action_payload),
            )
    for record in (record for record in typed if isinstance(record, ProvenanceRecord)):
        subject = owners.get(record.subject_ref)
        if subject is None:
            errors.append(
                f"provenance {record.id!r} cannot resolve ownership for subject {record.subject_ref!r}"
            )
            continue
        compatible(f"provenance {record.id!r}", subject, record.basis_ref)

    return tuple(errors)


def journals_from(records: Iterable[VersionedModel]) -> tuple[Journal, ...]:
    """Avoid carrying a mutable journal collection through the validation phases."""

    return tuple(record for record in records if isinstance(record, Journal))


def _embedded_record_references(value: object) -> set[str]:
    """Return explicit stable identifiers embedded in an Action JSON payload.

    Actions are append-only JSON audit envelopes, so their fields cannot carry typed
    relationships.  Only strings matching a known identifier prefix are candidates;
    ``compatible`` then decides whether a candidate resolves to a scoped record.
    Unknown free text remains free text and cannot accidentally become ownership data.
    """

    prefixes = (
        "cli-",
        "eng-",
        "prd-",
        "acc-",
        "jnl-",
        "bnk-",
        "btx-",
        "doc-",
        "thr-",
        "msg-",
        "irq-",
        "tsk-",
        "wpp-",
        "rvn-",
        "apv-",
        "evt-",
        "act-",
        "prv-",
    )
    if isinstance(value, str):
        return {value} if value.startswith(prefixes) else set()
    if isinstance(value, list):
        return {
            item for nested in value for item in _embedded_record_references(nested)
        }
    if isinstance(value, dict):
        return {
            item
            for nested in value.values()
            for item in _embedded_record_references(nested)
        }
    return set()
