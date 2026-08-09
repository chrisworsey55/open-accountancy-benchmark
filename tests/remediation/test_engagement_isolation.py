"""Regression coverage for same-client, cross-engagement isolation."""

from __future__ import annotations

from datetime import date

import pytest

from mirrorfirm.core.models import Engagement, QueryLogWorkpaper, Task, Workpaper
from mirrorfirm.tools import WorldToolEngine


@pytest.fixture
def second_engagement_records(engine: WorldToolEngine) -> tuple[Task, Workpaper]:
    """Create a distinct engagement for the same fictional client."""

    engagement = Engagement(
        id="eng-brightpath-secondary",
        client_id="cli-brightpath",
        scope="bookkeeping",
        period_ids=["prd-brightpath-may"],
        status="active",
    )
    task = Task(
        id="tsk-brightpath-secondary",
        engagement_id=engagement.id,
        assignee="per-agent",
        title="Fictional secondary BrightPath task",
        description="Must remain isolated from the active engagement session.",
        due=date(2026, 5, 31),
        status="open",
    )
    workpaper = Workpaper(
        id="wpp-brightpath-secondary",
        engagement_id=engagement.id,
        task_id=task.id,
        body=QueryLogWorkpaper(kind="query_log", items=[]),
        status="draft",
        created_by="per-agent",
        created_world_time=engine.now,
    )
    for record in (engagement, task, workpaper):
        engine.store.save(record)
    return task, workpaper


@pytest.mark.parametrize(
    ("tool", "payload_factory"),
    [
        ("get_task", lambda task, _: {"task_id": task.id}),
        (
            "update_task_status",
            lambda task, _: {"task_id": task.id, "status": "in_progress"},
        ),
        (
            "create_workpaper",
            lambda task, _: {
                "task_id": task.id,
                "body": {"kind": "query_log", "items": []},
            },
        ),
        (
            "update_workpaper",
            lambda _, workpaper: {
                "workpaper_id": workpaper.id,
                "body": {"kind": "query_log", "items": []},
            },
        ),
        ("finalize_workpaper", lambda _, workpaper: {"workpaper_id": workpaper.id}),
        (
            "submit_for_review",
            lambda task, workpaper: {
                "task_id": task.id,
                "workpaper_ids": [workpaper.id],
                "summary": "Fictional cross-engagement submission attempt.",
            },
        ),
        (
            "escalate",
            lambda task, _: {
                "to_role": "reviewer",
                "subject_refs": [task.id],
                "reason": "Fictional cross-engagement escalation attempt.",
            },
        ),
    ],
)
def test_same_client_second_engagement_references_are_not_accessible(
    engine: WorldToolEngine,
    second_engagement_records: tuple[Task, Workpaper],
    tool: str,
    payload_factory: object,
) -> None:
    """Every task/workpaper reference path must enforce the current engagement."""

    task, workpaper = second_engagement_records
    payload = payload_factory(task, workpaper)  # type: ignore[operator]
    result = engine.call(tool, payload)

    assert result.ok is False
    assert result.error is not None and result.error.code == "SCOPE_VIOLATION"
