"""A judge sees the agent's scoped work and actual task/escalation state."""

import json
from datetime import UTC, date, datetime

from conftest import SnapshotView, base_records

from mirrorfirm.core.models import JudgeCriterion, Message, ReviewNote, Task, Thread
from mirrorfirm.episodes.manifests import load_uk_episode_manifests
from mirrorfirm.evaluation.qualitative import QualitativeJudge
from mirrorfirm.evaluation.run_eval import _grade_qualitative, _targets_for
from mirrorfirm.evaluation.state import Deliverables

NOW = datetime(2026, 5, 28, 9, tzinfo=UTC)


def test_judge_receives_workflow_state_and_only_current_engagement_outputs() -> None:
    episode = load_uk_episode_manifests()[0].model_copy(
        update={
            "engagement_id": "eng-fictional-a",
            "qualitative_criteria": [
                JudgeCriterion(
                    id=target,
                    prompt="Assess the supplied work.",
                    target=target,
                    scale="binary",
                    weight=1.0,
                )
                for target in ("final_summary", "outbound_messages", "escalations")
            ],
        }
    )
    thread = Thread(
        id="thr-a",
        client_id="cli-fictional-a",
        subject="Active",
        message_ids=[],
        engagement_id=episode.engagement_id,
    )
    foreign_thread = thread.model_copy(
        update={"id": "thr-b", "engagement_id": "eng-fictional-b"}
    )
    task = Task(
        id="tsk-a",
        engagement_id=episode.engagement_id,
        assignee=episode.agent_person_id,
        title="Reconcile",
        description="Fictional",
        due=date(2026, 5, 28),
        status="open",
    )
    old_message = Message(
        id="msg-old",
        thread_id=thread.id,
        sender="per-reviewer",
        recipients=["per-client"],
        status="sent",
        world_time=NOW,
        body="OLD_MESSAGE_DO_NOT_GRADE",
        attachments=[],
        direction="outbound",
    )
    draft = old_message.model_copy(
        update={
            "id": "msg-approved",
            "status": "draft",
            "world_time": None,
            "body": "Approved message sent during this run.",
        }
    )
    old_note = ReviewNote(
        id="rvn-old",
        engagement_id=episode.engagement_id,
        target_ref=task.id,
        author_id=episode.agent_person_id,
        body="OLD_ESCALATION_DO_NOT_GRADE",
        created_world_time=NOW,
        status="open",
    )
    records = [
        *base_records(),
        thread,
        foreign_thread,
        task,
        old_message,
        draft,
        old_note,
    ]
    initial = SnapshotView(records)
    new_note = old_note.model_copy(
        update={"id": "rvn-current", "body": "The residual requires review."}
    )
    foreign_note = new_note.model_copy(
        update={
            "id": "rvn-foreign",
            "engagement_id": "eng-fictional-b",
            "body": "FOREIGN_NOTE_DO_NOT_EXPOSE",
        }
    )
    foreign_message = old_message.model_copy(
        update={
            "id": "msg-foreign",
            "thread_id": foreign_thread.id,
            "body": "FOREIGN_MESSAGE_DO_NOT_EXPOSE",
        }
    )
    final = SnapshotView(
        [
            *[r for r in records if r not in (task, draft)],
            task.model_copy(update={"status": "ready_for_review"}),
            draft.model_copy(update={"status": "sent", "world_time": NOW}),
            new_note,
            foreign_note,
            foreign_message,
        ]
    )
    deliverables = Deliverables(
        "Ready for review; residual escalated.", (task.id, new_note.id), ("Residual",)
    )
    captured = []

    def assess(prompt):
        captured.append(prompt)
        return '{"verdict":"pass","reasoning":"Fixture transport only"}'

    _grade_qualitative(
        episode,
        initial,
        final,
        deliverables,
        QualitativeJudge({"fixture": assess}),
        ["fixture"],
    )
    assert len(captured) == 3
    summary = captured[0]
    assert '"status": "ready_for_review"' in summary
    assert '"id": "rvn-current"' in summary
    assert '"status": "sent"' in summary
    assert "FOREIGN_" not in "\n".join(captured)
    assert "OLD_MESSAGE_DO_NOT_GRADE" not in captured[1]
    assert "Approved message sent during this run." in captured[1]
    assert "OLD_ESCALATION_DO_NOT_GRADE" not in captured[2]
    assert "The residual requires review." in captured[2]
    assert (
        _targets_for("outbound_messages", initial, initial, deliverables, episode) == []
    )
    assert _targets_for("escalations", initial, initial, deliverables, episode) == []
    # Recorded state remains structured, including statuses rather than bare prose.
    outbound = _targets_for("outbound_messages", initial, final, deliverables, episode)
    assert json.loads(outbound[0])["status"] == "sent"
