"""WP-05 compiler, structural gate ordering, traps, and determinism tests."""

from __future__ import annotations

import hashlib
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.core.models import Client, Journal
from mirrorfirm.worldgen import WorldCompileError, compile_world, validate_world


def _write_yaml(path: Path, content: object) -> None:
    path.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")


def _world_fixture(root: Path) -> Path:
    root.mkdir()
    _write_yaml(
        root / "world.yaml",
        {
            "world_id": "wld-fixture",
            "world_version": "0.1.0",
            "jurisdiction": "uk",
            "timezone": "Europe/London",
            "title": "Fictional fixture world",
            "description": "A synthetic world for compiler tests.",
            "seed": 41,
            "start_world_time": "2026-05-01T09:00:00Z",
            "practice_file": "practice.yaml",
            "client_files": ["clients.yaml"],
            "events_file": "events.yaml",
            "trap_register_file": "traps.yaml",
            "license": "CC-BY-4.0",
        },
    )
    _write_yaml(
        root / "practice.yaml",
        {
            "id": "prc-fixture",
            "name": "Fictional Accountancy",
            "jurisdiction": "uk",
            "people": ["per-agent", "per-contact"],
            "policies": {
                "approval_required_for": ["post_journal"],
                "outbound_comms": "agent_may_send",
                "materiality_minor": 10000,
            },
        },
    )
    _write_yaml(
        root / "clients.yaml",
        [
            {
                "id": "cli-fixture",
                "name": "Fictional Trading Ltd",
                "status": "active",
                "entity": {
                    "id": "ent-fixture",
                    "legal_form": "limited_company",
                    "basis": "accrual",
                    "currency": "GBP",
                    "tax": {
                        "kind": "uk",
                        "vat_registered": True,
                        "vat_number": "GB000000000",
                        "vat_scheme": "standard",
                    },
                },
                "contacts": ["per-contact"],
            }
        ],
    )
    _write_yaml(root / "events.yaml", {"events": []})
    _write_yaml(
        root / "traps.yaml",
        [
            {
                "trap_id": "trap-fixture-duplicate",
                "location_refs": ["doc-fixture"],
                "conflicts_with": ["jnl-fixture"],
                "expected_behaviour": "Flag the fictional duplicate.",
                "tempting_behaviour": "Classify it twice.",
                "grading_refs": ["duplicate_guard"],
                "severity": "material",
                "visible_to_agent": "discoverable",
            }
        ],
    )
    _write_yaml(
        root / "records.yaml",
        {
            "Person": [
                {
                    "id": "per-agent",
                    "name": "Fictional Agent",
                    "role": "agent",
                    "acting_as": "bookkeeper",
                },
                {
                    "id": "per-contact",
                    "name": "Fictional Contact",
                    "role": "client_contact",
                    "client_id": "cli-fixture",
                },
            ],
            "Engagement": [
                {
                    "id": "eng-fixture",
                    "client_id": "cli-fixture",
                    "scope": "bookkeeping",
                    "period_ids": ["prd-fixture"],
                    "status": "active",
                }
            ],
            "AccountingPeriod": [
                {
                    "id": "prd-fixture",
                    "entity_id": "ent-fixture",
                    "start": "2026-05-01",
                    "end": "2026-05-31",
                    "status": "open",
                }
            ],
            "Account": [
                {
                    "id": "acc-bank",
                    "entity_id": "ent-fixture",
                    "code": "1000",
                    "name": "Bank",
                    "type": "asset",
                    "tax_dimension": None,
                    "active": True,
                },
                {
                    "id": "acc-expense",
                    "entity_id": "ent-fixture",
                    "code": "7502",
                    "name": "Travel",
                    "type": "expense",
                    "tax_dimension": "input_vat",
                    "active": True,
                },
                {
                    "id": "acc-vat",
                    "entity_id": "ent-fixture",
                    "code": "2202",
                    "name": "VAT input",
                    "type": "asset",
                    "tax_dimension": "input_vat",
                    "active": True,
                },
            ],
            "BankAccount": [
                {
                    "id": "bnk-fixture",
                    "entity_id": "ent-fixture",
                    "name": "Fictional Bank",
                    "ledger_account_id": "acc-bank",
                    "currency": "GBP",
                    "engagement_id": "eng-fixture",
                }
            ],
            "BankTransaction": [
                {
                    "id": "btx-fixture",
                    "bank_account_id": "bnk-fixture",
                    "date": "2026-05-20",
                    "amount_minor": -12000,
                    "counterparty": "Fictional Travel",
                    "reference": "FIX-001",
                    "classification_status": "unclassified",
                    "reconciliation_status": "unmatched",
                }
            ],
            "Journal": [
                {
                    "id": "jnl-fixture",
                    "entity_id": "ent-fixture",
                    "date": "2026-05-20",
                    "memo": "Fictional proposed classification",
                    "source": "proposal",
                    "status": "proposed",
                    "lines": [
                        {
                            "account_id": "acc-expense",
                            "direction": "dr",
                            "amount_minor": 10000,
                            "currency": "GBP",
                            "tax": {
                                "kind": "uk_vat",
                                "code": "20-std",
                                "rate_bp": 2000,
                            },
                        },
                        {
                            "account_id": "acc-vat",
                            "direction": "dr",
                            "amount_minor": 2000,
                            "currency": "GBP",
                            "tax": {
                                "kind": "uk_vat",
                                "code": "20-std",
                                "rate_bp": 2000,
                            },
                        },
                        {
                            "account_id": "acc-bank",
                            "direction": "cr",
                            "amount_minor": 12000,
                            "currency": "GBP",
                            "bank_transaction_id": "btx-fixture",
                        },
                    ],
                    "proposed_by": "per-agent",
                    "approval_id": None,
                    "engagement_id": "eng-fixture",
                }
            ],
            "Document": [
                {
                    "id": "doc-fixture",
                    "sha256": "a" * 64,
                    "filename": "fictional-receipt.txt",
                    "mime": "text/plain",
                    "kind": "receipt",
                    "client_id": "cli-fixture",
                    "source": "fixture",
                    "received_world_time": "2026-05-20T10:00:00Z",
                    "engagement_id": "eng-fixture",
                }
            ],
        },
    )
    documents = root / "documents"
    documents.mkdir()
    content = b"Fictional fixture receipt\n"
    (documents / "fictional-receipt.txt").write_bytes(content)
    records = yaml.safe_load((root / "records.yaml").read_text(encoding="utf-8"))
    records["Document"][0]["filename"] = "documents/fictional-receipt.txt"
    records["Document"][0]["sha256"] = hashlib.sha256(content).hexdigest()
    _write_yaml(root / "records.yaml", records)
    return root


