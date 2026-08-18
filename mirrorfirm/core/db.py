"""SQLite persistence, snapshots, and read-only world views for WP-03.

The database deliberately stores validated Pydantic records as canonical JSON.  This
keeps the public models as the single schema authority while retaining SQLite's
durability and constraints for a compiled world.  ``actions`` and ``events`` are
separate append-only logs; every other persisted root record lives in ``records``.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Final, Iterator, Literal, Protocol, Self, TypeVar, cast

from .digest import logical_state_digest
from .models import (
    MODEL_TYPES,
    Action,
    Event,
    StateSnapshot,
    VersionedModel,
    WorldManifest,
)

ModelT = TypeVar("ModelT", bound=VersionedModel)

_SCHEMA_VERSION: Final = "0.1"
_PERSISTED_MODEL_TYPES: Final[tuple[type[VersionedModel], ...]] = tuple(
    model_type
    for model_type in MODEL_TYPES
    if model_type is WorldManifest or "id" in model_type.model_fields
)
_REQUIRED_TABLES: Final = frozenset(
    {"metadata", "records", "actions", "events", "event_firings"}
)
_IMMUTABILITY_TRIGGERS: Final = frozenset(
    {
        "actions_no_update",
        "actions_no_delete",
        "events_no_update",
        "events_no_delete",
        "event_firings_no_update",
        "event_firings_no_delete",
        "records_no_update_when_immutable",
        "records_no_delete_when_immutable",
    }
)


class WorldView(Protocol):
    """Stable, read-only query interface over one world-state database."""

    def get(self, model_type: type[ModelT], entity_id: str) -> ModelT | None:
        """Return one record, or ``None`` when that identifier is absent."""

    def list(self, model_type: type[ModelT]) -> tuple[ModelT, ...]:
        """Return records of one model type in deterministic identifier order."""

    def actions(self) -> tuple[Action, ...]:
        """Return the append-only audit log ordered by step then identifier."""

    def events(self) -> tuple[Event, ...]:
        """Return scheduled events, with firing state projected from its audit log."""

    def logical_state(self) -> dict[str, builtins.list[object]]:
        """Return the complete, JSON-compatible state used for digesting."""

    def state_digest(self) -> str:
        """Return the §D.4 canonical digest of ``logical_state()``."""


class SQLiteWorldView:
    """A read-only ``WorldView`` backed by a SQLite snapshot database."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open an existing database in SQLite read-only mode."""

        database_path = Path(path).resolve()
        if not database_path.is_file():
            raise FileNotFoundError(database_path)

        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            _verify_schema(connection)
            connection.execute("PRAGMA query_only = ON")
        except BaseException:
            connection.close()
            raise
        return cls(database_path, connection)

    @classmethod
    def from_serialized(cls, data: bytes) -> Self:
        """Open a read-only view from already-retained SQLite bytes.

        ``WorldStore`` uses this instead of reopening its mutable database pathname
        while a secured episode run is live.  The method deliberately has no file
        system dependency.
        """

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.deserialize(data)
            _verify_schema(connection)
            connection.execute("PRAGMA query_only = ON")
        except BaseException:
            connection.close()
            raise
        return cls(Path(":memory:"), connection)

    def close(self) -> None:
        """Close this read-only database handle."""

        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, model_type: type[ModelT], entity_id: str) -> ModelT | None:
        """Return one typed record without exposing a writable connection."""

        return _get_record(self._connection, model_type, entity_id)

    def list(self, model_type: type[ModelT]) -> tuple[ModelT, ...]:
        """List typed records in the deterministic ordering used by digests."""

        return _list_records(self._connection, model_type)

    def actions(self) -> tuple[Action, ...]:
        """Read the append-only action log in execution order."""

        return _list_records(self._connection, Action)

    def events(self) -> tuple[Event, ...]:
        """Read the append-only event log in deterministic identifier order."""

        return _list_records(self._connection, Event)

    def logical_state(self) -> dict[str, builtins.list[object]]:
        """Materialise only logical domain records, never SQLite metadata."""

        return _logical_state(self._connection)

    def state_digest(self) -> str:
        """Compute a §D.4 digest over this view's logical domain state."""

        return logical_state_digest(self.logical_state())


