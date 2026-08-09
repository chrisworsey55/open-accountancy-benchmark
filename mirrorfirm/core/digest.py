"""Canonical logical-state serialisation and SHA-256 digests.

The digest intentionally accepts mappings as well as Pydantic models because SQLite
persistence is introduced only in WP-03.  A later loader can therefore pass its
logical table mapping directly without changing this stable canonicalisation rule.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel

NON_LOGICAL_METADATA_FIELDS = frozenset(
    {
        "rowid",
        "sqlite_rowid",
        "created_at",
        "updated_at",
        "db_path",
    }
)
"""Storage metadata excluded from a logical-state digest (§D.4)."""


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical logical state requires timezone-aware datetimes")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sort_table_rows(value: list[Any]) -> list[Any]:
    """Sort an entity-table row collection by stable ``id`` where available."""

    if value and all(isinstance(item, Mapping) and "id" in item for item in value):
        return sorted(value, key=lambda item: str(item["id"]))
    return value


def canonical_logical_state(value: object) -> object:
    """Return the JSON-compatible logical representation required by §D.4.

    Mapping keys are normalised to strings, storage-only metadata is omitted, entity
    table rows are sorted by ``id``, and datetimes are rendered as UTC ISO-8601.  The
    subsequent JSON encoding orders every object field lexically.
    """

    if isinstance(value, BaseModel):
        return canonical_logical_state(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("logical-state mapping keys must be strings")
            if raw_key in NON_LOGICAL_METADATA_FIELDS:
                continue
            normalized[raw_key] = canonical_logical_state(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [canonical_logical_state(item) for item in value]
        return _sort_table_rows(items)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported logical-state value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Encode logical state into the exact deterministic JSON byte representation."""

    normalized = canonical_logical_state(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def logical_state_digest(value: object) -> str:
    """Compute ``sha256(canonical_json(logical_state))`` as specified in §D.4."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
