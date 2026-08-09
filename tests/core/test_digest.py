"""Golden vectors for §D.4 canonical logical-state digests."""

from datetime import date, datetime, timezone

import pytest

from mirrorfirm.core.digest import canonical_json, logical_state_digest
from mirrorfirm.core.models import Journal

GOLDEN_TABLE_STATE = {
    "journals": [
        {"id": "jnl-fixture-0002", "amount_minor": 1200, "currency": "GBP"},
        {"id": "jnl-fixture-0001", "amount_minor": -1200, "currency": "GBP"},
    ],
    "world_time": datetime(2026, 5, 31, 9, 30, tzinfo=timezone.utc),
    "rowid": 99,
    "updated_at": "not-logical-state",
}

GOLDEN_TABLE_JSON = (
    '{"journals":[{"amount_minor":-1200,"currency":"GBP","id":"jnl-fixture-0001"},'
    '{"amount_minor":1200,"currency":"GBP","id":"jnl-fixture-0002"}],'
    '"world_time":"2026-05-31T09:30:00.000000Z"}'
)
GOLDEN_TABLE_DIGEST = "74bc84e68522aff3092237cbd062fe9999dd4bd72032b12b303bbb5f3a2be8a8"
GOLDEN_JOURNAL_DIGEST = (
    "e6a521d3fcaaf0b08841227770bfa7f7bea25bf87cc7676d10317f9c8f19f9ff"
)


def _golden_journal() -> Journal:
    return Journal(
        id="jnl-fixture-0001",
        entity_id="ent-fixture-0001",
        date=date(2026, 5, 31),
        memo="Canonical fixture journal",
        source="proposal",
        status="proposed",
        lines=[
            {
                "account_id": "acc-fixture-0002",
                "direction": "cr",
                "amount_minor": 1200,
                "currency": "GBP",
            },
            {
                "account_id": "acc-fixture-0001",
                "direction": "dr",
                "amount_minor": 1200,
                "currency": "GBP",
            },
        ],
        proposed_by="per-agent",
        approval_id=None,
    )


def test_table_state_matches_fixed_canonical_json_and_digest_vector() -> None:
    reordered = {
        "updated_at": "not-logical-state",
        "world_time": datetime(2026, 5, 31, 9, 30, tzinfo=timezone.utc),
        "journals": list(reversed(GOLDEN_TABLE_STATE["journals"])),
        "rowid": 1,
    }

    assert canonical_json(GOLDEN_TABLE_STATE) == GOLDEN_TABLE_JSON
    assert canonical_json(reordered) == GOLDEN_TABLE_JSON
    assert logical_state_digest(GOLDEN_TABLE_STATE) == GOLDEN_TABLE_DIGEST
    assert logical_state_digest(reordered) == GOLDEN_TABLE_DIGEST


def test_pydantic_model_matches_fixed_digest_vector() -> None:
    assert logical_state_digest(_golden_journal()) == GOLDEN_JOURNAL_DIGEST


def test_canonical_payload_digest_ignores_key_order_and_detects_mutation() -> None:
    payload = {
        "tool": "request_approval",
        "descriptor": {"kind": "post_journal", "journal_id": "jnl-fixture-0001"},
    }
    reordered_payload = {
        "descriptor": {"journal_id": "jnl-fixture-0001", "kind": "post_journal"},
        "tool": "request_approval",
    }
    mutated_payload = {
        "tool": "request_approval",
        "descriptor": {"kind": "post_journal", "journal_id": "jnl-fixture-0002"},
    }

    assert logical_state_digest(payload) == logical_state_digest(reordered_payload)
    assert logical_state_digest(payload) != logical_state_digest(mutated_payload)


def test_digest_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        logical_state_digest({"world_time": datetime(2026, 5, 31, 9, 30)})