class WorldStore:
    """Writable SQLite world store; expose queries through ``SQLiteWorldView``."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection
        self._transaction_depth = 0

    @classmethod
    def create(cls, path: str | Path) -> Self:
        """Create a new, empty world database without overwriting an existing file."""

        database_path = Path(path).resolve()
        if database_path.exists():
            raise FileExistsError(database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)

        connection = _connect_writable(database_path)
        try:
            _initialise_schema(connection)
        except BaseException:
            connection.close()
            raise
        return cls(database_path, connection)

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open an existing WP-03 world database for persistence operations."""

        database_path = Path(path).resolve()
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        connection = _connect_writable(database_path)
        try:
            _verify_schema(connection)
        except BaseException:
            connection.close()
            raise
        return cls(database_path, connection)

    @property
    def path(self) -> Path:
        """The physical SQLite database path for this world."""

        return self._path

    def close(self) -> None:
        """Commit completed work and close the writable database handle."""

        if self._transaction_depth:
            self._connection.rollback()
            self._transaction_depth = 0
        self._connection.commit()
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save(self, record: VersionedModel) -> None:
        """Persist a validated root record.

        Actions are append-only. Events are initially appended as immutable schedules;
        their one allowed lifecycle transition appends an event-firing record rather
        than replacing the schedule.
        """

        if isinstance(record, Action):
            self.append_action(record)
            return
        if isinstance(record, Event):
            _save_event(
                self._connection,
                record,
                commit=self._transaction_depth == 0,
            )
            return

        model_type = type(record)
        _require_record_model(model_type)
        entity_id = _record_identifier(record)
        payload = _serialise_model(record)
        self._connection.execute(
            """
            INSERT INTO records (model_type, id, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(model_type, id) DO UPDATE SET payload = excluded.payload
            """,
            (model_type.__name__, entity_id, payload),
        )
        self._commit_if_autocommit()

    def load(self, model_type: type[ModelT], entity_id: str) -> ModelT:
        """Load one record or raise ``KeyError`` when it does not exist."""

        record = _get_record(self._connection, model_type, entity_id)
        if record is None:
            raise KeyError(f"{model_type.__name__} {entity_id!r} does not exist")
        return record

    def append_action(self, action: Action) -> None:
        """Append one audit action; update and deletion are schema-prohibited."""

        _append_log_record(self._connection, "actions", action)
        self._commit_if_autocommit()

    def append_event(self, event: Event) -> None:
        """Append one unfired scheduled event; its schedule is immutable thereafter."""

        _append_log_record(self._connection, "events", event)
        self._commit_if_autocommit()

    def mark_event_fired(self, event_id: str) -> None:
        """Append an event firing without mutating the immutable event schedule."""

        event = _get_record(self._connection, Event, event_id)
        if event is None:
            raise KeyError(f"Event {event_id!r} does not exist")
        if event.fired:
            raise ValueError(f"Event {event_id!r} is already fired")
        self._connection.execute(
            "INSERT INTO event_firings (event_id) VALUES (?)", (event_id,)
        )
        self._commit_if_autocommit()

    def view(self) -> SQLiteWorldView:
        """Open a separate read-only query view over the committed database state."""

        self._commit_if_autocommit()
        return SQLiteWorldView.from_serialized(self._connection.serialize())

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomically persist a tool's domain mutations and its audit Action.

        Nested callers share one SQLite transaction.  The store deliberately keeps
        read views separate and read-only; code which needs to observe its own
        uncommitted writes must retain the typed value it has just constructed.
        """

        outermost = self._transaction_depth == 0
        if outermost:
            self._connection.execute("BEGIN")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                self._connection.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self._connection.commit()

    def _commit_if_autocommit(self) -> None:
        if self._transaction_depth == 0:
            self._connection.commit()

    def logical_state(self) -> dict[str, list[object]]:
        """Return the current logical state without SQLite implementation details."""

        return _logical_state(self._connection)

    def state_digest(self) -> str:
        """Return the canonical logical-state digest of the writable world."""

        return logical_state_digest(self.logical_state())

    def snapshot(
        self,
        destination: str | Path,
        *,
        snapshot_id: str,
        episode_run_id: str,
        phase: Literal["initial", "final"],
        transactional: bool = False,
    ) -> StateSnapshot:
        """Copy the database and return its digest-bearing snapshot metadata.

        Snapshot paths must be new.  This protects a previously recorded initial or
        final state from accidental replacement and makes reset a simple recopy.
        """

        destination_path = Path(destination).resolve()
        if destination_path == self._path:
            raise ValueError(
                "snapshot destination must differ from the source database"
            )
        if destination_path.exists():
            raise FileExistsError(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        if transactional and self._transaction_depth == 0:
            raise ValueError("transactional snapshots require an active transaction")

        source_digest = self.state_digest()
        try:
            if transactional:
                destination_path.write_bytes(self._connection.serialize())
            else:
                self._connection.commit()
                shutil.copy2(self._path, destination_path)

            with SQLiteWorldView.open(destination_path) as snapshot_view:
                snapshot_digest = snapshot_view.state_digest()
            if snapshot_digest != source_digest:
                raise RuntimeError("snapshot digest differs from its source database")
        except BaseException:
            if destination_path.exists():
                destination_path.unlink()
            raise

        return StateSnapshot(
            id=snapshot_id,
            episode_run_id=episode_run_id,
            phase=phase,
            db_path=str(destination_path),
            state_digest=snapshot_digest,
        )

    def snapshot_to_directory_fd(
        self,
        destination_directory_fd: int,
        destination_name: str,
        *,
        displayed_path: str | Path,
        snapshot_id: str,
        episode_run_id: str,
        phase: Literal["initial", "final"],
        transactional: bool = False,
        verify_destination: Callable[[], None] | None = None,
    ) -> StateSnapshot:
        """Materialise a snapshot through a retained directory descriptor only.

        The database bytes are serialized from the active connection and written via
        ``openat`` semantics.  No mutable destination pathname is opened or followed.
        ``displayed_path`` is metadata for later artifact loading, not a write target.
        """

        if not destination_name or Path(destination_name).name != destination_name:
            raise ValueError("snapshot destination name is unsafe")
        if transactional and self._transaction_depth == 0:
            raise ValueError("transactional snapshots require an active transaction")
        try:
            metadata = os.fstat(destination_directory_fd)
        except OSError as error:
            raise ValueError("snapshot destination is unsafe") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("snapshot destination is unsafe")
        if verify_destination is not None:
            verify_destination()
        source_digest = self.state_digest()
        if not transactional:
            self._connection.commit()
        data = self._connection.serialize()
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=destination_directory_fd,
            )
            created = True
            destination_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(destination_metadata.st_mode):
                raise OSError("snapshot destination is not regular")
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fsync(descriptor)
        except OSError as error:
            raise RuntimeError("snapshot destination could not be created") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            with SQLiteWorldView.from_serialized(data) as snapshot_view:
                snapshot_digest = snapshot_view.state_digest()
            if snapshot_digest != source_digest:
                raise RuntimeError("snapshot digest differs from its source database")
            if verify_destination is not None:
                verify_destination()
        except BaseException:
            if created:
                try:
                    os.unlink(destination_name, dir_fd=destination_directory_fd)
                except FileNotFoundError:
                    pass
            raise
        return StateSnapshot(
            id=snapshot_id,
            episode_run_id=episode_run_id,
            phase=phase,
            db_path=str(displayed_path),
            state_digest=snapshot_digest,
        )


def _connect_writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    return connection


def _initialise_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        ) STRICT;

        CREATE TABLE records (
            model_type TEXT NOT NULL CHECK (length(model_type) > 0),
            id TEXT NOT NULL CHECK (length(id) > 0),
            payload TEXT NOT NULL CHECK (json_valid(payload)),
            PRIMARY KEY (model_type, id)
        ) STRICT;

        CREATE TABLE actions (
            id TEXT PRIMARY KEY NOT NULL CHECK (length(id) > 0),
            step INTEGER NOT NULL UNIQUE,
            payload TEXT NOT NULL CHECK (json_valid(payload))
        ) STRICT;

        CREATE TABLE events (
            id TEXT PRIMARY KEY NOT NULL CHECK (length(id) > 0),
            payload TEXT NOT NULL CHECK (json_valid(payload))
        ) STRICT;

        CREATE TABLE event_firings (
            event_id TEXT PRIMARY KEY NOT NULL REFERENCES events(id)
        ) STRICT;

        CREATE TRIGGER actions_no_update
        BEFORE UPDATE ON actions
        BEGIN
            SELECT RAISE(ABORT, 'actions are append-only');
        END;

        CREATE TRIGGER actions_no_delete
        BEFORE DELETE ON actions
        BEGIN
            SELECT RAISE(ABORT, 'actions are append-only');
        END;

        CREATE TRIGGER events_no_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END;

        CREATE TRIGGER events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are append-only');
        END;

        CREATE TRIGGER event_firings_no_update
        BEFORE UPDATE ON event_firings
        BEGIN
            SELECT RAISE(ABORT, 'event firings are append-only');
        END;

        CREATE TRIGGER event_firings_no_delete
        BEFORE DELETE ON event_firings
        BEGIN
            SELECT RAISE(ABORT, 'event firings are append-only');
        END;

        CREATE TRIGGER records_no_update_when_immutable
        BEFORE UPDATE ON records
        WHEN OLD.model_type = 'WorldManifest'
          OR OLD.model_type = 'Document'
          OR (OLD.model_type = 'Journal'
              AND json_extract(OLD.payload, '$.status') = 'posted')
          OR (OLD.model_type = 'Message'
              AND json_extract(OLD.payload, '$.status') = 'sent')
          OR (
              OLD.model_type = 'Workpaper'
              AND json_extract(OLD.payload, '$.status') = 'final'
              AND NOT (
                  json_extract(NEW.payload, '$.status') = 'draft'
                  AND json_extract(NEW.payload, '$.finalized_world_time') IS NULL
                  AND json_remove(OLD.payload, '$.status', '$.finalized_world_time')
                      = json_remove(NEW.payload, '$.status', '$.finalized_world_time')
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'immutable records cannot be changed');
        END;

        CREATE TRIGGER records_no_delete_when_immutable
        BEFORE DELETE ON records
        WHEN OLD.model_type = 'WorldManifest'
          OR OLD.model_type = 'Document'
          OR (OLD.model_type = 'Journal'
              AND json_extract(OLD.payload, '$.status') = 'posted')
          OR (OLD.model_type = 'Message'
              AND json_extract(OLD.payload, '$.status') = 'sent')
          OR (OLD.model_type = 'Workpaper'
              AND json_extract(OLD.payload, '$.status') = 'final')
        BEGIN
            SELECT RAISE(ABORT, 'immutable records cannot be deleted');
        END;
        """
    )
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        ("schema_version", _SCHEMA_VERSION),
    )
    connection.commit()


