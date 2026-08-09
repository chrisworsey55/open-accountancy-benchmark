"""Regression coverage for failed duration-bearing tool action timing."""

from __future__ import annotations

from datetime import date, timedelta

from mirrorfirm.core.models import Action
from mirrorfirm.tools import WorldToolEngine


def test_rejected_duration_bearing_tool_records_real_start_and_end_times(
    engine: WorldToolEngine,
) -> None:
    """A failed closed-period proposal still consumes its declared five minutes."""

    started = engine.now
    result = engine.call(
        "propose_journal",
        {
            "date": date(2026, 3, 15).isoformat(),
            "memo": "Fictional locked-period proposal.",
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
            "provenance_refs": ["doc-brightpath-may-001"],
        },
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "PERIOD_LOCKED"
    action = engine._list(Action)[-1]  # noqa: SLF001
    assert action.world_time_before == started
    assert action.world_time_after == started + timedelta(minutes=5)
