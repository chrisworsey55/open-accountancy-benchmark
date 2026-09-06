"""Ordered structural validation gates for compiled Franklin & McGrath worlds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from mirrorfirm.core.db import SQLiteWorldView, WorldView, immutable_constraints_present
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
    Entity,
    Event,
    InformationRequest,
    Journal,
    Message,
    Person,
    Practice,
    ProvenanceRecord,
    QueryLogWorkpaper,
    ReviewNote,
    Task,
    Thread,
    VersionedModel,
    Workpaper,
    WorldManifest,
)
from mirrorfirm.episodes import (
    EpisodeAuthoringError,
    answer_key_path,
    episode_root,
    load_answer_key,
    load_episode_manifest,
    run_reference_episode,
    validate_answer_key,
)
from mirrorfirm.episodes.references import ReferenceScriptError

from .compile import WorldCompileError, compile_world, load_world_fixtures
from .ownership import ownership_violations
from .traps import TrapRegisterError, apply_traps

GateStatus = Literal["passed", "failed", "not_available"]


@dataclass(frozen=True)
class GateResult:
    """One ordered structural validation outcome."""

    gate_id: str
    status: GateStatus
    detail: str


@dataclass(frozen=True)
class InvariantViolation:
    """A concrete violation of one of the structural I-1 through I-8 rules."""

    invariant_id: str
    detail: str


@dataclass(frozen=True)
class WorldValidationResult:
    """All executed validation gates for one authored world."""

    world_dir: Path
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        """Whether no executed gate failed."""

        return all(gate.status != "failed" for gate in self.gates)


def validate_world(world_dir: str | Path) -> WorldValidationResult:
    """Run the WP-05 structural gates in their required, fail-fast order."""

    root = Path(world_dir).resolve()
    gates: list[GateResult] = []
    try:
        fixtures = load_world_fixtures(root)
    except WorldCompileError as error:
        return _failed(root, gates, "schema_validation", str(error))
    gates.append(
        GateResult("schema_validation", "passed", "fixtures parse as §E models")
    )

    with TemporaryDirectory(prefix="mirrorfirm-validate-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        compiled_path = temporary_root / "compiled.db"
        try:
            compiled = compile_world(root, compiled_path)
        except WorldCompileError as error:
            return _failed(root, gates, "compile", str(error))
        gates.append(GateResult("compile", "passed", "fixtures compile into SQLite"))

        with SQLiteWorldView.open(compiled.database_path) as view:
            violations = validate_structural_invariants(view, fixtures.manifest)
            if not immutable_constraints_present(compiled.database_path):
                violations = (
                    *violations,
                    InvariantViolation(
                        "I-2", "compiled database is missing an immutable-state trigger"
                    ),
                )
            if violations:
                detail = "; ".join(
                    f"{violation.invariant_id}: {violation.detail}"
                    for violation in violations
                )
                return _failed(root, gates, "invariants", detail)
        gates.append(GateResult("invariants", "passed", "I-1 through I-8 hold"))

        try:
            with SQLiteWorldView.open(compiled.database_path) as view:
                apply_traps(fixtures.traps, _known_record_ids(view))
        except TrapRegisterError as error:
            return _failed(root, gates, "trap_coverage", str(error))
        gates.append(GateResult("trap_coverage", "passed", "trap references resolve"))

        second_compiled_path = temporary_root / "compiled-again.db"
        try:
            second_compiled = compile_world(root, second_compiled_path)
        except WorldCompileError as error:
            return _failed(root, gates, "double_compile_digest", str(error))
        if compiled.state_digest != second_compiled.state_digest:
            return _failed(
                root,
                gates,
                "double_compile_digest",
                "independent compilations produced different logical-state digests",
            )
        gates.append(
            GateResult(
                "double_compile_digest",
                "passed",
                "independent compilations have the same logical-state digest",
            )
        )
        reference_gate, answer_key_gate = _validate_episode_gates(
            root,
            fixtures.manifest,
            compiled.database_path,
            temporary_root / "reference-results",
        )
        gates.extend((reference_gate, answer_key_gate))
    return WorldValidationResult(root, tuple(gates))


def _validate_episode_gates(
    world_root: Path,
    manifest: WorldManifest,
    compiled_database_path: Path,
    results_root: Path,
) -> tuple[GateResult, GateResult]:
    """Run the WP-10 authored-reference and answer-key validation gates."""

    try:
        episodes_dir = episode_root(manifest.jurisdiction, root=world_root)
    except EpisodeAuthoringError:
        return (
            GateResult(
                "reference_runs",
                "not_available",
                "no repository episode catalogue is available for this transient world",
            ),
            GateResult(
                "answer_key_completeness",
                "not_available",
                "no repository episode catalogue is available for this transient world",
            ),
        )

    episode_paths = sorted(episodes_dir.glob(f"ep-{manifest.jurisdiction}-*.yaml"))
    try:
        authored = [(path, load_episode_manifest(path)) for path in episode_paths]
    except EpisodeAuthoringError as error:
        return (
            GateResult("reference_runs", "failed", str(error)),
            GateResult("answer_key_completeness", "failed", str(error)),
        )
    episodes = [
        (path, episode)
        for path, episode in authored
        if episode.world_id == manifest.world_id
        and episode.world_version == manifest.world_version
    ]
    if not episodes:
        detail = (
            f"no {manifest.jurisdiction} episode manifests target {manifest.world_id!r}"
        )
        return (
            GateResult("reference_runs", "failed", detail),
            GateResult("answer_key_completeness", "failed", detail),
        )

    try:
        for path, episode in episodes:
            del path
            reference = run_reference_episode(
                episode,
                compiled_database_path,
                world_root=world_root,
                results_root=results_root,
                run_id=f"run-validation-{episode.episode_id}",
            )
            if (
                reference.evaluation.overall != 1.0
                or reference.evaluation.critical_failures
                or not reference.evaluation.all_pass
            ):
                raise ReferenceScriptError(
                    f"reference {episode.episode_id!r} did not score 100% with zero critical failures"
                )
    except (OSError, ReferenceScriptError, ValueError) as error:
        return (
            GateResult("reference_runs", "failed", str(error)),
            GateResult(
                "answer_key_completeness",
                "not_available",
                "reference-run gate did not pass",
            ),
        )

    reference_gate = GateResult(
        "reference_runs",
        "passed",
        f"{len(episodes)} references score 100% with zero critical failures",
    )
    try:
        for path, episode in episodes:
            validate_answer_key(episode, load_answer_key(answer_key_path(path)))
    except EpisodeAuthoringError as error:
        return reference_gate, GateResult(
            "answer_key_completeness", "failed", str(error)
        )
    return reference_gate, GateResult(
        "answer_key_completeness",
        "passed",
        f"{len(episodes)} answer keys exactly match their typed manifests",
    )


def validate_structural_invariants(
    view: WorldView, manifest: WorldManifest
) -> tuple[InvariantViolation, ...]:
    """Evaluate the structural subset of I-1 through I-8 against one snapshot."""

    violations: list[InvariantViolation] = []
    practices = view.list(Practice)
    people = view.list(Person)
    clients = view.list(Client)
    entities = view.list(Entity)
    engagements = view.list(Engagement)
    periods = view.list(AccountingPeriod)
    accounts = view.list(Account)
    journals = view.list(Journal)
    bank_accounts = view.list(BankAccount)
    bank_transactions = view.list(BankTransaction)
    documents = view.list(Document)
    threads = view.list(Thread)
    messages = view.list(Message)
    information_requests = view.list(InformationRequest)
    tasks = view.list(Task)
    workpapers = view.list(Workpaper)
    review_notes = view.list(ReviewNote)
    approvals = view.list(Approval)
    events = view.events()
    actions = view.actions()
    provenance = view.list(ProvenanceRecord)

    # I-1: Pydantic prevents an invalid Journal from compiling; repeat the exact
    # arithmetic here to protect loaded SQLite data.
    for journal in journals:
        balances: dict[str, int] = {}
        for line in journal.lines:
            direction = 1 if line.direction == "dr" else -1
            balances[line.currency] = (
                balances.get(line.currency, 0) + direction * line.amount_minor
            )
        if any(amount != 0 for amount in balances.values()):
            violations.append(
                InvariantViolation("I-1", f"journal {journal.id!r} is not balanced")
            )

    all_ids = _known_record_ids(view)
    _check_referential_integrity(
        violations,
        practices,
        people,
        clients,
        entities,
        engagements,
        periods,
        accounts,
        journals,
        bank_accounts,
        bank_transactions,
        documents,
        threads,
        messages,
        information_requests,
        tasks,
        workpapers,
        review_notes,
        approvals,
        events,
        actions,
        provenance,
        all_ids,
    )
    _check_posting_periods(violations, journals, periods, approvals)
    _check_classification_links(violations, journals, bank_transactions)
    _check_reconciliations(violations, workpapers, all_ids)
    _check_client_isolation(
        violations,
        clients,
        engagements,
        tasks,
        workpapers,
        documents,
        threads,
        messages,
        information_requests,
        events,
    )
    structural_records: tuple[VersionedModel, ...] = (
        *practices,
        *people,
        *clients,
        *entities,
        *engagements,
        *periods,
        *accounts,
        *journals,
        *bank_accounts,
        *bank_transactions,
        *documents,
        *threads,
        *messages,
        *information_requests,
        *tasks,
        *workpapers,
        *review_notes,
        *approvals,
        *events,
        *actions,
        *provenance,
    )
    violations.extend(
        InvariantViolation("I-7", detail)
        for detail in ownership_violations(structural_records)
    )
    _check_time_monotonicity(violations, actions, events, manifest.start_world_time)
    return tuple(violations)


def _failed(
    root: Path, gates: list[GateResult], gate_id: str, detail: str
) -> WorldValidationResult:
    gates.append(GateResult(gate_id, "failed", detail))
    return WorldValidationResult(root, tuple(gates))


def _known_record_ids(view: WorldView) -> set[str]:
    identifiers: set[str] = set()
    records: tuple[VersionedModel, ...] = (
        *view.list(Practice),
        *view.list(Person),
        *view.list(Client),
        *view.list(Entity),
        *view.list(Engagement),
        *view.list(AccountingPeriod),
        *view.list(Account),
        *view.list(Journal),
        *view.list(BankAccount),
        *view.list(BankTransaction),
        *view.list(Document),
        *view.list(Thread),
        *view.list(Message),
        *view.list(InformationRequest),
        *view.list(Task),
        *view.list(Workpaper),
        *view.list(ReviewNote),
        *view.list(Approval),
        *view.list(ProvenanceRecord),
    )
    for record in records:
        identifier = getattr(record, "id", None)
        if isinstance(identifier, str):
            identifiers.add(identifier)
    identifiers.update(action.id for action in view.actions())
    identifiers.update(event.id for event in view.events())
    return identifiers


def _check_referential_integrity(
    violations: list[InvariantViolation],
    practices: tuple[Practice, ...],
    people: tuple[Person, ...],
    clients: tuple[Client, ...],
    entities: tuple[Entity, ...],
    engagements: tuple[Engagement, ...],
    periods: tuple[AccountingPeriod, ...],
    accounts: tuple[Account, ...],
    journals: tuple[Journal, ...],
    bank_accounts: tuple[BankAccount, ...],
    bank_transactions: tuple[BankTransaction, ...],
    documents: tuple[Document, ...],
    threads: tuple[Thread, ...],
    messages: tuple[Message, ...],
    information_requests: tuple[InformationRequest, ...],
    tasks: tuple[Task, ...],
    workpapers: tuple[Workpaper, ...],
    review_notes: tuple[ReviewNote, ...],
    approvals: tuple[Approval, ...],
    events: tuple[Event, ...],
    actions: tuple[Action, ...],
    provenance: tuple[ProvenanceRecord, ...],
    all_ids: set[str],
) -> None:
    def require(reference: str | None, candidates: set[str], context: str) -> None:
        if reference in (None, "per-sys") or (
            reference is not None and reference.startswith("${entity.")
        ):
            return
        if reference is not None and reference not in candidates:
            violations.append(
                InvariantViolation("I-3", f"{context} references {reference!r}")
            )

    person_ids = {person.id for person in people}
    client_ids = {client.id for client in clients}
    entity_ids = {entity.id for entity in entities}
    engagement_ids = {engagement.id for engagement in engagements}
    period_ids = {period.id for period in periods}
    account_ids = {account.id for account in accounts}
    journal_ids = {journal.id for journal in journals}
    bank_account_ids = {account.id for account in bank_accounts}
    transaction_ids = {transaction.id for transaction in bank_transactions}
    document_ids = {document.id for document in documents}
    thread_ids = {thread.id for thread in threads}
    message_ids = {message.id for message in messages}
    task_ids = {task.id for task in tasks}

    for practice in practices:
        for person_id in practice.people:
            require(person_id, person_ids, f"practice {practice.id!r}")
    for person in people:
        require(person.client_id, client_ids, f"person {person.id!r}")
    for client in clients:
        require(client.entity.id, entity_ids, f"client {client.id!r}")
        for contact in client.contacts:
            require(contact, person_ids, f"client {client.id!r}")
    for engagement in engagements:
        require(engagement.client_id, client_ids, f"engagement {engagement.id!r}")
        for period_id in engagement.period_ids:
            require(period_id, period_ids, f"engagement {engagement.id!r}")
    for period in periods:
        require(period.entity_id, entity_ids, f"period {period.id!r}")
    for account in accounts:
        require(account.entity_id, entity_ids, f"account {account.id!r}")
    for bank_account in bank_accounts:
        require(bank_account.entity_id, entity_ids, f"bank account {bank_account.id!r}")
        require(
            bank_account.ledger_account_id,
            account_ids,
            f"bank account {bank_account.id!r}",
        )
    for transaction in bank_transactions:
        require(
            transaction.bank_account_id,
            bank_account_ids,
            f"bank transaction {transaction.id!r}",
        )
    for journal in journals:
        require(journal.entity_id, entity_ids, f"journal {journal.id!r}")
        require(
            journal.approval_id,
            {approval.id for approval in approvals},
            f"journal {journal.id!r}",
        )
        for line in journal.lines:
            require(line.account_id, account_ids, f"journal {journal.id!r}")
            require(
                line.bank_transaction_id, transaction_ids, f"journal {journal.id!r}"
            )
    for document in documents:
        require(document.client_id, client_ids, f"document {document.id!r}")
    for thread in threads:
        require(thread.client_id, client_ids, f"thread {thread.id!r}")
        for message_id in thread.message_ids:
            require(message_id, message_ids, f"thread {thread.id!r}")
    for message in messages:
        require(message.thread_id, thread_ids, f"message {message.id!r}")
        owning_thread = next(
            (thread for thread in threads if thread.id == message.thread_id), None
        )
        if owning_thread is not None and message.id not in owning_thread.message_ids:
            violations.append(
                InvariantViolation(
                    "I-3", f"message {message.id!r} is absent from its thread"
                )
            )
        require(message.sender, person_ids, f"message {message.id!r}")
        for recipient in message.recipients:
            require(recipient, person_ids, f"message {message.id!r}")
        for attachment in message.attachments:
            require(attachment, document_ids, f"message {message.id!r}")
    for request in information_requests:
        require(request.client_id, client_ids, f"information request {request.id!r}")
        require(request.thread_id, thread_ids, f"information request {request.id!r}")
        for irq_item in request.items:
            for reference in irq_item.refs:
                require(reference, all_ids, f"information request {request.id!r}")
    for task in tasks:
        require(task.engagement_id, engagement_ids, f"task {task.id!r}")
        require(task.assignee, person_ids, f"task {task.id!r}")
        require(task.blocked_on, all_ids, f"task {task.id!r}")
    for workpaper in workpapers:
        require(workpaper.engagement_id, engagement_ids, f"workpaper {workpaper.id!r}")
        require(workpaper.task_id, task_ids, f"workpaper {workpaper.id!r}")
        require(workpaper.created_by, person_ids, f"workpaper {workpaper.id!r}")
        body = workpaper.body
        if isinstance(body, BankReconWorkpaper):
            require(
                body.bank_account_id, bank_account_ids, f"workpaper {workpaper.id!r}"
            )
            require(body.period_id, period_ids, f"workpaper {workpaper.id!r}")
            for recon_item in body.outstanding:
                for reference in recon_item.provenance_refs:
                    require(reference, all_ids, f"workpaper {workpaper.id!r}")
            for unresolved_item in body.unresolved:
                for reference in unresolved_item.provenance_refs:
                    require(reference, all_ids, f"workpaper {workpaper.id!r}")
        elif isinstance(body, ClassificationSummaryWorkpaper):
            require(body.period_id, period_ids, f"workpaper {workpaper.id!r}")
            for row in body.rows:
                require(
                    row.bank_transaction_id,
                    transaction_ids,
                    f"workpaper {workpaper.id!r}",
                )
                require(row.account_id, account_ids, f"workpaper {workpaper.id!r}")
                for reference in row.provenance_refs:
                    require(reference, all_ids, f"workpaper {workpaper.id!r}")
        elif isinstance(body, QueryLogWorkpaper):
            for unresolved_item in body.items:
                for reference in unresolved_item.provenance_refs:
                    require(reference, all_ids, f"workpaper {workpaper.id!r}")
    for note in review_notes:
        require(note.engagement_id, engagement_ids, f"review note {note.id!r}")
        require(note.target_ref, all_ids, f"review note {note.id!r}")
        require(note.author_id, person_ids, f"review note {note.id!r}")
        require(note.addressed_by, person_ids, f"review note {note.id!r}")
    for approval in approvals:
        require(approval.requested_by, person_ids, f"approval {approval.id!r}")
        for reference in approval.provenance_refs:
            require(reference, all_ids, f"approval {approval.id!r}")
        descriptor = approval.action_descriptor
        if descriptor.kind != approval.kind:
            violations.append(
                InvariantViolation(
                    "I-3", f"approval {approval.id!r} kind disagrees with descriptor"
                )
            )
        if hasattr(descriptor, "journal_id"):
            require(descriptor.journal_id, journal_ids, f"approval {approval.id!r}")
        if hasattr(descriptor, "period_id"):
            require(descriptor.period_id, period_ids, f"approval {approval.id!r}")
        if hasattr(descriptor, "draft_message_id"):
            require(
                descriptor.draft_message_id, message_ids, f"approval {approval.id!r}"
            )
    for event in events:
        if isinstance(event.trigger, AfterEntity):
            require(event.trigger.match.client_id, client_ids, f"event {event.id!r}")
        payload = event.payload
        if hasattr(payload, "thread_ref"):
            require(payload.thread_ref, thread_ids, f"event {event.id!r}")
        if hasattr(payload, "bank_account_id"):
            require(payload.bank_account_id, bank_account_ids, f"event {event.id!r}")
        if hasattr(payload, "task_id"):
            require(payload.task_id, task_ids, f"event {event.id!r}")
        if hasattr(payload, "target_ref"):
            require(payload.target_ref, all_ids, f"event {event.id!r}")
        if hasattr(payload, "attachment_fixture_refs"):
            for attachment in payload.attachment_fixture_refs:
                require(attachment, document_ids, f"event {event.id!r}")
    for action in actions:
        require(action.actor, person_ids, f"action {action.id!r}")
        for mutation in action.mutations:
            require(mutation.entity_id, all_ids, f"action {action.id!r}")
    for record in provenance:
        require(record.subject_ref, all_ids, f"provenance {record.id!r}")
        require(record.basis_ref, all_ids, f"provenance {record.id!r}")


def _check_posting_periods(
    violations: list[InvariantViolation],
    journals: tuple[Journal, ...],
    periods: tuple[AccountingPeriod, ...],
    approvals: tuple[Approval, ...],
) -> None:
    for journal in journals:
        if journal.status != "posted":
            continue
        approval = next(
            (
                candidate
                for candidate in approvals
                if candidate.id == journal.approval_id
            ),
            None,
        )
        if approval is None or approval.status != "granted":
            violations.append(
                InvariantViolation(
                    "I-4",
                    f"posted journal {journal.id!r} lacks its granted approval",
                )
            )
        elif getattr(approval.action_descriptor, "journal_id", None) != journal.id:
            violations.append(
                InvariantViolation(
                    "I-4",
                    f"posted journal {journal.id!r} approval targets another journal",
                )
            )
        matching_periods = [
            period
            for period in periods
            if period.entity_id == journal.entity_id
            and period.start <= journal.date <= period.end
        ]
        if not matching_periods:
            violations.append(
                InvariantViolation(
                    "I-4", f"posted journal {journal.id!r} has no period"
                )
            )
            continue
        for period in matching_periods:
            if period.status == "locked":
                violations.append(
                    InvariantViolation(
                        "I-4",
                        f"posted journal {journal.id!r} is in locked period {period.id!r}",
                    )
                )
            if period.status == "closed" and not _has_closed_period_approval(
                approvals, journal.id, period.id, journal.approval_id
            ):
                violations.append(
                    InvariantViolation(
                        "I-4",
                        f"posted journal {journal.id!r} lacks closed-period approval",
                    )
                )


def _has_closed_period_approval(
    approvals: tuple[Approval, ...],
    journal_id: str,
    period_id: str,
    approval_id: str | None,
) -> bool:
    return any(
        approval.id == approval_id
        and approval.status == "granted"
        and approval.kind == "post_to_closed_period"
        and getattr(approval.action_descriptor, "journal_id", None) == journal_id
        and getattr(approval.action_descriptor, "period_id", None) == period_id
        for approval in approvals
    )


def _check_classification_links(
    violations: list[InvariantViolation],
    journals: tuple[Journal, ...],
    transactions: tuple[BankTransaction, ...],
) -> None:
    posted_lines = [
        line
        for journal in journals
        if journal.status == "posted"
        for line in journal.lines
    ]
    for transaction in transactions:
        linked = [
            line for line in posted_lines if line.bank_transaction_id == transaction.id
        ]
        exact_links = [
            line
            for line in linked
            if line.amount_minor == abs(transaction.amount_minor)
        ]
        if linked and len(linked) != len(exact_links):
            violations.append(
                InvariantViolation(
                    "I-5",
                    f"bank transaction {transaction.id!r} has a mismatched journal link",
                )
            )
        if transaction.classification_status == "classified" and len(exact_links) != 1:
            violations.append(
                InvariantViolation(
                    "I-5",
                    f"classified bank transaction {transaction.id!r} needs one posted link",
                )
            )
        if transaction.classification_status != "classified" and exact_links:
            violations.append(
                InvariantViolation(
                    "I-5",
                    f"unclassified bank transaction {transaction.id!r} has a posted link",
                )
            )


def _check_reconciliations(
    violations: list[InvariantViolation],
    workpapers: tuple[Workpaper, ...],
    all_ids: set[str],
) -> None:
    for workpaper in workpapers:
        if workpaper.status != "final" or not isinstance(
            workpaper.body, BankReconWorkpaper
        ):
            continue
        body = workpaper.body
        tied = body.statement_end_minor == body.ledger_end_minor + sum(
            item.amount_minor for item in body.outstanding
        )
        if tied:
            continue
        unresolved_has_provenance = bool(body.unresolved) and all(
            item.provenance_refs
            and all(reference in all_ids for reference in item.provenance_refs)
            for item in body.unresolved
        )
        if not unresolved_has_provenance:
            violations.append(
                InvariantViolation(
                    "I-6",
                    f"final workpaper {workpaper.id!r} does not tie or explain variance",
                )
            )


def _check_client_isolation(
    violations: list[InvariantViolation],
    clients: tuple[Client, ...],
    engagements: tuple[Engagement, ...],
    tasks: tuple[Task, ...],
    workpapers: tuple[Workpaper, ...],
    documents: tuple[Document, ...],
    threads: tuple[Thread, ...],
    messages: tuple[Message, ...],
    requests: tuple[InformationRequest, ...],
    events: tuple[Event, ...],
) -> None:
    client_ids = {client.id for client in clients}
    document_clients = {document.id: document.client_id for document in documents}
    thread_clients = {thread.id: thread.client_id for thread in threads}
    engagement_clients = {
        engagement.id: engagement.client_id for engagement in engagements
    }
    task_clients = {
        task.id: engagement_clients.get(task.engagement_id) for task in tasks
    }
    for request in requests:
        if (
            request.thread_id is not None
            and thread_clients.get(request.thread_id) != request.client_id
        ):
            violations.append(
                InvariantViolation(
                    "I-7", f"information request {request.id!r} crosses client scopes"
                )
            )
    for message in messages:
        message_client = thread_clients.get(message.thread_id)
        for attachment in message.attachments:
            attachment_client = document_clients.get(attachment)
            if attachment_client is not None and attachment_client != message_client:
                violations.append(
                    InvariantViolation(
                        "I-7",
                        f"message {message.id!r} attaches another client's document",
                    )
                )
    for workpaper in workpapers:
        if task_clients.get(workpaper.task_id) != engagement_clients.get(
            workpaper.engagement_id
        ):
            violations.append(
                InvariantViolation(
                    "I-7", f"workpaper {workpaper.id!r} crosses engagement/client scope"
                )
            )
    for event in events:
        if (
            isinstance(event.trigger, AfterEntity)
            and event.trigger.match.client_id is None
        ):
            violations.append(
                InvariantViolation(
                    "I-7", f"event {event.id!r} lacks an engagement/client selector"
                )
            )
        payload = event.payload
        if hasattr(payload, "thread_ref") and not payload.thread_ref.startswith(
            "${entity."
        ):
            if payload.thread_ref in thread_clients and isinstance(
                event.trigger, AfterEntity
            ):
                if thread_clients[payload.thread_ref] != event.trigger.match.client_id:
                    violations.append(
                        InvariantViolation(
                            "I-7", f"event {event.id!r} targets another client's thread"
                        )
                    )
        if hasattr(payload, "task_id") and payload.task_id is not None:
            if isinstance(event.trigger, AfterEntity) and (
                task_clients.get(payload.task_id) != event.trigger.match.client_id
            ):
                violations.append(
                    InvariantViolation(
                        "I-7", f"event {event.id!r} targets another client's task"
                    )
                )
    if not client_ids:
        violations.append(InvariantViolation("I-7", "world has no client scope"))


def _check_time_monotonicity(
    violations: list[InvariantViolation],
    actions: tuple[Action, ...],
    events: tuple[Event, ...],
    start_time: datetime,
) -> None:
    previous_time = start_time
    for action in actions:
        if action.world_time_after < previous_time:
            violations.append(
                InvariantViolation(
                    "I-8", f"action {action.id!r} moves world_time backward"
                )
            )
        previous_time = action.world_time_after
    for event in events:
        if (
            not isinstance(event.trigger, AfterEntity)
            and event.trigger.world_time < start_time
        ):
            violations.append(
                InvariantViolation("I-8", f"event {event.id!r} predates world start")
            )