def _verify_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", ("schema_version",)
    ).fetchone()
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_names = {str(table_row["name"]) for table_row in table_rows}
    if (
        row is None
        or row["value"] != _SCHEMA_VERSION
        or not _REQUIRED_TABLES <= table_names
    ):
        raise ValueError("not a compatible Mirror Firm world database")


def immutable_constraints_present(path: str | Path) -> bool:
    """Check that a compiled database retains every I-2 immutability trigger."""

    database_path = Path(path).resolve()
    if not database_path.is_file():
        return False
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        trigger_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    finally:
        connection.close()
    return _IMMUTABILITY_TRIGGERS <= {str(row[0]) for row in trigger_rows}


def _require_record_model(model_type: type[VersionedModel]) -> None:
    if model_type not in _PERSISTED_MODEL_TYPES:
        raise TypeError(
            f"{model_type.__name__} is an embedded value model, not a persisted record"
        )
    if model_type in (Action, Event):
        raise TypeError(f"{model_type.__name__} must use its append-only log table")


def _record_identifier(record: VersionedModel) -> str:
    field_name = "world_id" if isinstance(record, WorldManifest) else "id"
    identifier = getattr(record, field_name, None)
    if not isinstance(identifier, str) or not identifier:
        raise TypeError(f"{type(record).__name__} has no persisted identifier")
    return identifier


