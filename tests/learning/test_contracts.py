"""Public contracts validate boundaries without claiming production ingestion."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from mirrorfirm.learning.schemas import (
    HumanEffort,
    ProductionTrace,
    TargetedEvalRef,
    emit_synthetic_trace,
)


def test_synthetic_trace_round_trip_and_unknown_fields():
    trace = emit_synthetic_trace(
        trace_id="trc-fictional",
        episode_id="epi-uk-02",
        actions=[{"tool": "calculate", "result": 1240}],
        outputs=[],
    )
    assert ProductionTrace.model_validate_json(trace.model_dump_json()) == trace
    with pytest.raises(ValidationError):
        ProductionTrace.model_validate(
            {**trace.model_dump(), "practitioner_email": "fictional@example.invalid"}
        )


def test_effort_distinguishes_unknown_from_measured_zero():
    assert HumanEffort(task_ref="tsk-fictional").review_minutes is None
    assert (
        HumanEffort(
            task_ref="tsk-fictional", review_minutes=0, measurement_method="observed"
        ).review_minutes
        == 0
    )
    with pytest.raises(ValidationError):
        HumanEffort(task_ref="tsk-fictional", review_minutes=0)
    with pytest.raises(ValidationError):
        HumanEffort(
            task_ref="tsk-fictional", review_minutes=-1, measurement_method="observed"
        )
    with pytest.raises(ValidationError):
        HumanEffort(
            task_ref="tsk-fictional",
            review_minutes=float("nan"),
            measurement_method="observed",
        )


def test_target_has_exactly_one_reference():
    assert TargetedEvalRef(episode_id="epi-uk-02").criterion_id is None
    with pytest.raises(ValidationError):
        TargetedEvalRef()
    with pytest.raises(ValidationError):
        TargetedEvalRef(episode_id="epi-uk-02", criterion_id="recon_ties")


def test_learning_has_no_private_pipeline_imports():
    import sys

    root = Path(__file__).resolve().parents[2] / "mirrorfirm/learning"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert all(
                name in sys.stdlib_module_names or name == "pydantic" for name in names
            )
