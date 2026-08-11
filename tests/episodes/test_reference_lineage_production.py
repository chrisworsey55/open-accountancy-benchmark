"""Regression coverage for typed reference-evidence lineage gates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import Action
from mirrorfirm.episodes import (
    load_uk_episode_manifests,
    load_us_episode_manifests,
    run_reference_episode,
)
from mirrorfirm.episodes import references as reference_module
from mirrorfirm.episodes.references import (
    ReferenceCall,
    ReferenceScriptError,
    evidence_lineage_errors,
)
from mirrorfirm.worldgen import compile_world, validate_world

ROOT = Path(__file__).resolve().parents[2]
US_WORLD = ROOT / "worlds" / "us-lakeshore"
UK_WORLD = ROOT / "worlds" / "uk-wyrley-brook"


def _action(
    step: int, tool: str, input_payload: JsonValue, output_payload: JsonValue
) -> Action:
    """Build a syntactically valid append-only action for a lineage attack."""

    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    return Action(
        id=f"act-lineage-{step}",
        step=step,
        actor="per-agent",
        tool=tool,
        input_digest=logical_state_digest(input_payload),
        output_digest=logical_state_digest(output_payload),
        input_payload=input_payload,
        output_payload=output_payload,
        engagement_id="eng-lineage",
        world_time_before=now,
        world_time_after=now,
        mutations=[],
    )


@pytest.mark.parametrize(
    ("actions", "allowed_tools"),
    [
        (
            (
                _action(
                    1,
                    "get_context",
                    {},
                    {"error": {"code": "NOT_FOUND", "message": "doc-hidden"}},
                ),
                _action(2, "read_document", {"doc_id": "doc-hidden"}, {}),
            ),
            ("get_context", "read_document"),
        ),
        (
            (
                _action(
                    1,
                    "calculate",
                    {"expression": "1 + 1"},
                    {"value": "doc-hidden", "calculation_action_id": "act-lineage-1"},
                ),
                _action(2, "read_document", {"doc_id": "doc-hidden"}, {}),
            ),
            ("calculate", "read_document"),
        ),
        (
            (
                _action(
                    1,
                    "get_context",
                    {},
                    {"metadata": {"document_id": "doc-hidden"}},
                ),
                _action(2, "read_document", {"doc_id": "doc-hidden"}, {}),
            ),
            ("get_context", "read_document"),
        ),
        (
            (_action(1, "trial_balance", {"period_id": "prd-hidden"}, {}),),
            ("trial_balance",),
        ),
        (
            (
                _action(
                    1,
                    "calculate",
                    {
                        "expression": "1 + 1",
                        "bindings": {"provenance_ref": "prv-hidden"},
                    },
                    {},
                ),
            ),
            ("calculate",),
        ),
    ],
    ids=["failed-output", "free-text", "hidden-metadata", "period", "provenance"],
)
def test_lineage_rejects_ids_not_exposed_by_a_successful_typed_result(
    actions: tuple[Action, ...], allowed_tools: tuple[str, ...]
) -> None:
    """Errors and untyped payloads cannot teach a scripted agent material IDs."""

    assert evidence_lineage_errors(allowed_tools, actions)


def test_lineage_rejects_a_prefixless_typed_document_identifier() -> None:
    """Input-contract identifier fields are enforced independently of ID spelling."""

    errors = evidence_lineage_errors(
        ("read_document",),
        (_action(1, "read_document", {"doc_id": "xyz-hidden"}, {}),),
    )

    assert errors and "input=doc_id" in errors[0] and "xyz-hidden" in errors[0]


def test_lineage_rejects_a_prefixless_record_field_expression_in_any_binding() -> None:
    """Calculation bindings identify record-field references by value, not key name."""

    errors = evidence_lineage_errors(
        ("calculate",),
        (
            _action(
                1,
                "calculate",
                {
                    "expression": "amount",
                    "bindings": {"amount": "btx-hidden.amount_minor"},
                },
                {},
            ),
        ),
    )

    assert errors and "input=bindings.amount" in errors[0]
    assert "btx-hidden" in errors[0]


def test_lineage_accepts_a_later_use_of_a_successful_typed_calculation_output() -> None:
    """A prior valid typed result may expose a generated Action identifier."""

    actions = (
        _action(
            1,
            "calculate",
            {"expression": "1 + 1"},
            {"value": 2, "calculation_action_id": "act-calculation"},
        ),
        _action(
            2,
            "calculate",
            {
                "expression": "value + 1",
                "bindings": {"derived_from_ref": "act-calculation"},
            },
            {"value": 3, "calculation_action_id": "act-follow-on"},
        ),
    )

    assert not evidence_lineage_errors(("calculate",), actions)


def test_lineage_accepts_a_record_created_by_a_prior_typed_mutation() -> None:
    """A successful mutation may expose its newly created typed record ID."""

    actions = (
        _action(
            1,
            "propose_classification",
            {
                "items": [
                    {
                        "bank_transaction_id": "btx-visible",
                        "account_id": "acc-visible",
                        "provenance_refs": ["doc-visible"],
                    }
                ]
            },
            {"journal_ids": ["jnl-created"], "transaction_ids": ["btx-visible"]},
        ),
        _action(
            2,
            "request_approval",
            {
                "kind": "post_journal",
                "action_descriptor": {
                    "kind": "post_journal",
                    "journal_id": "jnl-created",
                },
                "rationale": "Fictional approval for a derived journal.",
                "provenance_refs": ["doc-visible"],
            },
            {},
        ),
    )

    assert not evidence_lineage_errors(
        ("propose_classification", "request_approval"),
        actions,
        initial_visible_ids=("btx-visible", "acc-visible", "doc-visible"),
    )


@pytest.mark.parametrize(
    "actions",
    [
        (
            _action(
                1,
                "calculate",
                {
                    "expression": "1 + 1",
                    "bindings": {"derived_from_ref": "act-same"},
                },
                {"value": 2, "calculation_action_id": "act-same"},
            ),
        ),
        (
            _action(
                1,
                "calculate",
                {
                    "expression": "1 + 1",
                    "bindings": {"derived_from_ref": "act-later"},
                },
                {"value": 2, "calculation_action_id": "act-first"},
            ),
            _action(
                2,
                "calculate",
                {"expression": "1 + 1"},
                {"value": 2, "calculation_action_id": "act-later"},
            ),
        ),
    ],
    ids=["same-action-output", "later-action-output"],
)
def test_lineage_rejects_same_or_later_action_output_as_discovery(
    actions: tuple[Action, ...],
) -> None:
    """An output becomes visible only after its own successful Action completes."""

    assert evidence_lineage_errors(("calculate",), actions)


def test_lineage_rejects_an_identifier_exposed_only_by_a_disallowed_tool() -> None:
    """A reference cannot learn identifiers through a tool absent from its manifest."""

    actions = (
        _action(
            1,
            "calculate",
            {"expression": "1 + 1"},
            {"value": 2, "calculation_action_id": "act-disallowed"},
        ),
        _action(2, "read_document", {"doc_id": "act-disallowed"}, {}),
    )

    errors = evidence_lineage_errors(("read_document",), actions)
    assert errors and "disallowed" in errors[0]
    assert any("act-disallowed" in error for error in errors)


def _prefixless_lineage_attack(kind: str) -> tuple[ReferenceCall, ...]:
    """Build one deliberately rejected but fully logged lineage attack."""

    if kind == "document":
        attack = ReferenceCall(
            "undiscovered_document",
            "read_document",
            {"doc_id": "xyz-hidden"},
            expected_error="NOT_FOUND",
        )
    elif kind == "calculation":
        attack = ReferenceCall(
            "undiscovered_calculation",
            "calculate",
            {
                "expression": "amount",
                "bindings": {"amount": "btx-hidden.amount_minor"},
            },
            expected_error="NOT_FOUND",
        )
    else:
        raise AssertionError(f"unknown lineage attack {kind!r}")
    return (
        ReferenceCall("context", "get_context", {}),
        attack,
        ReferenceCall("finish", "finish_episode", {"summary": "Fictional end."}),
    )


@pytest.mark.parametrize(
    ("attack", "expected_path"),
    [("document", "input=doc_id"), ("calculation", "input=bindings.amount")],
)
def test_run_reference_episode_rejects_prefixless_lineage_attacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
    expected_path: str,
) -> None:
    """The production scripted-reference boundary is fail-closed on lineage."""

    episode = next(
        item for item in load_us_episode_manifests() if item.episode_id == "epi-us-01"
    )
    monkeypatch.setattr(
        reference_module,
        "reference_calls_for",
        lambda _: _prefixless_lineage_attack(attack),
    )
    compiled = compile_world(US_WORLD, tmp_path / "compiled.db")

    with pytest.raises(ReferenceScriptError, match=expected_path):
        run_reference_episode(
            episode,
            compiled.database_path,
            world_root=US_WORLD,
            results_root=tmp_path / "results",
        )


@pytest.mark.parametrize("attack", ["document", "calculation"])
def test_validate_world_fails_its_reference_gate_for_prefixless_lineage_attacks(
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """The deferred reference-run validation gate cannot bypass lineage checks."""

    monkeypatch.setattr(
        reference_module,
        "reference_calls_for",
        lambda _: _prefixless_lineage_attack(attack),
    )

    report = validate_world(US_WORLD)
    reference_gate = next(
        gate for gate in report.gates if gate.gate_id == "reference_runs"
    )
    assert reference_gate.status == "failed"
    assert "reference lineage" in reference_gate.detail


def test_every_reference_fails_lineage_validation_when_an_essential_read_is_removed(
    tmp_path: Path,
) -> None:
    """Each reference loses a required model-visible tool path when it is removed."""

    cases = (
        (
            UK_WORLD,
            load_uk_episode_manifests,
            {
                "epi-uk-01": ("list_tasks", "aggregate_table", "list_documents"),
                "epi-uk-02": ("list_tasks", "aggregate_table", "list_documents"),
                "epi-uk-03": ("list_tasks", "list_threads"),
                "epi-uk-04": ("list_tasks", "aggregate_table"),
            },
        ),
        (
            US_WORLD,
            load_us_episode_manifests,
            {
                "epi-us-01": (
                    "get_context",
                    "list_tasks",
                    "list_bank_transactions",
                    "list_documents",
                ),
                "epi-us-02": (
                    "get_context",
                    "list_tasks",
                    "list_bank_transactions",
                    "list_documents",
                ),
                "epi-us-03": ("list_tasks", "list_threads"),
            },
        ),
    )
    for world, load_episodes, essential_tools in cases:
        compiled = compile_world(world, tmp_path / f"{world.name}.db")
        for episode in load_episodes():
            result = run_reference_episode(
                episode,
                compiled.database_path,
                world_root=world,
                results_root=tmp_path / f"results-{episode.episode_id}",
            )
            with SQLiteWorldView.open(result.run.final_snapshot.db_path) as final:
                actions = final.actions()
            for removed_tool in essential_tools[episode.episode_id]:
                errors = evidence_lineage_errors(
                    [tool for tool in episode.allowed_tools if tool != removed_tool],
                    actions,
                )
                assert errors, (
                    episode.episode_id,
                    removed_tool,
                )