def _serialise_model(record: VersionedModel) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _append_log_record(
    connection: sqlite3.Connection,
    table: Literal["actions", "events"],
    record: Action | Event,
) -> None:
    if table == "actions":
        if not isinstance(record, Action):
            raise TypeError("actions table accepts only Action records")
        connection.execute(
            "INSERT INTO actions (id, step, payload) VALUES (?, ?, ?)",
            (record.id, record.step, _serialise_model(record)),
        )
        return

    if not isinstance(record, Event):
        raise TypeError("events table accepts only Event records")
    if record.fired:
        raise ValueError("events must be appended before they fire")
    connection.execute(
        "INSERT INTO events (id, payload) VALUES (?, ?)",
        (record.id, _serialise_model(record)),
    )


def _save_event(
    connection: sqlite3.Connection, event: Event, *, commit: bool = True
) -> None:
    """Persist an event creation or its one append-only fired transition."""

    existing = _get_record(connection, Event, event.id)
    if existing is None:
        _append_log_record(connection, "events", event)
        if commit:
            connection.commit()
        return

    scheduled = existing.model_copy(update={"fired": False})
    requested = event.model_copy(update={"fired": False})
    if not existing.fired and event.fired and scheduled == requested:
        connection.execute(
            "INSERT INTO event_firings (event_id) VALUES (?)", (event.id,)
        )
        if commit:
            connection.commit()
        return
    raise ValueError("event schedules are immutable and may fire only once")


