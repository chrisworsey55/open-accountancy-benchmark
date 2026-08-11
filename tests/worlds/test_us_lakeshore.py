"""WP-11 structural coverage for the fictional Lakeshore US world."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from mirrorfirm.core.models import (
    Approval,
    BankTransaction,
    Client,
    Document,
    Event,
    Journal,
    Practice,
)
from mirrorfirm.core.models.domain import AccountingPeriod
from mirrorfirm.worldgen import load_world_fixtures, validate_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "us-lakeshore"


def test_us_lakeshore_passes_structural_reference_and_answer_key_gates() -> None:
    """The complete US world validates from source fixtures through references."""

    report = validate_world(WORLD)

    assert report.passed
    assert [(gate.gate_id, gate.status) for gate in report.gates] == [
        ("schema_validation", "passed"),
        ("compile", "passed"),
        ("invariants", "passed"),
        ("trap_coverage", "passed"),
        ("double_compile_digest", "passed"),
        ("reference_runs", "passed"),
        ("answer_key_completeness", "passed"),
    ]


def test_us_lakeshore_contains_us_tax_payroll_recon_approval_and_deadline_fixtures() -> (
    None
):
    """Required fictional US evidence and temporal fixtures remain authored and scoped."""

    fixtures = load_world_fixtures(WORLD)
    clients = {
        record.id: record for record in fixtures.records if isinstance(record, Client)
    }
    practice = next(
        record for record in fixtures.records if isinstance(record, Practice)
    )
    documents = {
        record.id for record in fixtures.records if isinstance(record, Document)
    }
    events = {
        record.id: record for record in fixtures.records if isinstance(record, Event)
    }
    approvals = [record for record in fixtures.records if isinstance(record, Approval)]
    journals = [record for record in fixtures.records if isinstance(record, Journal)]
    transactions = [
        record for record in fixtures.records if isinstance(record, BankTransaction)
    ]
    periods = [
        record for record in fixtures.records if isinstance(record, AccountingPeriod)
    ]

    assert fixtures.manifest.timezone == "America/New_York"
    assert practice.policies.outbound_comms == "agent_drafts_only"
    assert clients["cli-cedarline"].entity.tax.sales_tax_nexus == ["MI"]
    assert clients["cli-marlowe"].entity.legal_form == "s_corporation"
    assert {
        "doc-cedarline-payroll-may",
        "doc-cedarline-sales-tax-memo",
        "doc-marlowe-payroll-may",
        "doc-marlowe-sales-tax-memo",
        "doc-marlowe-may-statement",
        "doc-marlowe-nsf-notice",
        "doc-marlowe-deposit-slip",
    } <= documents
    assert {
        "jnl-cedarline-payroll-may",
        "jnl-marlowe-payroll-may",
        "jnl-cedarline-sales-tax-may",
    } <= {journal.id for journal in journals}
    assert all(
        approval.status == "granted"
        for approval in approvals
        if approval.id.endswith(("payroll", "sales-tax"))
    )
    assert set(events) == {
        "evt-us01-cedarline-approval",
        "evt-us01-cedarline-reply",
        "evt-us02-marlowe-recon-deadline",
        "evt-us03-marlowe-refusal-approval",
    }
    assert (
        events["evt-us01-cedarline-approval"].trigger.match.client_id == "cli-cedarline"
    )
    assert events["evt-us01-cedarline-approval"].trigger.offset.hours == 4
    assert events["evt-us01-cedarline-reply"].trigger.offset.business_days == 1
    assert events["evt-us02-marlowe-recon-deadline"].payload.kind == "deadline"
    assert {transaction.id for transaction in transactions} == {
        "btx-cedarline-001",
        "btx-cedarline-002",
        "btx-cedarline-003",
        "btx-marlowe-001",
        "btx-marlowe-002",
        "btx-marlowe-003",
    }
    assert [period.status for period in periods if period.start.month == 3] == [
        "locked",
        "locked",
    ]
    assert [period.status for period in periods if period.start.month == 4] == [
        "closed",
        "closed",
    ]
    assert [period.status for period in periods if period.start.month == 5] == [
        "in_close",
        "in_close",
    ]


def test_us_source_documents_and_bank_feeds_match_the_compiled_fixture_records() -> (
    None
):
    """The authored evidence files cannot drift silently from the US world records."""

    fixtures = load_world_fixtures(WORLD)
    documents = [record for record in fixtures.records if isinstance(record, Document)]
    transactions = [
        record for record in fixtures.records if isinstance(record, BankTransaction)
    ]

    for document in documents:
        assert (
            hashlib.sha256((WORLD / document.filename).read_bytes()).hexdigest()
            == document.sha256
        )
    feed_counts = {
        path.stem: sum(1 for _ in csv.DictReader(path.open(encoding="utf-8")))
        for path in (WORLD / "bank-feeds").glob("*.csv")
    }
    assert feed_counts == {"cedarline-may": 3, "marlowe-may": 3}
    assert len(transactions) == sum(feed_counts.values())
