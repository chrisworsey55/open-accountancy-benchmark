"""§H layer weighting, efficiency, and reliability aggregation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt

from mirrorfirm.core.models import EvaluationResult, LayerScores


@dataclass(frozen=True)
class ScoredEvaluation:
    """The score portion of an evaluation before it is wrapped in E.17 metadata."""

    scores: LayerScores
    overall: float
    all_pass: bool


@dataclass(frozen=True)
class ReliabilityAggregate:
    """Aggregate score and the required Pass@k / Pass^k reliability signals."""

    run_count: int
    mean_overall: float
    all_pass_rate: float
    pass_at_k: int
    pass_to_k: int


def clamp(value: float) -> float:
    """Clamp a layer component to the closed interval required by §H."""

    return min(1.0, max(0.0, value))


def efficiency_score(
    *,
    reference_steps: int,
    steps_used: int,
    reference_world_days: float,
    world_days_used: float,
) -> float:
    """Implement the prescribed multiplicative efficiency score exactly."""

    step_ratio = (
        1.0
        if steps_used == 0 and reference_steps == 0
        else reference_steps / max(steps_used, 1)
    )
    day_ratio = (
        1.0
        if world_days_used == 0 and reference_world_days == 0
        else reference_world_days / max(world_days_used, 1e-12)
    )
    return clamp(step_ratio) * sqrt(clamp(day_ratio))


def score_evaluation(
    *,
    accounting: float,
    state: float,
    provenance: float,
    task_completion: float,
    communication: float,
    safety: float,
    efficiency: float,
    critical_failure_count: int,
    deterministic_passed: bool,
    qualitative_passed: bool,
) -> ScoredEvaluation:
    """Apply §H's fixed layer weights and the safety-first terminal override."""

    scores = LayerScores(
        accounting=clamp(accounting),
        state=clamp(state),
        provenance=clamp(provenance),
        task_completion=clamp(task_completion),
        communication=clamp(communication),
        safety=clamp(safety),
        efficiency=clamp(efficiency),
    )
    raw_overall = (
        0.30 * scores.accounting
        + 0.20 * scores.state
        + 0.15 * scores.provenance
        + 0.10 * scores.task_completion
        + 0.10 * scores.communication
        + 0.10 * scores.safety
        + 0.05 * scores.efficiency
    )
    has_critical_failure = critical_failure_count > 0
    return ScoredEvaluation(
        scores=scores,
        overall=0.0 if has_critical_failure else raw_overall,
        all_pass=(
            not has_critical_failure and deterministic_passed and qualitative_passed
        ),
    )


def aggregate_results(
    results: Sequence[object], *, k: int | None = None
) -> ReliabilityAggregate:
    """Aggregate only native Mirror Firm evaluations using the first ``k`` runs."""

    if not results:
        raise ValueError("at least one evaluation result is required")
    if k is not None and (k <= 0 or len(results) < k):
        raise ValueError(
            "k must be positive and no greater than the completed run count"
        )
    selected = list(results if k is None else results[:k])
    native_results = [_require_native_aggregation_result(result) for result in selected]
    overall = [result.overall for result in native_results]
    all_pass = [result.all_pass for result in native_results]
    return ReliabilityAggregate(
        run_count=len(selected),
        mean_overall=sum(overall) / len(overall),
        all_pass_rate=sum(all_pass) / len(all_pass),
        pass_at_k=int(any(all_pass)),
        pass_to_k=int(all(all_pass)),
    )


def _require_native_aggregation_result(
    result: object,
) -> ScoredEvaluation | EvaluationResult:
    """Fail closed before a contaminated or untyped result reaches headline metrics."""

    if isinstance(result, (ScoredEvaluation, EvaluationResult)):
        return result
    contamination = getattr(result, "contamination", None)
    if contamination == "public-reference-answers":
        raise ValueError(
            "contaminated external APEX results cannot enter native aggregation"
        )
    raise ValueError(
        "native aggregation accepts only verified Mirror Firm EvaluationResult values"
    )


def reliability_summary(
    results: Sequence[EvaluationResult],
) -> dict[str, float | int]:
    """Return the suite columns required for Pass@1/5 and headline Pass^3/5."""

    aggregate = aggregate_results(results)
    summary: dict[str, float | int] = {
        "run_count": aggregate.run_count,
        "mean_overall": aggregate.mean_overall,
        "all_pass_rate": aggregate.all_pass_rate,
    }
    for value in (1, 3, 5):
        if len(results) >= value:
            run_group = aggregate_results(results, k=value)
            summary[f"pass_at_{value}"] = run_group.pass_at_k
            summary[f"pass_to_{value}"] = run_group.pass_to_k
    return summary