def test_compile_persists_yaml_fixtures_with_a_stable_digest(tmp_path: Path) -> None:
    world = _world_fixture(tmp_path / "fixture-world")
    first = compile_world(world, tmp_path / "first.db")
    second = compile_world(world, tmp_path / "second.db")

    assert first.manifest.world_id == "wld-fixture"
    assert first.state_digest == second.state_digest
    with SQLiteWorldView.open(first.database_path) as view:
        assert view.get(Client, "cli-fixture").name == "Fictional Trading Ltd"
        assert view.get(Journal, "jnl-fixture").lines[-1].amount_minor == 12000
        assert view.state_digest() == first.state_digest


def test_validation_runs_structural_gates_in_order_and_defers_wp10_gates(
    tmp_path: Path,
) -> None:
    report = validate_world(_world_fixture(tmp_path / "fixture-world"))

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


def test_validation_stops_before_later_gates_when_trap_coverage_fails(
    tmp_path: Path,
) -> None:
    world = _world_fixture(tmp_path / "fixture-world")
    _write_yaml(
        world / "traps.yaml",
        [
            {
                "trap_id": "trap-fixture-missing",
                "location_refs": ["doc-missing"],
                "conflicts_with": [],
                "expected_behaviour": "Flag the fictional missing item.",
                "tempting_behaviour": "Ignore it.",
                "grading_refs": ["duplicate_guard"],
                "severity": "minor",
                "visible_to_agent": "discoverable",
            }
        ],
    )

    report = validate_world(world)

    assert not report.passed
    assert [(gate.gate_id, gate.status) for gate in report.gates] == [
        ("schema_validation", "passed"),
        ("compile", "passed"),
        ("invariants", "passed"),
        ("trap_coverage", "failed"),
    ]


def test_structural_validation_rejects_closed_period_posting_without_approval(
    tmp_path: Path,
) -> None:
    world = _world_fixture(tmp_path / "fixture-world")
    records = yaml.safe_load((world / "records.yaml").read_text(encoding="utf-8"))
    records["AccountingPeriod"][0]["status"] = "closed"
    records["Journal"][0]["status"] = "posted"
    records["BankTransaction"][0]["classification_status"] = "classified"
    _write_yaml(world / "records.yaml", records)

    report = validate_world(world)

    assert [(gate.gate_id, gate.status) for gate in report.gates][-1] == (
        "invariants",
        "failed",
    )
    assert "I-4" in report.gates[-1].detail


def test_calendar_fixture_dates_are_real_dates() -> None:
    assert date(2026, 5, 20).isoformat() == "2026-05-20"


@pytest.mark.parametrize(
    ("relative_path", "replacement", "expected_message"),
    [
        (
            "documents/brightpath-may-001.txt",
            "tampered fictional source\n",
            "content hash",
        ),
        (
            "bank-feeds/brightpath-may.csv",
            "date,amount_minor,counterparty,reference\n2026-05-02,-1,Study Supplies,BP-MAY-001\n",
            "does not match its compiled transactions",
        ),
        (
            "opening-balances.csv",
            "entity_id,account_code,amount_minor\nent-brightpath,1000,1\nent-harper,1000,85000\nent-kestrel,1000,110000\n",
            "does not match posted opening journal balances",
        ),
    ],
)
def test_authoritative_sources_reject_tampering(
    tmp_path: Path, relative_path: str, replacement: str, expected_message: str
) -> None:
    source = Path(__file__).resolve().parents[2] / "worlds" / "uk-wyrley-brook"
    world = tmp_path / "tampered-world"
    shutil.copytree(source, world)
    (world / relative_path).write_text(replacement, encoding="utf-8")

    with pytest.raises(WorldCompileError, match=expected_message):
        compile_world(world, tmp_path / "world.db")


def test_structural_validation_checks_irq_and_embedded_references(
    tmp_path: Path,
) -> None:
    world = _world_fixture(tmp_path / "fixture-world")
    records = yaml.safe_load((world / "records.yaml").read_text(encoding="utf-8"))
    records["InformationRequest"] = [
        {
            "id": "irq-fixture",
            "client_id": "cli-fixture",
            "thread_id": None,
            "items": [{"description": "Missing proof", "refs": ["doc-missing"]}],
            "status": "draft",
            "engagement_id": "eng-fixture",
        }
    ]
    _write_yaml(world / "records.yaml", records)

    report = validate_world(world)

    assert report.gates[-1].gate_id == "compile"
    assert report.gates[-1].status == "failed"
    assert "ownership" in report.gates[-1].detail
