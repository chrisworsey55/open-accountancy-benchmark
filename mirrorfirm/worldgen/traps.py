"""Trap-register parsing and structural coverage checks for authored worlds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Trap(BaseModel):
    """One deliberately authored scenario trap from ``traps.yaml``."""

    model_config = ConfigDict(extra="forbid")

    trap_id: str
    location_refs: list[str] = Field(min_length=1)
    conflicts_with: list[str] = Field(default_factory=list)
    expected_behaviour: str
    tempting_behaviour: str
    grading_refs: list[str] = Field(min_length=1)
    severity: Literal["minor", "material", "critical"]
    visible_to_agent: Literal["visible", "discoverable", "hidden"]


class TrapRegisterError(ValueError):
    """A trap register is malformed or does not map to compiled world records."""


@dataclass(frozen=True)
class TrapApplication:
    """Structural result of registering authored traps for a compiled world."""

    trap_ids: tuple[str, ...]
    referenced_record_ids: tuple[str, ...]


def load_trap_register(path: str | Path) -> tuple[Trap, ...]:
    """Parse a YAML trap register into validated, deterministic trap records."""

    trap_path = Path(path)
    try:
        raw = yaml.safe_load(trap_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise TrapRegisterError(f"could not read trap register {trap_path}") from error
    except yaml.YAMLError as error:
        raise TrapRegisterError(f"invalid YAML in trap register {trap_path}") from error

    if raw is None:
        entries: object = []
    elif isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("traps")
    else:
        entries = None
    if not isinstance(entries, list):
        raise TrapRegisterError("trap register must be a list or an object with traps")

    try:
        traps = tuple(Trap.model_validate(entry) for entry in entries)
    except ValidationError as error:
        raise TrapRegisterError("trap register contains an invalid trap") from error

    trap_ids = [trap.trap_id for trap in traps]
    if len(set(trap_ids)) != len(trap_ids):
        raise TrapRegisterError("trap register contains duplicate trap_id values")
    return traps


def apply_traps(traps: tuple[Trap, ...], known_record_ids: set[str]) -> TrapApplication:
    """Validate that every authored trap maps to records in the compiled world.

    WP-05 records the trap register and validates its coverage.  The individual trap
    behaviours are exercised by the episode/world packets that introduce them.
    """

    referenced_ids: set[str] = set()
    for trap in traps:
        for reference in (*trap.location_refs, *trap.conflicts_with):
            if reference not in known_record_ids:
                raise TrapRegisterError(
                    f"trap {trap.trap_id!r} references unknown record {reference!r}"
                )
            referenced_ids.add(reference)
    return TrapApplication(
        trap_ids=tuple(trap.trap_id for trap in traps),
        referenced_record_ids=tuple(sorted(referenced_ids)),
    )
