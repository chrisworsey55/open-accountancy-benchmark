"""Fail-closed qualitative checks used only by scripted-reference self-tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from mirrorfirm.core.models import JudgeCriterion

from .judge import JudgeAssessment, JudgeResult

REFERENCE_VALIDATION_MODEL = (
    "reference validation (scripted only; not model performance)"
)


class ReferenceTargetExpectation(BaseModel):
    """Explicit, non-provider expectation for one scripted reference target."""

    model_config = ConfigDict(extra="forbid")

    target: str
    required_text: list[str] = Field(min_length=1)
    forbidden_text: list[str] = Field(default_factory=list)
    min_targets: int = Field(default=1, ge=1)


class ReferenceQualitativeValidator:
    """Validate reference targets without presenting a gold answer to a model."""

    reference_only = True

    def __init__(self, expectations: Mapping[str, ReferenceTargetExpectation]) -> None:
        self._expectations = dict(expectations)

    def judge(
        self,
        criterion: JudgeCriterion,
        targets: list[str],
        judge_models: list[str],
    ) -> JudgeResult:
        """Fail closed unless the actual target satisfies its explicit expectation."""

        if judge_models != [REFERENCE_VALIDATION_MODEL]:
            raise ValueError("reference validator is unavailable to model baselines")
        try:
            expectation = self._expectations[criterion.id]
        except KeyError as error:
            raise ValueError(
                f"reference validator has no expectation for {criterion.id!r}"
            ) from error
        text = "\n\n".join(targets)
        required = [
            phrase
            for phrase in expectation.required_text
            if phrase.casefold() not in text.casefold()
        ]
        forbidden = [
            phrase
            for phrase in expectation.forbidden_text
            if phrase.casefold() in text.casefold()
        ]
        failures: list[str] = []
        if criterion.target != expectation.target:
            failures.append(
                f"expected target {expectation.target!r}, got {criterion.target!r}"
            )
        if len(targets) < expectation.min_targets or not text.strip():
            failures.append("target is empty or incomplete")
        failures.extend(f"missing required text {phrase!r}" for phrase in required)
        failures.extend(f"contains forbidden text {phrase!r}" for phrase in forbidden)
        passed = not failures
        detail = (
            "reference validation passed"
            if passed
            else "reference validation failed: " + "; ".join(failures)
        )
        return JudgeResult(
            criterion_id=criterion.id,
            score=1.0 if passed else 0.0,
            passed=passed,
            detail=detail,
            evidence_refs=targets,
            assessments=[
                JudgeAssessment(
                    model=REFERENCE_VALIDATION_MODEL,
                    band=5 if passed else 1,
                    score=1.0 if passed else 0.0,
                    passed=passed,
                    reasoning=detail,
                )
            ],
        )

    def judge_all(
        self,
        criteria: Sequence[JudgeCriterion],
        targets: Mapping[str, list[str]],
        judge_models: list[str],
    ) -> list[JudgeResult]:
        """Validate every reference-only qualitative criterion deterministically."""

        return [
            self.judge(criterion, targets.get(criterion.id, []), judge_models)
            for criterion in criteria
        ]
