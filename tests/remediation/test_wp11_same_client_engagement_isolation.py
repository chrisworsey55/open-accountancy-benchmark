"""Systematic same-client, different-engagement tool-boundary regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import Engagement
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "us-lakeshore"


@pytest.fixture
def secondary_marlowe_engine(tmp_path: Path) -> WorldToolEngine:
    """Run a second bookkeeping engagement for the same fictional client."""

    compiled = compile_world(WORLD, tmp_path / "world.db")
    store = WorldStore.open(compiled.database_path)
    store.save(
        Engagement(
            id="eng-marlowe-secondary",
            client_id="cli-marlowe",
            scope="bookkeeping",
            # The primary engagement alone owns the May period.  This proves that
            # client equality cannot lend its records to a second engagement.
            period_ids=[],
            status="active",
        )
    )
    try:
        yield WorldToolEngine(
            store,
            world_root=WORLD,
            actor_id="per-agent",
            engagement_id="eng-marlowe-secondary",
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        ("read_document", {"doc_id": "doc-marlowe-may-statement"}),
        ("search_documents", {"query": "Marlowe"}),
        ("list_bank_transactions", {"bank_account_id": "bnk-marlowe"}),
        ("query_ledger", {"period": "prd-marlowe-may"}),
        ("trial_balance", {"period_id": "prd-marlowe-may"}),
        (
            "get_reconciliation_status",
            {"bank_account_id": "bnk-marlowe", "period_id": "prd-marlowe-may"},
        ),
        ("read_thread", {"thread_id": "thr-marlowe-benchmark-request"}),
        ("get_task", {"task_id": "tsk-marlowe-may-recon"}),
        (
            "update_task_status",
            {"task_id": "tsk-marlowe-may-recon", "status": "in_progress"},
        ),
        (
            "create_workpaper",
            {
                "task_id": "tsk-marlowe-may-recon",
                "body": {"kind": "query_log", "items": []},
            },
        ),
        (
            "submit_for_review",
            {
                "task_id": "tsk-marlowe-may-recon",
                "workpaper_ids": ["wpp-marlowe-foreign"],
                "summary": "Unauthorized secondary-engagement submission.",
            },
        ),
        (
            "propose_classification",
            {
                "items": [
                    {
                        "bank_transaction_id": "btx-marlowe-001",
                        "account_id": "acc-marlowe-ar",
                        "provenance_refs": ["doc-marlowe-may-statement"],
                    }
                ]
            },
        ),
        (
            "propose_journal",
            {
                "date": "2026-05-20",
                "memo": "Unauthorized secondary-engagement journal",
                "lines": [
                    {
                        "account_id": "acc-marlowe-ar",
                        "direction": "dr",
                        "amount_minor": 1,
                        "currency": "USD",
                    },
                    {
                        "account_id": "acc-marlowe-bank",
                        "direction": "cr",
                        "amount_minor": 1,
                        "currency": "USD",
                    },
                ],
                "provenance_refs": ["doc-marlowe-nsf-notice"],
            },
        ),
        (
            "draft_reply",
            {
                "thread_id": "thr-marlowe-benchmark-request",
                "body": "Unauthorized secondary-engagement reply.",
                "attachments": [],
            },
        ),
        (
            "compare_datasets",
            {
                "left": [
                    {
                        "id": "foreign",
                        "amount_minor": 1,
                        "source_ref": "doc-marlowe-nsf-notice",
                    }
                ],
                "right": [
                    {
                        "id": "foreign",
                        "amount_minor": 1,
                        "source_ref": "doc-marlowe-nsf-notice",
                    }
                ],
                "keys": ["id"],
                "compare_fields": ["amount_minor"],
                "tolerance_minor": 0,
            },
        ),
        (
            "calculate",
            {
                "expression": "amount",
                "bindings": {"amount": "btx-marlowe-001.amount_minor"},
            },
        ),
        (
            "request_approval",
            {
                "kind": "post_journal",
                "action_descriptor": {
                    "kind": "post_journal",
                    "journal_id": "jnl-marlowe-opening",
                },
                "rationale": "Unauthorized secondary-engagement approval.",
                "provenance_refs": ["doc-marlowe-may-statement"],
            },
        ),
        (
            "escalate",
            {
                "to_role": "partner",
                "subject_refs": ["doc-marlowe-nsf-notice"],
                "reason": "Unauthorized secondary-engagement escalation.",
            },
        ),
        ("advance_time", {"minutes": 240}),
        ("list_approvals", {}),
        ("list_review_notes", {}),
    ],
)
def test_client_owned_records_cannot_cross_same_client_engagement_boundary(
    secondary_marlowe_engine: WorldToolEngine,
    tool: str,
    payload: dict[str, object],
) -> None:
    """Client equality alone never authorises read, mutation, message, or provenance."""

    result = secondary_marlowe_engine.call(tool, payload)

    if tool == "search_documents":
        assert result.ok
        assert result.result == {"matches": []}
        return
    if tool == "list_approvals":
        assert result.ok
        assert result.result == {"approvals": []}
        return
    if tool == "list_review_notes":
        assert result.ok
        assert result.result == {"review_notes": []}
        return
    if tool == "advance_time":
        assert result.ok
        assert result.result is not None and result.result["fired_events"] == []
        return
    assert not result.ok
    assert result.error is not None and result.error.code == "SCOPE_VIOLATION"


def test_primary_engagement_retains_its_explicitly_owned_records(
    tmp_path: Path,
) -> None:
    """Adding another engagement must not make the primary workspace unusable."""

    compiled = compile_world(WORLD, tmp_path / "world.db")
    with WorldStore.open(compiled.database_path) as store:
        store.save(
            Engagement(
                id="eng-marlowe-secondary",
                client_id="cli-marlowe",
                scope="bookkeeping",
                period_ids=[],
                status="active",
            )
        )
        engine = WorldToolEngine(
            store,
            world_root=WORLD,
            actor_id="per-agent",
            engagement_id="eng-marlowe-bookkeeping",
        )

        assert engine.call("read_document", {"doc_id": "doc-marlowe-may-statement"}).ok
        assert engine.call(
            "list_bank_transactions", {"bank_account_id": "bnk-marlowe"}
        ).ok
        assert engine.call("query_ledger", {"period": "prd-marlowe-may"}).ok
        assert engine.call(
            "read_thread", {"thread_id": "thr-marlowe-benchmark-request"}
        ).ok
