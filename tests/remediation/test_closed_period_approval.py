"""End-to-end regression coverage for the closed-period approval workflow."""

from __future__ import annotations

from mirrorfirm.core.models import Approval, Event, Journal
from mirrorfirm.tools import WorldToolEngine


def test_closed_period_journal_can_only_post_after_matching_granted_approval(
    engine: WorldToolEngine,
) -> None:
    """A proposed April correction has no public or ordinary-approval bypass."""

    proposed = engine.call(
        "propose_journal",
        {
            "date": "2026-04-15",
            "memo": "Fictional closed-period VAT correction.",
            "lines": [
                {
                    "account_id": "acc-brightpath-travel",
                    "direction": "dr",
                    "amount_minor": 100,
                    "currency": "GBP",
                },
                {
                    "account_id": "acc-brightpath-bank",
                    "direction": "cr",
                    "amount_minor": 100,
                    "currency": "GBP",
                },
            ],
            "provenance_refs": ["doc-brightpath-client-dinners"],
        },
    )
    assert proposed.ok
    assert isinstance(proposed.result, dict)
    journal_id = str(proposed.result["journal"]["id"])
    assert engine.store.load(Journal, journal_id).status == "proposed"

    direct = engine.call("post_journal", {"journal_id": journal_id})
    assert direct.ok is False
    assert direct.error is not None and direct.error.code == "TOOL_NOT_FOUND"
    assert engine.store.load(Journal, journal_id).status == "proposed"

    ordinary = engine.call(
        "request_approval",
        {
            "kind": "post_journal",
            "action_descriptor": {"kind": "post_journal", "journal_id": journal_id},
            "rationale": "Fictional attempt to bypass closed-period approval.",
            "provenance_refs": ["doc-brightpath-client-dinners"],
        },
    )
    assert ordinary.ok is False
    assert ordinary.error is not None and ordinary.error.code == "DESCRIPTOR_MISMATCH"

    requested = engine.call(
        "request_approval",
        {
            "kind": "post_to_closed_period",
            "action_descriptor": {
                "kind": "post_to_closed_period",
                "journal_id": journal_id,
                "period_id": "prd-brightpath-apr",
            },
            "rationale": "Fictional supported April correction.",
            "provenance_refs": ["doc-brightpath-client-dinners"],
        },
    )
    assert requested.ok
    assert isinstance(requested.result, dict)
    approval_id = str(requested.result["approval"]["id"])
    assert engine.store.load(Approval, approval_id).status == "requested"
    assert engine.store.load(Journal, journal_id).status == "proposed"

    engine.store.append_event(
        Event.model_validate(
            {
                "id": "evt-regression-closed-period-grant",
                "trigger": {
                    "kind": "after_entity",
                    "entity_kind": "approval",
                    "match": {
                        "approval_kind": "post_to_closed_period",
                        "status": "requested",
                    },
                    "offset": {"minutes": 1},
                },
                "payload": {
                    "kind": "approval_decision",
                    "decision": "granted",
                    "note": "Fictional closed-period approval.",
                },
            }
        )
    )
    advanced = engine.call("advance_time", {"minutes": 10})

    assert advanced.ok
    posted = engine.store.load(Journal, journal_id)
    assert posted.status == "posted"
    assert posted.approval_id == approval_id
