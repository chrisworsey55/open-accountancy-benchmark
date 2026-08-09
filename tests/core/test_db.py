"""WP-03 persistence, append-only log, snapshot, and read-only view coverage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mirrorfirm.core.db import SQLiteWorldView, WorldStore
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Action,
    Document,
    Event,
    Journal,
    Message,
    Person,
    Workpaper,
    WorldManifest,
)


def _person(person_id: str, name: str) -> Person:
    return Person(id=person_id, name=name, role="bookkeeper")


def _action() -> Action:
    input_payload = {"query": "fictional receipt"}
    output_payload = {"document_ids": ["doc-fixture-0001"]}
    return Action(
        id="act-fixture-0001",
        step=1,
        actor="per-agent",
        tool="search_documents",
        input_digest=logical_state_digest(input_payload),
        output_digest=logical_state_digest(output_payload),
        input_payload=input_payload,
        output_payload=output_payload,
        world_time_before=datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc),
        world_time_after=datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc),
        mutations=[],
    )


def _event() -> Event:
    return Event.model_validate(
        {
            "id": "evt-fixture-0001",
            "trigger": {"kind": "at_time", "world_time": "2026-05-31T10:00:00Z"},
            "payload": {
                "kind": "deadline",
                "description": "Fictional month-end deadline.",
            },
        }
    )


def test_persistence_round_trip_and_digest_are_stable(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"

    with WorldStore.create(database_path) as store:
        store.save(_person("per-fixture-0002", "Fictional Two"))
        store.save(_person("per-fixture-0001", "Fictional One"))
        store.append_action(_action())
        store.append_event(_event())

        initial_digest = store.state_digest()
        snapshot = store.snapshot(
            tmp_path / "initial.db",
            snapshot_id="snp-fixture-0001",
            episode_run_id="run-fixture-0001",
            phase="initial",
        )

        assert store.load(Person, "per-fixture-0001") == _person(
            "per-fixture-0001", "Fictional One"
        )
        with store.view() as view:
            assert [person.id for person in view.list(Person)] == [
                "per-fixture-0001",
                "per-fixture-0002",
            ]
            assert view.actions() == (_action(),)
            assert view.events() == (_event(),)
            assert view.state_digest() == initial_digest

    with WorldStore.open(database_path) as reopened:
        assert reopened.state_digest() == initial_digest
        assert reopened.load(Action, "act-fixture-0001") == _action()
        assert reopened.load(Event, "evt-fixture-0001") == _event()

    with SQLiteWorldView.open(snapshot.db_path) as snapshot_view:
        assert snapshot.state_digest == initial_digest
        assert snapshot_view.state_digest() == initial_digest


def test_action_and_event_logs_are_append_only(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"
    with WorldStore.create(database_path) as store:
        store.append_action(_action())
        event = _event()
        store.append_event(event)
        digest_before_firing = store.state_digest()
        store.save(event.model_copy(update={"fired": True}))

        assert store.load(Event, "evt-fixture-0001").fired is True
        assert store.state_digest() != digest_before_firing
        with store.view() as view:
            assert view.events()[0].fired is True

        with pytest.raises(sqlite3.IntegrityError):
            store.append_action(_action())
        with pytest.raises(sqlite3.IntegrityError):
            store.append_event(_event())
        with pytest.raises(ValueError, match="already fired"):
            store.mark_event_fired("evt-fixture-0001")

    connection = sqlite3.connect(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE actions SET step = 2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM event_firings")
    finally:
        connection.close()


def test_snapshots_are_independent_and_never_overwritten(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"
    snapshot_path = tmp_path / "initial.db"

    with WorldStore.create(database_path) as store:
        store.save(_person("per-fixture-0001", "Fictional One"))
        snapshot = store.snapshot(
            snapshot_path,
            snapshot_id="snp-fixture-0001",
            episode_run_id="run-fixture-0001",
            phase="initial",
        )
        store.save(_person("per-fixture-0001", "Fictional Updated Name"))

        assert store.state_digest() != snapshot.state_digest
        with pytest.raises(FileExistsError):
            store.snapshot(
                snapshot_path,
                snapshot_id="snp-fixture-0002",
                episode_run_id="run-fixture-0001",
                phase="final",
            )

    with SQLiteWorldView.open(snapshot_path) as snapshot_view:
        assert snapshot_view.get(Person, "per-fixture-0001") == _person(
            "per-fixture-0001", "Fictional One"
        )


def test_views_are_opened_read_only(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"
    with WorldStore.create(database_path) as store:
        store.save(_person("per-fixture-0001", "Fictional One"))

    with SQLiteWorldView.open(database_path) as view:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            view._connection.execute("DELETE FROM records")  # noqa: SLF001


def test_immutable_records_cannot_be_overwritten_or_deleted(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"
    created = datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc)
    world = WorldManifest(
        world_id="wld-fixture-0001",
        world_version="0.1.0",
        jurisdiction="uk",
        timezone="Europe/London",
        title="Fictional world",
        description="Fictional persistence fixture.",
        seed=1,
        start_world_time=created,
        practice_file="practice.yaml",
        client_files=["client.yaml"],
        events_file="events.yaml",
        trap_register_file="traps.yaml",
        license="CC-BY-4.0",
    )
    document = Document(
        id="doc-fixture-0001",
        sha256="a" * 64,
        filename="original.txt",
        mime="text/plain",
        kind="memo",
        client_id="cli-fixture-0001",
        source="fixture",
        received_world_time=created,
    )
    proposed_journal = Journal.model_validate(
        {
            "id": "jnl-fixture-0001",
            "entity_id": "ent-fixture-0001",
            "date": "2026-05-31",
            "memo": "Posted fictional journal",
            "source": "proposal",
            "status": "proposed",
            "lines": [
                {
                    "account_id": "acc-fixture-0001",
                    "direction": "dr",
                    "amount_minor": 100,
                    "currency": "GBP",
                },
                {
                    "account_id": "acc-fixture-0002",
                    "direction": "cr",
                    "amount_minor": 100,
                    "currency": "GBP",
                },
            ],
            "proposed_by": "per-agent",
            "approval_id": None,
        }
    )
    posted_journal = proposed_journal.model_copy(
        update={"status": "posted", "approval_id": "apv-fixture-0001"}
    )
    draft_message = Message(
        id="msg-fixture-0001",
        thread_id="thr-fixture-0001",
        sender="per-agent",
        recipients=["per-contact-0001"],
        status="draft",
        world_time=None,
        body="Fictional sent message.",
        attachments=[],
        direction="outbound",
    )
    sent_message = draft_message.model_copy(
        update={"status": "sent", "world_time": created}
    )
    draft_workpaper = Workpaper.model_validate(
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
    final_workpaper = draft_workpaper.model_copy(
        update={
            "status": "final",
            "finalized_world_time": datetime(2026, 5, 31, 9, 1, tzinfo=timezone.utc),
        }
    )
    changed_records = (
        world.model_copy(update={"title": "Replacement world"}),
        document.model_copy(update={"filename": "replacement.txt"}),
        posted_journal.model_copy(update={"memo": "Replacement memo"}),
        sent_message.model_copy(update={"body": "Replacement message."}),
        final_workpaper.model_copy(update={"created_by": "per-other"}),
    )

    with WorldStore.create(database_path) as store:
        for record in (
            world,
            document,
            proposed_journal,
            draft_message,
            draft_workpaper,
        ):
            store.save(record)
        for record in (posted_journal, sent_message, final_workpaper):
            store.save(record)
        for record in changed_records:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store.save(record)

    connection = sqlite3.connect(database_path)
    try:
        for model_type, entity_id in (
            ("WorldManifest", world.world_id),
            ("Document", document.id),
            ("Journal", posted_journal.id),
            ("Message", sent_message.id),
            ("Workpaper", final_workpaper.id),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "DELETE FROM records WHERE model_type = ? AND id = ?",
                    (model_type, entity_id),
                )
    finally:
        connection.close()


def test_final_workpaper_may_only_reopen_to_the_same_draft(tmp_path: Path) -> None:
    database_path = tmp_path / "world.db"
    final = Workpaper.model_validate(
        {
            "id": "wpp-fixture-0001",
            "engagement_id": "eng-fixture-0001",
            "task_id": "tsk-fixture-0001",
            "body": {"kind": "query_log", "items": []},
            "status": "final",
            "created_by": "per-agent",
            "created_world_time": "2026-05-31T09:00:00Z",
            "finalized_world_time": "2026-05-31T09:01:00Z",
        }
    )
    reopened = final.model_copy(
        update={"status": "draft", "finalized_world_time": None}
    )

    with WorldStore.create(database_path) as store:
        store.save(final)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.save(reopened.model_copy(update={"created_by": "per-other"}))
        store.save(reopened)

        assert store.load(Workpaper, reopened.id) == reopened
