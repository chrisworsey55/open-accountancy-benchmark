"""Regression coverage for immutable approval-gated outbound drafts."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import Action, Approval, InformationRequest, Message, Thread
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "us-lakeshore"


def _engine(tmp_path: Path) -> WorldToolEngine:
    compiled = compile_world(WORLD, tmp_path / "world.db")
    store = WorldStore.open(compiled.database_path)
    return WorldToolEngine(
        store,
        world_root=WORLD,
        actor_id="per-agent",
        engagement_id="eng-cedarline-bookkeeping",
        active_event_ids=["evt-us01-cedarline-approval"],
    )


def _call(
    engine: WorldToolEngine, name: str, payload: dict[str, object]
) -> dict[str, object]:
    result = engine.call(name, payload)
    assert result.ok, result.error
    assert isinstance(result.result, dict)
    return result.result


def _approved_irq(
    engine: WorldToolEngine, *, attachments: list[str] | None = None
) -> tuple[str, str]:
    drafted = _call(
        engine,
        "draft_information_request",
        {
            "client_id": "cli-cedarline",
            "items": [
                {
                    "description": "Please provide the fictional receipt.",
                    "refs": ["btx-cedarline-003"],
                }
            ],
            "body": "Please provide the fictional receipt.",
            "attachments": attachments or [],
        },
    )
    message = drafted["message"]
    assert isinstance(message, dict)
    message_id = message["id"]
    assert isinstance(message_id, str)
    approval = _call(
        engine,
        "request_approval",
        {
            "kind": "send_external_message",
            "action_descriptor": {
                "kind": "send_external_message",
                "draft_message_id": message_id,
            },
            "rationale": "The fictional evidence request needs reviewer approval.",
            "provenance_refs": ["btx-cedarline-003"],
        },
    )["approval"]
    assert isinstance(approval, dict)
    approval_id = approval["id"]
    assert isinstance(approval_id, str)
    return message_id, approval_id


def test_unchanged_approved_draft_is_sent_once_with_its_approved_state(
    tmp_path: Path,
) -> None:
    """The system path sends the immutable approved draft once, not a replacement."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(engine)

        advanced = _call(engine, "advance_time", {"minutes": 240})

        message = engine.store.load(Message, message_id)
        approval = engine.store.load(Approval, approval_id)
        assert advanced["fired_events"]
        assert approval.status == "granted"
        assert message.status == "sent"
        assert (
            sum(
                action.tool == "system.execute_approval"
                for action in engine._list(Action)
            )
            == 1
        )
    finally:
        engine.store.close()


@pytest.mark.parametrize("mutation", ["body", "attachments", "irq_refs"])
def test_mutating_any_approved_irq_component_expires_the_approval_before_event_send(
    tmp_path: Path, mutation: str
) -> None:
    """Whitespace, attachments, and IRQ references are part of one approved payload."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(engine)
        if mutation == "body":
            _call(
                engine,
                "update_draft",
                {
                    "message_id": message_id,
                    "body": "Please provide the fictional receipt. ",
                },
            )
        elif mutation == "attachments":
            _call(
                engine,
                "update_draft",
                {
                    "message_id": message_id,
                    "attachments": ["doc-cedarline-materials-receipt"],
                },
            )
        else:
            _call(
                engine,
                "update_draft",
                {
                    "message_id": message_id,
                    "items": [
                        {
                            "description": "Please provide the fictional receipt.",
                            "refs": ["btx-cedarline-002"],
                        }
                    ],
                },
            )

        _call(engine, "advance_time", {"minutes": 240})

        assert engine.store.load(Approval, approval_id).status == "expired"
        assert engine.store.load(Message, message_id).status == "draft"
        assert not any(
            action.tool == "system.execute_approval" for action in engine._list(Action)
        )
    finally:
        engine.store.close()


@pytest.mark.parametrize("mutation", ["subject", "recipients", "sender"])
def test_out_of_band_send_relevant_mutation_cannot_reuse_approved_draft(
    tmp_path: Path, mutation: str
) -> None:
    """The event path independently checks the canonical state, not only API updates."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(engine)
        message = engine.store.load(Message, message_id)
        thread = engine.store.load(Thread, message.thread_id)
        if mutation == "subject":
            engine.store.save(thread.model_copy(update={"subject": "Changed subject"}))
        elif mutation == "recipients":
            engine.store.save(
                message.model_copy(update={"recipients": ["per-reviewer"]})
            )
        else:
            engine.store.save(message.model_copy(update={"sender": "per-reviewer"}))

        _call(engine, "advance_time", {"minutes": 240})

        assert engine.store.load(Approval, approval_id).status == "expired"
        assert engine.store.load(Message, message_id).status == "draft"
    finally:
        engine.store.close()


@pytest.mark.parametrize("replacement", [[], ["doc-cedarline-equipment-quote"]])
def test_removing_or_replacing_an_approved_attachment_expires_approval(
    tmp_path: Path, replacement: list[str]
) -> None:
    """An approval covers the whole attachment list, not merely additions."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(
            engine, attachments=["doc-cedarline-materials-receipt"]
        )
        _call(
            engine,
            "update_draft",
            {"message_id": message_id, "attachments": replacement},
        )

        _call(engine, "advance_time", {"minutes": 240})

        assert engine.store.load(Approval, approval_id).status == "expired"
        assert engine.store.load(Message, message_id).status == "draft"
    finally:
        engine.store.close()


@pytest.mark.parametrize("substitution", ["cross_client", "cross_engagement"])
def test_approval_cannot_be_substituted_across_client_or_engagement(
    tmp_path: Path, substitution: str
) -> None:
    """A grant bound to one draft cannot authorise a differently scoped delivery."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(engine)
        message = engine.store.load(Message, message_id)
        thread = engine.store.load(Thread, message.thread_id)
        if substitution == "cross_client":
            engine.store.save(thread.model_copy(update={"client_id": "cli-marlowe"}))
        else:
            engine.store.save(
                thread.model_copy(update={"engagement_id": "eng-marlowe-bookkeeping"})
            )

        _call(engine, "advance_time", {"minutes": 240})

        # The event is no longer eligible to execute any message; a cross-scoped
        # substitution cannot silently turn into a granted delivery.
        assert engine.store.load(Message, message_id).status == "draft"
        assert not any(
            action.tool == "system.execute_approval" for action in engine._list(Action)
        )
        assert engine.store.load(Approval, approval_id).status == "requested"
    finally:
        engine.store.close()


def test_reverting_a_modified_draft_cannot_reuse_its_prior_approval(
    tmp_path: Path,
) -> None:
    """An approval authorises one revision and is not resurrected by a revert."""

    engine = _engine(tmp_path)
    try:
        message_id, approval_id = _approved_irq(engine)
        _call(
            engine,
            "update_draft",
            {
                "message_id": message_id,
                "body": "Please provide the fictional receipt. ",
            },
        )
        _call(
            engine,
            "update_draft",
            {"message_id": message_id, "body": "Please provide the fictional receipt."},
        )

        _call(engine, "advance_time", {"minutes": 240})

        assert engine.store.load(Approval, approval_id).status == "expired"
        assert engine.store.load(Message, message_id).status == "draft"
        assert engine.store.load(InformationRequest, "irq-r000003").status == "draft"
    finally:
        engine.store.close()
