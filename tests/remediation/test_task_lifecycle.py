"""Regression coverage for task lifecycle enforcement across system mutations."""

from __future__ import annotations

from datetime import timedelta

from mirrorfirm.core.models import Event, Task
from mirrorfirm.tools import WorldToolEngine


def test_reviewer_event_cannot_bypass_task_lifecycle_from_open_to_done(
    engine: WorldToolEngine,
) -> None:
    """Reviewer/system effects must use the same legal task transitions as tools."""

    task = engine.store.load(Task, "tsk-brightpath-may-close")
    engine.store.append_event(
        Event.model_validate(
            {
                "id": "evt-regression-illegal-task-done",
                "trigger": {
                    "kind": "at_time",
                    "world_time": (engine.now + timedelta(minutes=1)).isoformat(),
                },
                "payload": {
                    "kind": "reviewer_note",
                    "target_ref": task.id,
                    "note": "Fictional invalid reviewer transition.",
                    "effect": "set_task_status",
                    "effect_arg": "done",
                },
            }
        )
    )

    result = engine.call("advance_time", {"minutes": 2})

    assert result.ok is False
    assert result.error is not None and result.error.code == "REVIEW_REQUIRED"
    assert engine.store.load(Task, task.id).status == "open"
    assert engine.store.load(Event, "evt-regression-illegal-task-done").fired is False
