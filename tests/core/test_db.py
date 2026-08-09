"""WP-03 persistence, append-only log, snapshot, and read-only view coverage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mirrorfirm.core.db import SQLiteWorldView, WorldStore
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import Action, Event, Person


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
        store.append_event(_event())

        with pytest.raises(sqlite3.IntegrityError):
            store.append_action(_action())
        with pytest.raises(sqlite3.IntegrityError):
            store.append_event(_event())

    connection = sqlite3.connect(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE actions SET step = 2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")
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
