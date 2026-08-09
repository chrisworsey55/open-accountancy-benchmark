"""Deterministic compilation of authored YAML world fixtures into one SQLite DB."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import (
    MODEL_TYPES,
    ClassificationSummaryWorkpaper,
    Client,
    Event,
    Journal,
    VersionedModel,
    Workpaper,
    WorldManifest,
)
from mirrorfirm.jurisdictions import get_pack

from .traps import Trap, TrapRegisterError, load_trap_register

_MODEL_BY_NAME: Final[dict[str, type[VersionedModel]]] = {
    model_type.__name__: model_type for model_type in MODEL_TYPES
}


class WorldCompileError(ValueError):
    """An authored fixture cannot be parsed or compiled into a world database."""


@dataclass(frozen=True)
class LoadedWorldFixtures:
    """Validated world-fixture models before they are persisted to SQLite."""

    root: Path
    manifest: WorldManifest
    records: tuple[VersionedModel, ...]
    traps: tuple[Trap, ...]


@dataclass(frozen=True)
class CompiledWorld:
    """The reproducible output of compiling one authored world."""

    manifest: WorldManifest
    database_path: Path
    state_digest: str


def load_world_fixtures(world_dir: str | Path) -> LoadedWorldFixtures:
    """Load the manifest-directed YAML fixtures and optional generic record fixtures.

    The manifest owns ``practice_file``, ``client_files``, ``events_file``, and
    ``trap_register_file``.  Additional top-level §E records belong in an optional
    ``records.yaml`` mapping keyed by Pydantic model name, for example
    ``{Person: [{...}], Engagement: [{...}]}``.
    """

    root = Path(world_dir).resolve()
    manifest_path = root / "world.yaml"
    manifest = _parse_one(WorldManifest, _read_yaml(manifest_path), manifest_path)
    practice = _parse_one(
        _model("Practice"),
        _read_yaml(root / manifest.practice_file),
        root / manifest.practice_file,
    )

    records: list[VersionedModel] = [manifest, practice]
    for client_file in manifest.client_files:
        path = root / client_file
        records.extend(_parse_many(_model("Client"), _read_yaml(path), path))

    events_path = root / manifest.events_file
    events_raw = _read_yaml(events_path)
    if isinstance(events_raw, dict):
        events_raw = events_raw.get("events", [])
    records.extend(_parse_many(Event, events_raw, events_path))

    records_path = root / "records.yaml"
    if records_path.exists():
        extra_records = _read_yaml(records_path)
        if not isinstance(extra_records, dict):
            raise WorldCompileError("records.yaml must map model names to record lists")
        for model_name, values in extra_records.items():
            if not isinstance(model_name, str):
                raise WorldCompileError("records.yaml model names must be strings")
            if model_name == "WorldManifest":
                raise WorldCompileError("WorldManifest belongs only in world.yaml")
            model_type = _model(model_name)
            records.extend(_parse_many(model_type, values, records_path))

    try:
        traps = load_trap_register(root / manifest.trap_register_file)
    except TrapRegisterError as error:
        raise WorldCompileError(str(error)) from error

    return LoadedWorldFixtures(
        root=root,
        manifest=manifest,
        records=_normalise_records(records),
        traps=traps,
    )


def compile_world(
    world_dir: str | Path, output_db: str | Path | None = None
) -> CompiledWorld:
    """Compile one authored fixture directory into a new deterministic SQLite DB."""

    fixtures = load_world_fixtures(world_dir)
    database_path = (
        Path(output_db).resolve()
        if output_db is not None
        else fixtures.root / "world.db"
    )
    _validate_pack_data(fixtures)

    try:
        with WorldStore.create(database_path) as store:
            for record in fixtures.records:
                store.save(record)
            state_digest = store.state_digest()
    except (OSError, ValueError) as error:
        raise WorldCompileError(
            f"could not compile world {fixtures.manifest.world_id}"
        ) from error

    return CompiledWorld(
        manifest=fixtures.manifest,
        database_path=database_path,
        state_digest=state_digest,
    )


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise WorldCompileError(f"could not read fixture {path}") from error
    except yaml.YAMLError as error:
        raise WorldCompileError(f"invalid YAML in fixture {path}") from error


def _model(name: str) -> type[VersionedModel]:
    try:
        return _MODEL_BY_NAME[name]
    except KeyError as error:
        raise WorldCompileError(f"records.yaml has unknown model {name!r}") from error


def _parse_one(
    model_type: type[VersionedModel], raw: object, path: Path
) -> VersionedModel:
    if not isinstance(raw, dict):
        raise WorldCompileError(f"fixture {path} must contain one YAML object")
    try:
        return model_type.model_validate(raw)
    except ValidationError as error:
        raise WorldCompileError(
            f"fixture {path} does not match {model_type.__name__}"
        ) from error


def _parse_many(
    model_type: type[VersionedModel], raw: object, path: Path
) -> list[VersionedModel]:
    entries = raw if isinstance(raw, list) else [raw]
    if not all(isinstance(entry, dict) for entry in entries):
        raise WorldCompileError(f"fixture {path} must contain YAML objects")
    try:
        return [model_type.model_validate(entry) for entry in entries]
    except ValidationError as error:
        raise WorldCompileError(
            f"fixture {path} does not match {model_type.__name__}"
        ) from error


def _normalise_records(records: list[VersionedModel]) -> tuple[VersionedModel, ...]:
    expanded: list[VersionedModel] = []
    for record in records:
        expanded.append(record)
        if isinstance(record, Client):
            expanded.append(record.entity)

    unique: dict[tuple[str, str], VersionedModel] = {}
    for record in expanded:
        key = (type(record).__name__, _record_id(record))
        existing = unique.get(key)
        if existing is not None and existing != record:
            raise WorldCompileError(
                f"conflicting duplicate fixture record {key[0]} {key[1]!r}"
            )
        unique[key] = record
    return tuple(unique[key] for key in sorted(unique))


def _record_id(record: VersionedModel) -> str:
    field_name = "world_id" if isinstance(record, WorldManifest) else "id"
    identifier = getattr(record, field_name, None)
    if not isinstance(identifier, str) or not identifier:
        raise WorldCompileError(
            f"{type(record).__name__} is not a persisted root model"
        )
    return identifier


def _validate_pack_data(fixtures: LoadedWorldFixtures) -> None:
    pack = get_pack(fixtures.manifest.jurisdiction)
    expected_currency = "GBP" if fixtures.manifest.jurisdiction == "uk" else "USD"
    for record in fixtures.records:
        if isinstance(record, Client):
            if record.entity.legal_form not in pack.legal_forms:
                raise WorldCompileError(
                    f"client {record.id!r} uses unsupported legal form "
                    f"{record.entity.legal_form!r}"
                )
            if record.entity.currency != expected_currency:
                raise WorldCompileError(
                    f"client {record.id!r} must use {expected_currency} currency"
                )
            if record.entity.tax.kind != fixtures.manifest.jurisdiction:
                raise WorldCompileError(
                    f"client {record.id!r} tax registration does not match the world pack"
                )
        elif isinstance(record, Journal):
            for line in record.lines:
                pack.validate_tax_tag(line.tax)
        elif isinstance(record, Workpaper) and isinstance(
            record.body, ClassificationSummaryWorkpaper
        ):
            for row in record.body.rows:
                pack.validate_tax_tag(row.tax)
