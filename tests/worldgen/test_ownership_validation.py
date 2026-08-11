"""Regression coverage for compile-time engagement ownership validation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.worldgen import WorldCompileError, compile_world, validate_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "us-lakeshore"


def _write_records(world: Path, records: dict[str, object]) -> None:
    (world / "records.yaml").write_text(
        yaml.safe_dump(records, sort_keys=False), encoding="utf-8"
    )


def _write_events(world: Path, events: dict[str, object]) -> None:
    (world / "events.yaml").write_text(
        yaml.safe_dump(events, sort_keys=False), encoding="utf-8"
    )


def _copy_malformed_world(tmp_path: Path, attack: str) -> Path:
    """Copy the US world and apply one ownership-only fixture corruption."""

    world = tmp_path / attack
    shutil.copytree(WORLD, world)
    records = yaml.safe_load((world / "records.yaml").read_text(encoding="utf-8"))
    assert isinstance(records, dict)

    if attack == "missing_document_owner":
        engagements = records["Engagement"]
        documents = records["Document"]
        assert isinstance(engagements, list) and isinstance(documents, list)
        engagements.append(
            {
                "id": "eng-cedarline-secondary",
                "client_id": "cli-cedarline",
                "scope": "bookkeeping",
                "period_ids": [],
                "status": "active",
            }
        )
        documents[0].pop("engagement_id")
    elif attack == "foreign_document_owner":
        documents = records["Document"]
        assert isinstance(documents, list)
        documents[0]["engagement_id"] = "eng-marlowe-bookkeeping"
    elif attack == "dangling_bank_owner":
        accounts = records["BankAccount"]
        assert isinstance(accounts, list)
        accounts[0]["engagement_id"] = "eng-does-not-exist"
    elif attack == "conflicting_journal_owner":
        journals = records["Journal"]
        assert isinstance(journals, list)
        journals[0]["engagement_id"] = "eng-marlowe-bookkeeping"
    elif attack == "missing_approval_owner":
        approvals = records["Approval"]
        assert isinstance(approvals, list)
        approvals[0].pop("engagement_id")
    elif attack == "dangling_approval_owner":
        approvals = records["Approval"]
        assert isinstance(approvals, list)
        approvals[0]["engagement_id"] = "eng-does-not-exist"
    elif attack == "foreign_approval_owner":
        approvals = records["Approval"]
        assert isinstance(approvals, list)
        approvals[0]["engagement_id"] = "eng-marlowe-bookkeeping"
    elif attack == "contradictory_approval_owner":
        engagements = records["Engagement"]
        approvals = records["Approval"]
        assert isinstance(engagements, list) and isinstance(approvals, list)
        engagements.append(
            {
                "id": "eng-cedarline-secondary",
                "client_id": "cli-cedarline",
                "scope": "bookkeeping",
                "period_ids": [],
                "status": "active",
            }
        )
        approvals[0]["engagement_id"] = "eng-cedarline-secondary"
    elif attack == "cross_engagement_journal_line":
        journals = records["Journal"]
        assert isinstance(journals, list)
        marlowe = next(
            journal for journal in journals if journal["id"] == "jnl-marlowe-opening"
        )
        marlowe["lines"][0]["bank_transaction_id"] = "btx-cedarline-001"
    elif attack == "foreign_thread_owner":
        threads = records["Thread"]
        assert isinstance(threads, list)
        threads[0]["engagement_id"] = "eng-cedarline-bookkeeping"
    elif attack == "cross_engagement_message_attachment":
        messages = records["Message"]
        assert isinstance(messages, list)
        messages[0]["attachments"] = ["doc-cedarline-materials-receipt"]
    elif attack == "foreign_information_request":
        requests = records.setdefault("InformationRequest", [])
        assert isinstance(requests, list)
        requests.append(
            {
                "id": "irq-marlowe-cross-scope",
                "client_id": "cli-marlowe",
                "thread_id": "thr-marlowe-benchmark-request",
                "items": [
                    {
                        "description": "Fictional cross-scope request",
                        "refs": ["doc-marlowe-benchmark-memo"],
                    }
                ],
                "status": "draft",
                "engagement_id": "eng-cedarline-bookkeeping",
            }
        )
    elif attack == "cross_engagement_workpaper":
        workpapers = records.setdefault("Workpaper", [])
        assert isinstance(workpapers, list)
        workpapers.append(
            {
                "id": "wpp-marlowe-cross-scope",
                "engagement_id": "eng-cedarline-bookkeeping",
                "task_id": "tsk-marlowe-may-recon",
                "body": {"kind": "query_log", "items": []},
                "status": "draft",
                "created_by": "per-agent",
                "created_world_time": "2026-05-28T13:00:00Z",
            }
        )
    elif attack == "cross_engagement_review_note":
        notes = records.setdefault("ReviewNote", [])
        assert isinstance(notes, list)
        notes.append(
            {
                "id": "rvn-marlowe-cross-scope",
                "engagement_id": "eng-cedarline-bookkeeping",
                "target_ref": "tsk-marlowe-may-recon",
                "author_id": "per-reviewer",
                "body": "Fictional cross-scope note.",
                "created_world_time": "2026-05-28T13:00:00Z",
                "status": "open",
            }
        )
    elif attack == "cross_client_provenance":
        provenance = records.setdefault("ProvenanceRecord", [])
        assert isinstance(provenance, list)
        provenance.append(
            {
                "id": "prv-marlowe-cross-client",
                "subject_ref": "btx-marlowe-001",
                "basis_ref": "doc-cedarline-materials-receipt",
                "relation": "supported_by",
            }
        )
    elif attack == "cross_engagement_action_mutation":
        actions = records.setdefault("Action", [])
        assert isinstance(actions, list)
        empty_digest = logical_state_digest({})
        actions.append(
            {
                "id": "act-fixture-cross-scope",
                "step": 1,
                "actor": "per-agent",
                "tool": "calculate",
                "input_digest": empty_digest,
                "output_digest": empty_digest,
                "input_payload": {},
                "output_payload": {},
                "engagement_id": "eng-cedarline-bookkeeping",
                "world_time_before": "2026-05-28T13:00:00Z",
                "world_time_after": "2026-05-28T13:00:00Z",
                "mutations": [
                    {
                        "entity_kind": "Task",
                        "entity_id": "tsk-marlowe-may-recon",
                        "change": "updated",
                        "summary": "Fictional invalid scope mutation.",
                    }
                ],
            }
        )
    elif attack == "cross_engagement_event_reference":
        events = yaml.safe_load((world / "events.yaml").read_text(encoding="utf-8"))
        assert isinstance(events, dict)
        event_list = events["events"]
        assert isinstance(event_list, list)
        deadline = next(
            event
            for event in event_list
            if event["id"] == "evt-us02-marlowe-recon-deadline"
        )
        deadline["engagement_id"] = "eng-cedarline-bookkeeping"
        _write_events(world, events)
    else:
        raise AssertionError(f"unknown attack {attack!r}")

    _write_records(world, records)
    return world


@pytest.mark.parametrize(
    "attack",
    [
        "missing_document_owner",
        "foreign_document_owner",
        "dangling_bank_owner",
        "conflicting_journal_owner",
        "missing_approval_owner",
        "dangling_approval_owner",
        "foreign_approval_owner",
        "contradictory_approval_owner",
        "cross_engagement_journal_line",
        "foreign_thread_owner",
        "cross_engagement_message_attachment",
        "foreign_information_request",
        "cross_engagement_workpaper",
        "cross_engagement_review_note",
        "cross_client_provenance",
        "cross_engagement_action_mutation",
        "cross_engagement_event_reference",
    ],
)
def test_compile_and_validate_reject_malformed_engagement_ownership(
    tmp_path: Path, attack: str
) -> None:
    """Ownership must be a compile-time invariant, not only a runtime scope check."""

    world = _copy_malformed_world(tmp_path, attack)

    with pytest.raises(WorldCompileError, match="ownership"):
        compile_world(world, tmp_path / f"{attack}.db")

    report = validate_world(world)
    assert not report.passed


@pytest.mark.parametrize(
    "world",
    [ROOT / "worlds" / "uk-wyrley-brook", ROOT / "worlds" / "us-lakeshore"],
)
def test_authored_worlds_pass_complete_engagement_ownership_validation(
    tmp_path: Path, world: Path
) -> None:
    """Both authored packets carry unambiguous explicit operational ownership."""

    compiled = compile_world(world, tmp_path / f"{world.name}.db")
    assert compiled.state_digest
    assert validate_world(world).passed


def test_explicit_ownership_is_part_of_the_compiled_logical_state_digest(
    tmp_path: Path,
) -> None:
    """A valid reassignment to another engagement cannot be digest-invisible."""

    original = compile_world(WORLD, tmp_path / "original.db")
    reassigned_world = tmp_path / "reassigned"
    shutil.copytree(WORLD, reassigned_world)
    records = yaml.safe_load(
        (reassigned_world / "records.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(records, dict)
    engagements = records["Engagement"]
    accounts = records["BankAccount"]
    assert isinstance(engagements, list) and isinstance(accounts, list)
    engagements.append(
        {
            "id": "eng-cedarline-secondary",
            "client_id": "cli-cedarline",
            "scope": "bookkeeping",
            "period_ids": [],
            "status": "active",
        }
    )
    accounts[0]["engagement_id"] = "eng-cedarline-secondary"
    _write_records(reassigned_world, records)

    reassigned = compile_world(reassigned_world, tmp_path / "reassigned.db")

    assert reassigned.state_digest != original.state_digest
