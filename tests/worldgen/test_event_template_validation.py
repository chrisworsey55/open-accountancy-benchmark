"""Regression coverage for compile-time event template validation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from mirrorfirm.worldgen import WorldCompileError, compile_world, validate_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "us-lakeshore"


def _copy_world_with_event_attack(tmp_path: Path, attack: str) -> Path:
    """Copy a valid world and make one event template structurally malformed."""

    world = tmp_path / attack
    shutil.copytree(WORLD, world)
    events_path = world / "events.yaml"
    raw = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    events = raw["events"]
    assert isinstance(events, list)
    reply = next(event for event in events if event["id"] == "evt-us01-cedarline-reply")
    deadline = next(
        event for event in events if event["id"] == "evt-us02-marlowe-recon-deadline"
    )

    if attack == "missing_bound_field":
        reply["payload"]["thread_ref"] = "${entity.not_a_real_field}"
    elif attack == "wrong_record_family":
        reply["payload"]["thread_ref"] = "${entity.client_id}"
    elif attack == "unbound_at_time_template":
        deadline["payload"]["task_id"] = "${entity.id}"
    elif attack == "unknown_binding":
        reply["payload"]["thread_ref"] = "${binding.thread_id}"
    elif attack == "dangling_resolved_id":
        reply["payload"]["thread_ref"] = "thr-does-not-exist"
    elif attack == "cross_client_resolved_id":
        reply["payload"]["thread_ref"] = "thr-marlowe-benchmark-request"
    elif attack == "cross_engagement_resolved_id":
        records_path = world / "records.yaml"
        records = yaml.safe_load(records_path.read_text(encoding="utf-8"))
        assert isinstance(records, dict)
        engagements = records["Engagement"]
        tasks = records["Task"]
        assert isinstance(engagements, list) and isinstance(tasks, list)
        engagements.append(
            {
                "id": "eng-cedarline-secondary",
                "client_id": "cli-cedarline",
                "scope": "bookkeeping",
                "period_ids": [],
                "status": "active",
            }
        )
        tasks.append(
            {
                "id": "tsk-cedarline-secondary",
                "engagement_id": "eng-cedarline-secondary",
                "assignee": "per-agent",
                "title": "Fictional secondary task",
                "description": "Fictional isolation regression record.",
                "due": "2026-05-31",
                "status": "open",
            }
        )
        deadline["engagement_id"] = "eng-cedarline-bookkeeping"
        deadline["payload"]["task_id"] = "tsk-cedarline-secondary"
        records_path.write_text(
            yaml.safe_dump(records, sort_keys=False), encoding="utf-8"
        )
    elif attack == "nested_missing_bound_field":
        reply["payload"]["body"] = "Fictional ${entity.not_a_real_field} reply."
    elif attack == "circular_trigger_binding":
        reply["trigger"]["match"]["client_id"] = "${entity.client_id}"
    elif attack == "invalid_timestamp":
        deadline["trigger"]["world_time"] = "not-a-timestamp"
    elif attack == "nullable_bound_thread":
        records_path = world / "records.yaml"
        records = yaml.safe_load(records_path.read_text(encoding="utf-8"))
        assert isinstance(records, dict)
        requests = records.setdefault("InformationRequest", [])
        assert isinstance(requests, list)
        requests.append(
            {
                "id": "irq-cedarline-null-thread",
                "client_id": "cli-cedarline",
                "thread_id": None,
                "items": [],
                "status": "sent",
                "engagement_id": "eng-cedarline-bookkeeping",
            }
        )
        records_path.write_text(
            yaml.safe_dump(records, sort_keys=False), encoding="utf-8"
        )
    else:
        raise AssertionError(f"unknown attack {attack!r}")

    events_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return world


@pytest.mark.parametrize(
    "attack", ["missing_bound_field", "wrong_record_family", "unbound_at_time_template"]
)
def test_compile_and_validate_reject_invalid_event_templates(
    tmp_path: Path, attack: str
) -> None:
    """Templates must be structurally sound before an event can reach runtime."""

    world = _copy_world_with_event_attack(tmp_path, attack)

    with pytest.raises(WorldCompileError, match="template"):
        compile_world(world, tmp_path / f"{attack}.db")

    report = validate_world(world)
    assert not report.passed


def test_compile_and_validate_reject_a_nullable_bound_template_value(
    tmp_path: Path,
) -> None:
    """A matching bound entity cannot supply ``None`` to a required template."""

    world = _copy_world_with_event_attack(tmp_path, "nullable_bound_thread")

    with pytest.raises(WorldCompileError, match="template.*thread_id"):
        compile_world(world, tmp_path / "nullable-bound-thread.db")
    assert not validate_world(world).passed


@pytest.mark.parametrize(
    "attack",
    [
        "unknown_binding",
        "dangling_resolved_id",
        "cross_client_resolved_id",
        "cross_engagement_resolved_id",
        "nested_missing_bound_field",
        "circular_trigger_binding",
        "invalid_timestamp",
    ],
)
def test_compile_and_validate_reject_all_template_and_resolved_reference_boundaries(
    tmp_path: Path, attack: str
) -> None:
    """Nested, circular, dangling, and cross-client event values fail pre-runtime."""

    world = _copy_world_with_event_attack(tmp_path, attack)

    with pytest.raises(WorldCompileError):
        compile_world(world, tmp_path / f"{attack}.db")
    assert not validate_world(world).passed


@pytest.mark.parametrize(
    "world", [ROOT / "worlds" / "uk-wyrley-brook", ROOT / "worlds" / "us-lakeshore"]
)
def test_all_authored_event_templates_compile_and_validate(
    world: Path, tmp_path: Path
) -> None:
    """Every authored UK and US event remains structurally valid before runtime."""

    compiled = compile_world(world, tmp_path / f"{world.name}.db")
    assert compiled.state_digest
    assert validate_world(world).passed
