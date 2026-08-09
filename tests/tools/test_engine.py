"""WP-07 permissioned tools, derivation, event, and MCP contract coverage."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mirrorfirm.core.db import SQLiteWorldView, WorldStore
from mirrorfirm.core.models import (
    Action,
    Approval,
    BankTransaction,
    Event,
    Journal,
    Task,
)
from mirrorfirm.tools import MCPToolServer, WorldToolEngine
from mirrorfirm.tools.errors import ToolExecutionError
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
    assert {
        mutation.entity_kind
        for mutation in engine._list(Action)[-1].mutations  # noqa: SLF001
    } == {"Journal", "BankTransaction", "ProvenanceRecord"}

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
    with SQLiteWorldView.open(Path(snapshot["db_path"])) as view:
        assert view.actions()[-1].tool == "finish_episode"
        assert view.state_digest() == finished["state_digest"]

    action_count = len(engine._list(Action))  # noqa: SLF001
    rejected = engine.call("get_current_time", {})
    assert rejected.ok is False
    assert rejected.error is not None and rejected.error.code == "VALIDATION_ERROR"
    assert len(engine._list(Action)) == action_count  # noqa: SLF001


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


@pytest.mark.parametrize(
    ("starting_status", "target_status", "error_code"),
    [
        ("open", "blocked", "VALIDATION_ERROR"),
        ("open", "ready_for_review", "VALIDATION_ERROR"),
        ("open", "done", "REVIEW_REQUIRED"),
        ("done", "in_progress", "VALIDATION_ERROR"),
    ],
)
def test_task_lifecycle_rejects_illegal_transitions(
    engine: WorldToolEngine,
    starting_status: str,
    target_status: str,
    error_code: str,
) -> None:
    task = engine.store.load(Task, "tsk-brightpath-may-close")
    engine.store.save(
        task.model_copy(update={"status": starting_status, "blocked_on": None})
    )

    result = engine.call(
        "update_task_status",
        {"task_id": task.id, "status": target_status},
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == error_code
    assert engine.store.load(type(task), task.id).status == starting_status


def test_deferred_bank_data_is_hidden_from_list_calculation_and_provenance(
    engine: WorldToolEngine,
) -> None:
    engine.store.append_event(
        Event.model_validate(
            {
                "id": "evt-test-hidden-bank",
                "trigger": {
                    "kind": "at_time",
                    "world_time": (engine.now + timedelta(minutes=10)).isoformat(),
                },
                "payload": {
                    "kind": "new_bank_feed",
                    "bank_account_id": "bnk-brightpath",
                    "txn_fixture_refs": ["btx-brightpath-004"],
                },
            }
        )
    )

    listed = call(
        engine, "list_bank_transactions", {"bank_account_id": "bnk-brightpath"}
    )
    assert "btx-brightpath-004" not in {row["id"] for row in listed["transactions"]}
    calculated = engine.call(
        "calculate",
        {"expression": "x", "bindings": {"x": "btx-brightpath-004.amount_minor"}},
    )
    assert calculated.ok is False
    assert (
        calculated.error is not None and calculated.error.code == "UNRESOLVED_BINDING"
    )
    proposed = engine.call(
        "propose_journal",
        {
            "date": "2026-05-28",
            "memo": "Deferred evidence attempt",
            "lines": [
                {
                    "account_id": "acc-brightpath-travel",
                    "direction": "dr",
                    "amount_minor": 1,
                    "currency": "GBP",
                },
                {
                    "account_id": "acc-brightpath-bank",
                    "direction": "cr",
                    "amount_minor": 1,
                    "currency": "GBP",
                },
            ],
            "provenance_refs": ["btx-brightpath-004"],
        },
    )
    assert proposed.ok is False
    assert proposed.error is not None and proposed.error.code == "NOT_FOUND"


def test_other_clients_events_neither_surface_nor_fire(engine: WorldToolEngine) -> None:
    advanced = call(engine, "advance_time", {"minutes": 60 * 48})

    assert all("kestrel" not in item["event_id"] for item in advanced["fired_events"])
    assert engine.store.load(Event, "evt-uk02-kestrel-recon-deadline").fired is False
    cross_client = engine.call("get_task", {"task_id": "tsk-kestrel-may-recon"})
    assert (
        cross_client.error is not None and cross_client.error.code == "SCOPE_VIOLATION"
    )


def test_client_reply_documents_remain_hidden_until_the_reply_event(
    engine: WorldToolEngine,
) -> None:
    call(engine, "advance_time", {"minutes": 120})

    documents = call(engine, "list_documents", {})
    assert "doc-brightpath-may-480-receipt" not in {
        document["id"] for document in documents["documents"]
    }
    hidden = engine.call("read_document", {"doc_id": "doc-brightpath-may-480-receipt"})
    assert hidden.ok is False
    assert hidden.error is not None and hidden.error.code == "NOT_FOUND"


def test_event_and_tool_actions_preserve_real_start_and_end_times(
    engine: WorldToolEngine,
) -> None:
    started = engine.now
    advanced = call(engine, "advance_time", {"minutes": 60 * 48})
    actions = engine._list(Action)  # noqa: SLF001

    assert advanced["world_time"] == "2026-05-29T12:00:00Z"
    assert actions[-2].tool == "event.deadline"
    assert actions[-2].world_time_before == actions[-2].world_time_after
    assert actions[-1].tool == "advance_time"
    assert actions[-1].world_time_before == started
    assert actions[-1].world_time_after.isoformat() == "2026-05-29T12:00:00+00:00"


def test_mutations_and_action_are_rolled_back_together(
    engine: WorldToolEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_append = engine.store.append_action

    def fail_action(_: Action) -> None:
        monkeypatch.setattr(engine.store, "append_action", original_append)
        raise ToolExecutionError("VALIDATION_ERROR", "forced audit failure")

    monkeypatch.setattr(engine.store, "append_action", fail_action)
    result = engine.call(
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
    assert result.ok is False
    assert (
        engine.store.load(BankTransaction, "btx-brightpath-001").classification_status
        == "unclassified"
    )
    assert not any(journal.id.startswith("jnl-r") for journal in engine._list(Journal))  # noqa: SLF001


@pytest.mark.parametrize(
    ("transaction_id", "amount_minor", "tax", "expected_lines"),
    [
        (
            "btx-brightpath-001",
            -3,
            {"kind": "uk_vat", "code": "20-std", "rate_bp": 2000},
            [
                ("acc-brightpath-bank", "cr", 3),
                ("acc-brightpath-travel", "dr", 2),
                ("acc-2202", "dr", 1),
            ],
        ),
        (
            "btx-brightpath-002",
            -8400,
            {"kind": "uk_vat", "code": "blocked", "rate_bp": 2000},
            [
                ("acc-brightpath-bank", "cr", 8400),
                ("acc-brightpath-travel", "dr", 8400),
            ],
        ),
        (
            "btx-brightpath-003",
            36000,
            {"kind": "uk_vat", "code": "20-std", "rate_bp": 2000},
            [
                ("acc-brightpath-bank", "dr", 36000),
                ("acc-brightpath-travel", "cr", 30000),
                ("acc-2201", "cr", 6000),
            ],
        ),
        (
            "btx-brightpath-004",
            -5600,
            {"kind": "uk_vat", "code": "0-zero", "rate_bp": 0},
            [
                ("acc-brightpath-bank", "cr", 5600),
                ("acc-brightpath-travel", "dr", 5600),
            ],
        ),
        (
            "btx-brightpath-005",
            -14000,
            {"kind": "uk_vat", "code": "exempt", "rate_bp": 0},
            [
                ("acc-brightpath-bank", "cr", 14000),
                ("acc-brightpath-travel", "dr", 14000),
            ],
        ),
    ],
)
def test_vat_derivation_covers_rounding_blocked_output_and_zero_rated(
    engine: WorldToolEngine,
    transaction_id: str,
    amount_minor: int,
    tax: dict[str, object],
    expected_lines: list[tuple[str, str, int]],
) -> None:
    transaction = engine.store.load(BankTransaction, transaction_id)
    engine.store.save(transaction.model_copy(update={"amount_minor": amount_minor}))
    result = call(
        engine,
        "propose_classification",
        {
            "items": [
                {
                    "bank_transaction_id": transaction_id,
                    "account_id": "acc-brightpath-travel",
                    "tax": tax,
                    "provenance_refs": ["doc-brightpath-may-001"],
                }
            ]
        },
    )
    journal = engine.store.load(Journal, result["journal_ids"][0])
    assert [
        (line.account_id, line.direction, line.amount_minor) for line in journal.lines
    ] == expected_lines


def test_vat_split_mismatch_fails_without_mutating_the_transaction(
    engine: WorldToolEngine,
) -> None:
    result = engine.call(
        "propose_classification",
        {
            "items": [
                {
                    "bank_transaction_id": "btx-brightpath-001",
                    "splits": [
                        {"account_id": "acc-brightpath-travel", "gross_minor": 11999},
                        {"account_id": "acc-brightpath-travel", "gross_minor": 2},
                    ],
                    "provenance_refs": ["doc-brightpath-may-001"],
                }
            ]
        },
    )
    assert result.ok is False
    assert result.error is not None and result.error.code == "SPLIT_MISMATCH"
    assert (
        engine.store.load(BankTransaction, "btx-brightpath-001").classification_status
        == "unclassified"
    )
