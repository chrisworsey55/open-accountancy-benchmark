"""WP-02 JSON Schema build, freshness, and Pydantic round-trip coverage."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from mirrorfirm.core.models import (
    MODEL_TYPES,
    Action,
    Approval,
    BankAccount,
    Document,
    Event,
    EventPayload,
    InformationRequest,
    Journal,
    ReviewNote,
    StateAssertion,
    TaxRegistration,
    Thread,
    VersionedModel,
    Workpaper,
    WorkpaperBody,
)
from scripts.export_schemas import SCHEMA_EXPORTS, export_schemas, render_schema

ROOT = Path(__file__).resolve().parents[2]


def test_every_model_exports_a_schema_with_schema_version() -> None:
    for model in MODEL_TYPES:
        schema = model.model_json_schema()
        assert "schema_version" in schema["properties"]
        assert issubclass(model, VersionedModel)


def test_explicit_discriminated_unions_build_and_round_trip() -> None:
    values: list[tuple[object, object]] = [
        (
            EventPayload,
            {
                "kind": "deadline",
                "description": "Fictional deadline",
                "task_id": "tsk-fixture-0001",
            },
        ),
        (
            StateAssertion,
            {
                "kind": "unresolved_flagged",
                "workpaper_kind": "query_log",
                "min_items": 1,
            },
        ),
        (
            TaxRegistration,
            {"kind": "us", "sales_tax_nexus": ["MI"]},
        ),
        (
            WorkpaperBody,
            {"kind": "query_log", "items": []},
        ),
    ]

    for schema_type, value in values:
        adapter = TypeAdapter(schema_type)
        parsed = adapter.validate_python(value)
        assert adapter.validate_json(adapter.dump_json(parsed)) == parsed
        assert adapter.json_schema()


def test_corrected_audit_and_workflow_fields_are_in_exported_schemas() -> None:
    action_schema = Action.model_json_schema()
    approval_schema = Approval.model_json_schema()
    workpaper_schema = Workpaper.model_json_schema()
    review_note_schema = ReviewNote.model_json_schema()

    assert {"input_payload", "output_payload"} <= action_schema["properties"].keys()
    assert approval_schema["properties"]["status"]["enum"] == [
        "requested",
        "granted",
        "rejected",
        "expired",
    ]
    assert "approved_draft_digest" in approval_schema["properties"]
    for model in (
        Approval,
        BankAccount,
        Document,
        Event,
        InformationRequest,
        Journal,
        Thread,
    ):
        assert "engagement_id" in model.model_json_schema()["properties"]
    assert "body" in workpaper_schema["properties"]
    assert {"addressed_by", "addressed_world_time"} <= review_note_schema[
        "properties"
    ].keys()


def test_schema_export_is_complete_and_fresh(tmp_path: Path) -> None:
    written = export_schemas(tmp_path)

    assert {path.stem for path in written} == set(SCHEMA_EXPORTS)
    for path in written:
        assert json.loads(path.read_text(encoding="utf-8"))

    for name, schema_type in SCHEMA_EXPORTS.items():
        tracked = ROOT / "schemas" / f"{name}.json"
        assert tracked.read_text(encoding="utf-8") == render_schema(schema_type)
