"""Regression coverage for atomic, fail-safe terminal completion."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.db import SQLiteWorldView, WorldStore
from mirrorfirm.core.models import Action
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

_FINISH_PAYLOAD = {
    "summary": "The fictional close is ready for review.",
    "deliverable_refs": [],
    "unresolved_items": [],
}
_ROOT = Path(__file__).resolve().parents[2]
_WORLD = _ROOT / "worlds" / "uk-wyrley-brook"


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


@pytest.mark.parametrize("failure_mode", ["collision", "write_failure"])
def test_failed_finish_remains_resumable_after_reopen_then_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """Only a snapshot-backed terminal Action may make a reopened engine terminal."""

    database_path = compile_world(_WORLD, tmp_path / "world.db").database_path
    snapshot_dir = tmp_path / "snapshots"
    collision = snapshot_dir / "snp-r000001.db"
    if failure_mode == "collision":
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_text("existing fictional snapshot", encoding="utf-8")

    with WorldStore.open(database_path) as first_store:
        first = WorldToolEngine(
            first_store,
            world_root=_WORLD,
            actor_id="per-agent",
            engagement_id="eng-brightpath-bookkeeping",
            snapshot_dir=snapshot_dir,
        )
        if failure_mode == "write_failure":

            def fail_snapshot(*_: object, **__: object) -> object:
                raise OSError("fictional snapshot write failure")

            monkeypatch.setattr(first.store, "snapshot", fail_snapshot)

        failed = first.call("finish_episode", _FINISH_PAYLOAD)

        assert failed.ok is False
        assert failed.error is not None and failed.error.code == "VALIDATION_ERROR"
        failed_actions = [
            action
            for action in first._list(Action)
            if action.tool == "finish_episode"  # noqa: SLF001
        ]
        assert len(failed_actions) == 1
        assert "final_snapshot_id" not in failed_actions[0].output_payload
        assert first.call("get_current_time", {}).ok
        if failure_mode == "collision":
            assert (
                collision.read_text(encoding="utf-8") == "existing fictional snapshot"
            )
        else:
            assert not snapshot_dir.exists()

    with WorldStore.open(database_path) as resumed_store:
        resumed = WorldToolEngine(
            resumed_store,
            world_root=_WORLD,
            actor_id="per-agent",
            engagement_id="eng-brightpath-bookkeeping",
            snapshot_dir=snapshot_dir,
        )
        assert resumed.call("get_current_time", {}).ok
        finished = resumed.call("finish_episode", _FINISH_PAYLOAD)

        assert finished.ok
        assert isinstance(finished.result, dict)
        snapshot = finished.result["final_snapshot"]
        snapshot_path = Path(str(snapshot["db_path"]))
        successful_actions = [
            action
            for action in resumed._list(Action)  # noqa: SLF001
            if action.tool == "finish_episode"
            and "final_snapshot_id" in action.output_payload
        ]
        assert len(successful_actions) == 1
        with SQLiteWorldView.open(snapshot_path) as view:
            assert view.actions()[-1] == successful_actions[0]
            assert view.state_digest() == finished.result["state_digest"]

    with WorldStore.open(database_path) as terminal_store:
        terminal = WorldToolEngine(
            terminal_store,
            world_root=_WORLD,
            actor_id="per-agent",
            engagement_id="eng-brightpath-bookkeeping",
            snapshot_dir=snapshot_dir,
        )
        rejected = terminal.call("get_current_time", {})
        assert rejected.ok is False
        assert rejected.error is not None and rejected.error.code == "VALIDATION_ERROR"