def _get_record(
    connection: sqlite3.Connection, model_type: type[ModelT], entity_id: str
) -> ModelT | None:
    if model_type is Action:
        row = connection.execute(
            "SELECT payload FROM actions WHERE id = ?", (entity_id,)
        ).fetchone()
    elif model_type is Event:
        row = connection.execute(
            "SELECT payload FROM events WHERE id = ?", (entity_id,)
        ).fetchone()
    else:
        _require_record_model(model_type)
        row = connection.execute(
            "SELECT payload FROM records WHERE model_type = ? AND id = ?",
            (model_type.__name__, entity_id),
        ).fetchone()
    if row is None:
        return None
    if model_type is Event:
        event = Event.model_validate_json(row["payload"])
        return cast(ModelT, _event_with_firing_state(connection, event))
    return model_type.model_validate_json(row["payload"])


def _list_records(
    connection: sqlite3.Connection, model_type: type[ModelT]
) -> tuple[ModelT, ...]:
    if model_type is Action:
        rows = connection.execute(
            "SELECT payload FROM actions ORDER BY step, id"
        ).fetchall()
    elif model_type is Event:
        rows = connection.execute("SELECT payload FROM events ORDER BY id").fetchall()
    else:
        _require_record_model(model_type)
        rows = connection.execute(
            "SELECT payload FROM records WHERE model_type = ? ORDER BY id",
            (model_type.__name__,),
        ).fetchall()
    if model_type is Event:
        events = tuple(
            _event_with_firing_state(
                connection, Event.model_validate_json(row["payload"])
            )
            for row in rows
        )
        return cast(tuple[ModelT, ...], events)
    return tuple(model_type.model_validate_json(row["payload"]) for row in rows)


def _event_with_firing_state(connection: sqlite3.Connection, event: Event) -> Event:
    """Project immutable schedules plus append-only firing records as an Event."""

    firing = connection.execute(
        "SELECT 1 FROM event_firings WHERE event_id = ?", (event.id,)
    ).fetchone()
    return event.model_copy(update={"fired": firing is not None})


def _logical_state(connection: sqlite3.Connection) -> dict[str, list[object]]:
    state: dict[str, list[object]] = {
        model_type.__name__: [] for model_type in _PERSISTED_MODEL_TYPES
    }
    for model_type in _PERSISTED_MODEL_TYPES:
        if model_type in (Action, Event):
            records: tuple[VersionedModel, ...] = _list_records(connection, model_type)
        else:
            records = _list_records(connection, model_type)
        state[model_type.__name__] = [
            record.model_dump(mode="json") for record in records
        ]
    return state
