"""WP-02 unit coverage for core Pydantic schema validation."""

from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Action,
    Approval,
    ApprovalActionDescriptor,
    Event,
    Journal,
    Person,
    ReviewNote,
    StateAssertion,
    Workpaper,
)


def _line(
    direction: str, amount_minor: int, currency: str = "GBP"
) -> dict[str, object]:
    return {
        "account_id": "acc-fixture-0001",
        "direction": direction,
        "amount_minor": amount_minor,
        "currency": currency,
    }


def _journal(lines: list[dict[str, object]]) -> Journal:
    return Journal(
        id="jnl-fixture-0001",
        entity_id="ent-fixture-0001",
        date=date(2026, 5, 31),
        memo="Fixture journal",
        source="proposal",
        status="proposed",
        lines=lines,
        proposed_by="per-agent",
        approval_id=None,
    )


def test_journal_requires_at_least_two_lines() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        _journal([_line("dr", 100)])


def test_journal_requires_balanced_debits_and_credits_per_currency() -> None:
    journal = _journal([_line("dr", 12000), _line("cr", 12000)])

    assert journal.lines[0].amount_minor == 12000

    with pytest.raises(ValidationError, match="GBP=1"):
        _journal([_line("dr", 12000), _line("cr", 11999)])

    with pytest.raises(ValidationError, match="GBP=12000, USD=-12000"):
        _journal([_line("dr", 12000, "GBP"), _line("cr", 12000, "USD")])


def test_role_dependent_fields_are_enforced() -> None:
    contact = Person(
        id="per-fixture-0001",
        name="Fictional Contact",
        role="client_contact",
        client_id="cli-fixture-0001",
    )

    assert contact.client_id == "cli-fixture-0001"

    with pytest.raises(ValidationError, match="client_id"):
        Person(id="per-fixture-0002", name="Invalid", role="client_contact")

    with pytest.raises(ValidationError, match="acting_as"):
        Person(id="per-fixture-0003", name="Invalid Agent", role="agent")


def test_discriminated_unions_choose_the_explicit_branch() -> None:
    event = Event.model_validate(
        {
            "id": "evt-fixture-0001",
            "trigger": {"kind": "at_time", "world_time": "2026-05-31T09:00:00Z"},
            "payload": {
                "kind": "client_reply",
                "thread_ref": "thr-fixture-0001",
                "body": "Attached is the fictional receipt.",
                "attachment_fixture_refs": ["doc-fixture-0001"],
            },
        }
    )
    descriptor = TypeAdapter(ApprovalActionDescriptor).validate_python(
        {"kind": "send_external_message", "draft_message_id": "msg-fixture-0001"}
    )
    assertion = TypeAdapter(StateAssertion).validate_python(
        {
            "kind": "txn_status",
            "btx_ids": ["btx-fixture-0001"],
            "classification_status": "proposed",
        }
    )

    assert event.trigger.kind == "at_time"
    assert event.payload.kind == "client_reply"
    assert descriptor.kind == "send_external_message"
    assert assertion.kind == "txn_status"


def test_models_round_trip_through_json() -> None:
    approval = Approval.model_validate(
        {
            "id": "apv-fixture-0001",
            "kind": "post_journal",
            "requested_by": "per-agent",
            "approver_role": "reviewer",
            "action_descriptor": {
                "kind": "post_journal",
                "journal_id": "jnl-fixture-0001",
            },
            "rationale": "Fictional fixture approval.",
            "provenance_refs": ["doc-fixture-0001"],
            "status": "requested",
        }
    )
    input_payload = {"kind": "post_journal", "journal_id": "jnl-fixture-0001"}
    output_payload = {"approval_id": "apv-fixture-0001", "status": "requested"}
    action = Action.model_validate(
        {
            "id": "act-fixture-0001",
            "step": 1,
            "actor": "per-agent",
            "tool": "request_approval",
            "input_digest": logical_state_digest(input_payload),
            "output_digest": logical_state_digest(output_payload),
            "input_payload": input_payload,
            "output_payload": output_payload,
            "world_time_before": "2026-05-31T09:00:00Z",
            "world_time_after": "2026-05-31T09:01:00Z",
            "mutations": [],
        }
    )

    assert Approval.model_validate_json(approval.model_dump_json()) == approval
    assert Action.model_validate_json(action.model_dump_json()) == action


def test_approval_requires_a_declared_lifecycle_status() -> None:
    approval = {
        "id": "apv-fixture-0002",
        "kind": "send_external_message",
        "requested_by": "per-agent",
        "approver_role": "reviewer",
        "action_descriptor": {
            "kind": "send_external_message",
            "draft_message_id": "msg-fixture-0001",
        },
        "rationale": "Fictional fixture approval.",
        "provenance_refs": ["doc-fixture-0001"],
    }

    with pytest.raises(ValidationError, match="status"):
        Approval.model_validate(approval)

    assert (
        Approval.model_validate({**approval, "status": "granted"}).status == "granted"
    )


def test_workpaper_finalisation_fields_follow_status() -> None:
    draft = Workpaper.model_validate(
        {
            "id": "wpp-fixture-0001",
            "engagement_id": "eng-fixture-0001",
            "task_id": "tsk-fixture-0001",
            "body": {"kind": "query_log", "items": []},
            "status": "draft",
            "created_by": "per-agent",
            "created_world_time": "2026-05-31T09:00:00Z",
        }
    )

    assert draft.body.kind == "query_log"
    assert draft.finalized_world_time is None

    with pytest.raises(ValidationError, match="finalized_world_time"):
        Workpaper.model_validate(
            {
                **draft.model_dump(mode="json"),
                "status": "final",
            }
        )

    with pytest.raises(ValidationError, match="finalized_world_time"):
        Workpaper.model_validate(
            {
                **draft.model_dump(mode="json"),
                "finalized_world_time": "2026-05-31T10:00:00Z",
            }
        )


def test_review_note_addressing_fields_follow_status() -> None:
    note = ReviewNote.model_validate(
        {
            "id": "rvn-fixture-0001",
            "engagement_id": "eng-fixture-0001",
            "target_ref": "wpp-fixture-0001",
            "author_id": "per-reviewer",
            "body": "Please resolve the fictional timing difference.",
            "created_world_time": "2026-05-31T09:00:00Z",
            "status": "open",
        }
    )

    assert note.addressed_by is None

    with pytest.raises(ValidationError, match="addressed_by"):
        ReviewNote.model_validate(
            {
                **note.model_dump(mode="json"),
                "status": "addressed",
            }
        )

    with pytest.raises(ValidationError, match="addressed_by"):
        ReviewNote.model_validate(
            {
                **note.model_dump(mode="json"),
                "addressed_by": "per-agent",
                "addressed_world_time": "2026-05-31T10:00:00Z",
            }
        )


def test_action_payloads_must_match_their_digests() -> None:
    input_payload = {"b": 2, "a": 1}
    output_payload = {"status": "ok"}
    action_data = {
        "id": "act-fixture-0002",
        "step": 2,
        "actor": "per-agent",
        "tool": "fixture_tool",
        "input_digest": logical_state_digest(input_payload),
        "output_digest": logical_state_digest(output_payload),
        "input_payload": input_payload,
        "output_payload": output_payload,
        "world_time_before": "2026-05-31T09:00:00Z",
        "world_time_after": "2026-05-31T09:01:00Z",
        "mutations": [],
    }

    assert Action.model_validate(action_data).input_payload == {"a": 1, "b": 2}

    with pytest.raises(ValidationError, match="input_digest"):
        Action.model_validate({**action_data, "input_payload": {"a": 2, "b": 2}})
