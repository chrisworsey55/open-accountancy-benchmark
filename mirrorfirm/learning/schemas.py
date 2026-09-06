"""Versioned public learning contracts. No production capture or pipeline logic."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class LearningContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1"] = "0.1"


class HumanEffort(LearningContract):
    """Explicit observations, never inferred from model scores or review flags.

    None means not measured. Zero means a measured zero. No practitioner identity
    belongs in this public interchange schema.
    """

    task_ref: str = Field(min_length=1)
    intervention_count: int | None = Field(default=None, ge=0)
    review_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    rework_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    measurement_method: Literal["not_measured", "observed", "self_reported"] = (
        "not_measured"
    )

    @model_validator(mode="after")
    def validate_observation(self) -> HumanEffort:
        values = (self.intervention_count, self.review_minutes, self.rework_minutes)
        if self.measurement_method == "not_measured" and any(
            value is not None for value in values
        ):
            raise ValueError("unmeasured effort must not contain invented observations")
        if self.measurement_method != "not_measured" and all(
            value is None for value in values
        ):
            raise ValueError("a measurement requires at least one observation")
        return self


class ProductionTrace(LearningContract):
    trace_id: str = Field(min_length=1)
    context_ref: str = Field(min_length=1)
    actions: list[dict[str, JsonValue]]
    outputs: list[dict[str, JsonValue]]
    world_or_prod: Literal["synthetic", "production"]
    human_effort: list[HumanEffort] = Field(default_factory=list)


class PractitionerCorrection(LearningContract):
    trace_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    observed: JsonValue
    expected: JsonValue
    category: Literal[
        "extraction_miss",
        "mapping_error",
        "unsupported_capability",
        "practitioner_preference",
        "workflow_noise",
        "judgement",
    ]
    note: str = Field(min_length=1)


class DifferenceRecord(LearningContract):
    correction_id: str = Field(min_length=1)
    actionable: bool
    field_path: str = Field(min_length=1)


class FailureCluster(LearningContract):
    cluster_id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    member_refs: list[str] = Field(min_length=1)
    status: Literal["candidate", "validated", "rejected"]


class TargetedEvalRef(LearningContract):
    episode_id: str | None = Field(default=None, min_length=1)
    criterion_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_target(self) -> TargetedEvalRef:
        if (self.episode_id is None) == (self.criterion_id is None):
            raise ValueError("provide exactly one episode_id or criterion_id")
        return self


class ImprovementTask(LearningContract):
    cluster_id: str = Field(min_length=1)
    bounded_scope: str = Field(min_length=1)
    targeted_eval_refs: list[TargetedEvalRef] = Field(min_length=1)
    regression_suite_ref: str = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)


def emit_synthetic_trace(
    *,
    trace_id: str,
    episode_id: str,
    actions: list[dict[str, JsonValue]],
    outputs: list[dict[str, JsonValue]],
    human_effort: list[HumanEffort] | None = None,
) -> ProductionTrace:
    """Validate an episode export; callers must supply only fictional, scrubbed data."""
    if not episode_id.startswith("epi-"):
        raise ValueError("a synthetic trace needs an episode identifier")
    return ProductionTrace(
        trace_id=trace_id,
        context_ref=episode_id,
        actions=actions,
        outputs=outputs,
        world_or_prod="synthetic",
        human_effort=human_effort or [],
    )
