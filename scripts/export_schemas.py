"""Export the stable WP-02 Pydantic schemas as deterministic JSON Schema files.

Run with ``uv run python scripts/export_schemas.py`` from the repository root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mirrorfirm.core.models import (  # noqa: E402
    MODEL_TYPES,
    ApprovalActionDescriptor,
    EventPayload,
    EventTrigger,
    StateAssertion,
    TaxRegistration,
    WorkpaperBody,
)
from mirrorfirm.learning.schemas import (  # noqa: E402
    DifferenceRecord,
    FailureCluster,
    HumanEffort,
    ImprovementTask,
    PractitionerCorrection,
    ProductionTrace,
    TargetedEvalRef,
)

SCHEMA_EXPORTS: dict[str, Any] = {model.__name__: model for model in MODEL_TYPES}
SCHEMA_EXPORTS.update(
    {
        "ApprovalActionDescriptor": ApprovalActionDescriptor,
        "EventPayload": EventPayload,
        "EventTrigger": EventTrigger,
        "StateAssertion": StateAssertion,
        "TaxRegistration": TaxRegistration,
        "WorkpaperBody": WorkpaperBody,
    }
)
SCHEMA_EXPORTS.update(
    {
        model.__name__: model
        for model in (
            DifferenceRecord,
            FailureCluster,
            HumanEffort,
            ImprovementTask,
            PractitionerCorrection,
            ProductionTrace,
            TargetedEvalRef,
        )
    }
)


def schema_for(schema_type: Any) -> dict[str, Any]:
    """Build JSON Schema for a model or an explicitly discriminated union."""

    if isinstance(schema_type, type):
        return schema_type.model_json_schema()
    return TypeAdapter(schema_type).json_schema()


def render_schema(schema_type: Any) -> str:
    """Render an exported schema in the deterministic repository format."""

    return (
        json.dumps(
            schema_for(schema_type), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def export_schemas(destination: Path = ROOT / "schemas") -> list[Path]:
    """Replace generated schema files and return the paths written."""

    destination.mkdir(parents=True, exist_ok=True)
    for existing in destination.glob("*.json"):
        existing.unlink()

    written: list[Path] = []
    for name, schema_type in sorted(SCHEMA_EXPORTS.items()):
        path = destination / f"{name}.json"
        path.write_text(render_schema(schema_type), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    """Export schemas into the repository's public ``schemas`` directory."""

    export_schemas()


if __name__ == "__main__":
    main()
