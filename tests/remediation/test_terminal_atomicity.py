"""Regression coverage for atomic, fail-safe terminal completion."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.models import Action
from mirrorfirm.tools import WorldToolEngine

_FINISH_PAYLOAD = {
    "summary": "The fictional close is ready for review.",
    "deliverable_refs": [],
    "unresolved_items": [],
}


def test_successful_finish_is_terminal_and_snapshot_contains_terminal_action(
    engine: WorldToolEngine,
) -> None:
    """Normal terminal completion is explicit, durable, and rejects later calls."""

    finished = engine.call("finish_episode", _FINISH_PAYLOAD)

    assert finished.ok
    assert isinstance(finished.result, dict)
    snapshot = finished.result["final_snapshot"]
    assert Path(snapshot["db_path"]).is_file()
    assert engine._list(Action)[-1].tool == "finish_episode"  # noqa: SLF001
    later = engine.call("get_current_time", {})
    assert later.ok is False
    assert later.error is not None and later.error.code == "VALIDATION_ERROR"


def test_snapshot_collision_rolls_back_terminal_action_and_keeps_engine_open(
    engine: WorldToolEngine,
) -> None:
    """A pre-existing final snapshot path cannot commit a half-finished episode."""

    collision = engine.snapshot_dir / "snp-r000001.db"
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_text("existing fictional snapshot", encoding="utf-8")

    result = engine.call("finish_episode", _FINISH_PAYLOAD)

    assert result.ok is False
    assert not any(
        action.tool == "finish_episode" and "final_snapshot_id" in action.output_payload
        for action in engine._list(Action)  # noqa: SLF001
    )
    assert engine.call("get_current_time", {}).ok


def test_snapshot_failure_rolls_back_terminal_action_and_keeps_engine_open(
    engine: WorldToolEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I/O failure during snapshot creation cannot leave a terminal residue."""

    def fail_snapshot(*_: object, **__: object) -> object:
        raise OSError("fictional snapshot failure")

    monkeypatch.setattr(engine.store, "snapshot", fail_snapshot)
    result = engine.call("finish_episode", _FINISH_PAYLOAD)

    assert result.ok is False
    assert not any(
        action.tool == "finish_episode" and "final_snapshot_id" in action.output_payload
        for action in engine._list(Action)  # noqa: SLF001
    )
    assert engine.call("get_current_time", {}).ok
