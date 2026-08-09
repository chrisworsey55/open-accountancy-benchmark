"""WP-06 structural validation for the fictional Wyrley Brook UK world."""

import csv
import hashlib
from pathlib import Path

from mirrorfirm.core.models import BankTransaction, Client, Document, Event
from mirrorfirm.core.models.domain import AccountingPeriod
from mirrorfirm.worldgen import load_world_fixtures, validate_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"


def test_uk_wyrley_brook_passes_all_available_structural_gates() -> None:
    report = validate_world(WORLD)

    assert report.passed
    assert [(gate.gate_id, gate.status) for gate in report.gates] == [
        ("schema_validation", "passed"),
        ("compile", "passed"),
        ("invariants", "passed"),
        ("trap_coverage", "passed"),
        ("double_compile_digest", "passed"),
        ("reference_runs", "not_available"),
        ("answer_key_completeness", "not_available"),
    ]


def test_uk_wyrley_brook_has_three_clients_documents_feeds_and_episode_events() -> None:
    fixtures = load_world_fixtures(WORLD)

    client_ids = {
        record.id for record in fixtures.records if type(record).__name__ == "Client"
    }
    assert client_ids == {"cli-brightpath", "cli-harper", "cli-kestrel"}
    assert {
        event.id for event in fixtures.records if type(event).__name__ == "Event"
    } == {
        "evt-uk01-brightpath-reply",
        "evt-uk02-kestrel-recon-deadline",
        "evt-uk03-vat-review-reminder",
        "evt-uk04-harper-horizon",
    }
    assert {trap.trap_id for trap in fixtures.traps} >= {
        "trap-uk02-outstanding-cheque",
        "trap-uk02-duplicate-feed-line",
        "trap-uk02-unresolved-residual",
    }
    support_document_ids = {
        record.id for record in fixtures.records if isinstance(record, Document)
    }
    assert {
        f"doc-brightpath-may-{number:03d}" for number in range(1, 25)
    } <= support_document_ids
    assert len(fixtures.traps) == 9
    assert (WORLD / "opening-balances.csv").is_file()
    feed_counts = {
        path.stem: sum(1 for _ in csv.DictReader(path.open(encoding="utf-8")))
        for path in (WORLD / "bank-feeds").glob("*.csv")
    }
    assert feed_counts == {
        "brightpath-may": 28,
        "harper-may": 6,
        "kestrel-may": 7,
    }
    assert len(list((WORLD / "documents").glob("*.txt"))) >= 35


def test_uk_wyrley_brook_matches_the_uk_episode_catalogue() -> None:
    fixtures = load_world_fixtures(WORLD)

    assert fixtures.manifest.timezone == "Europe/London"
    clients = {
        record.id: record for record in fixtures.records if isinstance(record, Client)
    }
    assert clients["cli-brightpath"].entity.tax.vat_scheme == "standard"
    assert clients["cli-harper"].entity.legal_form == "sole_trader"
    assert not clients["cli-harper"].entity.tax.vat_registered
    assert clients["cli-kestrel"].entity.tax.vat_scheme == "standard"
    assert clients["cli-kestrel"].entity.basis == "cash"

    periods = [
        record for record in fixtures.records if isinstance(record, AccountingPeriod)
    ]
    assert [period.status for period in periods if period.start.month == 3] == [
        "locked",
        "locked",
        "locked",
    ]
    assert [period.status for period in periods if period.start.month == 4] == [
        "closed",
        "closed",
        "closed",
    ]
    assert [period.status for period in periods if period.start.month == 5] == [
        "in_close",
        "in_close",
        "in_close",
    ]

    transactions = [
        record for record in fixtures.records if isinstance(record, BankTransaction)
    ]
    brightpath_transactions = [
        transaction
        for transaction in transactions
        if transaction.bank_account_id == "bnk-brightpath"
    ]
    harper_transactions = [
        transaction
        for transaction in transactions
        if transaction.bank_account_id == "bnk-harper"
    ]
    kestrel_transactions = [
        transaction
        for transaction in transactions
        if transaction.bank_account_id == "bnk-kestrel"
    ]
    assert len(brightpath_transactions) == 28
    assert len(harper_transactions) == 6
    assert len(kestrel_transactions) == 7
    duplicate_lines = [
        transaction
        for transaction in kestrel_transactions
        if transaction.reference == "KC-DUP-22"
    ]
    assert len(duplicate_lines) == 2
    assert duplicate_lines[0].model_dump(exclude={"id"}) == duplicate_lines[
        1
    ].model_dump(exclude={"id"})

    events = {
        record.id: record for record in fixtures.records if isinstance(record, Event)
    }
    brightpath_reply = events["evt-uk01-brightpath-reply"]
    assert brightpath_reply.trigger.kind == "after_entity"
    assert brightpath_reply.trigger.entity_kind == "information_request"
    assert brightpath_reply.trigger.match.client_id == "cli-brightpath"
    assert brightpath_reply.trigger.match.status == "sent"
    assert brightpath_reply.trigger.offset.business_days == 1
    assert brightpath_reply.payload.kind == "client_reply"
    assert brightpath_reply.payload.marks_irq == "responded_partial"
    assert events["evt-uk04-harper-horizon"].payload.kind == "deadline"


def test_document_records_match_the_authored_file_hashes() -> None:
    fixtures = load_world_fixtures(WORLD)
    documents = (record for record in fixtures.records if isinstance(record, Document))

    for document in documents:
        assert (
            hashlib.sha256((WORLD / document.filename).read_bytes()).hexdigest()
            == document.sha256
        )
