"""WP-07 permissioned tools, derivation, event, and MCP contract coverage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import Action, Approval, BankTransaction, Event, Journal
from mirrorfirm.tools import MCPToolServer, WorldToolEngine
from mirrorfirm.tools.registry import (
    ALL_INTERNAL_ROLES,
    BOOKKEEPING_ROLES,
    DEFAULT_REGISTRY,
)
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"

EXPECTED_TOOL_NAMES = {
    "get_context",
    "get_current_time",
    "list_tasks",
    "get_task",
    "list_clients",
    "get_client",
    "list_documents",
    "read_document",
    "read_table",
    "search_documents",
    "list_bank_transactions",
    "query_ledger",
    "trial_balance",
    "list_threads",
    "read_thread",
    "get_reconciliation_status",
    "list_approvals",
    "list_review_notes",
    "aggregate_table",
    "calculate",
    "compare_datasets",
    "propose_classification",
    "propose_journal",
    "request_approval",
    "draft_information_request",
    "update_draft",
    "send_information_request",
    "draft_reply",
    "send_reply",
    "create_workpaper",
    "update_workpaper",
    "finalize_workpaper",
    "update_task_status",
    "submit_for_review",
    "escalate",
    "advance_time",
    "finish_episode",
}


@pytest.fixture
def engine(tmp_path: Path) -> WorldToolEngine:
    compiled = compile_world(WORLD, tmp_path / "world.db")
    store = WorldStore.open(compiled.database_path)
    try:
        yield WorldToolEngine(
            store,
            world_root=WORLD,
            actor_id="per-agent",
            engagement_id="eng-brightpath-bookkeeping",
        )
    finally:
        store.close()


def call(
    engine: WorldToolEngine, name: str, payload: dict[str, object]
) -> dict[str, object]:
    result = engine.call(name, payload)
    assert result.ok, result.error
    assert isinstance(result.result, dict)
    assert result.action_id is not None
    return result.result


def test_registry_defines_every_specified_tool_and_mcp_schema(
    engine: WorldToolEngine,
) -> None:
    definitions = DEFAULT_REGISTRY.definitions()

    assert {definition.name for definition in definitions} == EXPECTED_TOOL_NAMES
    assert all(
        definition.output_model.__name__ == "ToolCallResult"
        for definition in definitions
    )
    assert all("SCOPE_VIOLATION" in definition.errors for definition in definitions)
    assert all(
        definition.name not in {"bash", "write_file", "delete_file"}
        for definition in definitions
    )

    server = MCPToolServer(engine)
    mcp_tools = server.list_tools()["tools"]
    assert {tool["name"] for tool in mcp_tools} == EXPECTED_TOOL_NAMES
    assert server.call_tool("get_current_time", {})["isError"] is False


def test_permission_matrix_covers_every_registered_tool() -> None:
    read_and_compute_tools = {
        "get_context",
        "get_current_time",
        "list_tasks",
        "get_task",
        "list_clients",
        "get_client",
        "list_documents",
        "read_document",
        "read_table",
        "search_documents",
        "list_bank_transactions",
        "query_ledger",
        "trial_balance",
        "list_threads",
        "read_thread",
        "get_reconciliation_status",
        "list_approvals",
        "list_review_notes",
        "aggregate_table",
        "calculate",
        "compare_datasets",
    }

    for definition in DEFAULT_REGISTRY.definitions():
        expected_roles = (
            ALL_INTERNAL_ROLES
            if definition.name in read_and_compute_tools
            else BOOKKEEPING_ROLES
        )
        assert definition.roles == expected_roles


def test_permissions_and_client_isolation_fail_safely_and_are_audited(
    engine: WorldToolEngine,
) -> None:
    denied_engine = WorldToolEngine(
        engine.store,
        world_root=WORLD,
        actor_id="per-brightpath-contact",
        engagement_id="eng-brightpath-bookkeeping",
    )

    denied = denied_engine.call("get_context", {})
    assert denied.ok is False
    assert denied.error is not None and denied.error.code == "PERMISSION_DENIED"

    leaked = engine.call("get_client", {"client_id": "cli-harper"})
    assert leaked.ok is False
    assert leaked.error is not None and leaked.error.code == "SCOPE_VIOLATION"
    assert any(action.tool == "get_client" for action in engine._list(Action))  # noqa: SLF001
    document = call(engine, "read_document", {"doc_id": "doc-brightpath-may-001"})
    assert "Bank reference: BP-MAY-001" in document["content"]


def test_classification_derives_balanced_uk_vat_journal_and_duration(
    engine: WorldToolEngine,
) -> None:
    before = engine.now
    result = call(
        engine,
        "propose_classification",
        {
            "items": [
                {
                    "bank_transaction_id": "btx-brightpath-001",
                    "account_id": "acc-brightpath-travel",
                    "tax": {"kind": "uk_vat", "code": "20-std", "rate_bp": 2000},
                    "provenance_refs": ["doc-brightpath-may-001"],
                }
            ]
        },
    )
    journal = engine.store.load(Journal, result["journal_ids"][0])

    assert [
        (line.account_id, line.direction, line.amount_minor) for line in journal.lines
    ] == [
        ("acc-brightpath-bank", "cr", 12000),
        ("acc-brightpath-travel", "dr", 10000),
        ("acc-2202", "dr", 2000),
    ]
    assert (
        engine.store.load(BankTransaction, "btx-brightpath-001").classification_status
        == "proposed"
    )
    assert engine.now == before + timedelta(minutes=2)
    assert engine._list(Action)[-1].world_time_before == before  # noqa: SLF001
    assert engine._list(Action)[-1].world_time_after == engine.now  # noqa: SLF001

    split_error = engine.call(
        "propose_classification",
        {
            "items": [
                {
                    "bank_transaction_id": "btx-brightpath-002",
                    "splits": [
                        {"account_id": "acc-brightpath-travel", "gross_minor": 8300}
                    ],
                    "provenance_refs": ["doc-brightpath-may-002"],
                }
            ]
        },
    )
    assert split_error.ok is False
    assert split_error.error is not None and split_error.error.code == "SPLIT_MISMATCH"
    assert (
        engine.store.load(BankTransaction, "btx-brightpath-002").classification_status
        == "unclassified"
    )


def test_after_entity_reply_binds_on_send_and_stops_at_next_event(
    engine: WorldToolEngine,
) -> None:
    drafted = call(
        engine,
        "draft_information_request",
        {
            "client_id": "cli-brightpath",
            "items": [
                {
                    "description": "Please send the fictional receipt.",
                    "refs": ["btx-brightpath-027"],
                }
            ],
            "body": "Please send the fictional receipt for the May transaction.",
        },
    )
    message_id = drafted["message"]["id"]
    sent = call(engine, "send_information_request", {"draft_message_id": message_id})
    assert sent["information_request"]["status"] == "sent"

    advanced = call(engine, "advance_time", {"minutes": 60 * 48})
    assert advanced["stopped_early"] is True
    assert advanced["fired_events"] == [
        {
            "event_id": "evt-uk01-brightpath-reply",
            "kind": "client_reply",
            "surface_refs": [drafted["thread"]["id"]],
        }
    ]
    thread = call(engine, "read_thread", {"thread_id": drafted["thread"]["id"]})
    assert thread["messages"][-1]["direction"] == "inbound"
    assert thread["messages"][-1]["attachments"] == ["doc-brightpath-may-480-receipt"]


def test_granted_approval_event_auto_executes_the_requested_posting(
    engine: WorldToolEngine,
) -> None:
    classified = call(
        engine,
        "propose_classification",
        {
            "items": [
                {
                    "bank_transaction_id": "btx-brightpath-003",
                    "account_id": "acc-brightpath-travel",
                    "tax": {"kind": "uk_vat", "code": "20-std", "rate_bp": 2000},
                    "provenance_refs": ["doc-brightpath-may-003"],
                }
            ]
        },
    )
    journal_id = classified["journal_ids"][0]
    requested = call(
        engine,
        "request_approval",
        {
            "kind": "post_journal",
            "action_descriptor": {"kind": "post_journal", "journal_id": journal_id},
            "rationale": "Please post the supported fictional classification.",
            "provenance_refs": ["doc-brightpath-may-003"],
        },
    )
    approval_id = requested["approval"]["id"]
    engine.store.append_event(
        Event.model_validate(
            {
                "id": "evt-test-approval-grant",
                "trigger": {
                    "kind": "after_entity",
                    "entity_kind": "approval",
                    "match": {"approval_kind": "post_journal", "status": "requested"},
                    "offset": {"minutes": 1},
                },
                "payload": {
                    "kind": "approval_decision",
                    "decision": "granted",
                    "note": "Fictional approval.",
                },
            }
        )
    )

    call(engine, "advance_time", {"minutes": 10})

    assert engine.store.load(Approval, approval_id).status == "granted"
    assert engine.store.load(Journal, journal_id).status == "posted"
    assert (
        engine.store.load(BankTransaction, "btx-brightpath-003").classification_status
        == "classified"
    )
    assert any(
        action.actor == "per-sys" and action.tool == "system.execute_approval"
        for action in engine._list(Action)
    )  # noqa: SLF001


def test_new_bank_feed_remains_hidden_until_its_event_fires(
    engine: WorldToolEngine,
) -> None:
    engine.store.append_event(
        Event.model_validate(
            {
                "id": "evt-test-new-feed",
                "trigger": {
                    "kind": "at_time",
                    "world_time": (engine.now + timedelta(minutes=1)).isoformat(),
                },
                "payload": {
                    "kind": "new_bank_feed",
                    "bank_account_id": "bnk-brightpath",
                    "txn_fixture_refs": ["btx-brightpath-004"],
                },
            }
        )
    )

    before = call(
        engine, "list_bank_transactions", {"bank_account_id": "bnk-brightpath"}
    )
    assert "btx-brightpath-004" not in {row["id"] for row in before["transactions"]}

    call(engine, "advance_time", {"minutes": 2})
    after = call(
        engine, "list_bank_transactions", {"bank_account_id": "bnk-brightpath"}
    )
    assert "btx-brightpath-004" in {row["id"] for row in after["transactions"]}


def test_finish_episode_materializes_a_final_snapshot(engine: WorldToolEngine) -> None:
    finished = call(
        engine,
        "finish_episode",
        {
            "summary": "The fictional close is ready for review.",
            "deliverable_refs": [],
            "unresolved_items": [],
        },
    )

    snapshot = finished["final_snapshot"]
    assert Path(snapshot["db_path"]).is_file()
    assert snapshot["state_digest"] == finished["state_digest"]


def test_reconciliation_workpaper_requires_a_tie_or_explicit_unresolved_item(
    engine: WorldToolEngine,
) -> None:
    result = engine.call(
        "create_workpaper",
        {
            "task_id": "tsk-brightpath-may-close",
            "body": {
                "kind": "bank_reconciliation",
                "bank_account_id": "bnk-brightpath",
                "period_id": "prd-brightpath-may",
                "statement_end_minor": 10000,
                "ledger_end_minor": 9990,
                "outstanding": [],
                "unresolved": [],
            },
        },
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "RECON_DOES_NOT_TIE"
