"""Per-run evaluation orchestration and extended ``scores.json`` persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.core.models import (
    CriterionResult,
    EpisodeManifest,
    EvaluationResult,
    Message,
    ReviewNote,
    StateSnapshot,
    Usage,
)
from mirrorfirm.evaluation.deterministic import grade_registered
from mirrorfirm.evaluation.qualitative import (
    QualitativeJudge,
    ReferenceQualitativeValidator,
)
from mirrorfirm.evaluation.safety import detect_critical_failures
from mirrorfirm.evaluation.scoring import (
    aggregate_results,
    efficiency_score,
    reliability_summary,
    score_evaluation,
)
from mirrorfirm.evaluation.state import Deliverables, ProvenanceGraph
from mirrorfirm.reporting.artifacts import write_json_atomic, write_scores_artifact


def evaluate_run(
    episode: EpisodeManifest,
    initial_snapshot: StateSnapshot,
    final_snapshot: StateSnapshot,
    agent_result: Mapping[str, object],
    *,
    model: str,
    qualitative_judge: QualitativeJudge | ReferenceQualitativeValidator | None = None,
    judge_models: list[str] | None = None,
    cost_usd: float = 0.0,
    reference_usage: Usage | None = None,
    reference_validation: bool = False,
) -> EvaluationResult:
    """Evaluate one snapshot-backed episode and return the stable E.17 result."""

    with (
        SQLiteWorldView.open(initial_snapshot.db_path) as initial,
        SQLiteWorldView.open(final_snapshot.db_path) as final,
    ):
        actions = list(final.actions())
        deliverables = Deliverables.from_actions(actions)
        provenance = ProvenanceGraph.from_world(final)
        deterministic_results = [
            grade_registered(
                criterion.grader_id,
                initial,
                final,
                actions,
                provenance,
                deliverables,
                _criterion_params(criterion.grader_id, criterion.params, episode),
            )
            for criterion in episode.deterministic_criteria
        ]
        if isinstance(qualitative_judge, ReferenceQualitativeValidator) and (
            not reference_validation
            or model != "reference (scripted) - not model performance"
        ):
            raise ValueError("reference validator is unavailable to model baselines")
        qualitative_results, judge_flags = _grade_qualitative(
            episode,
            final,
            deliverables,
            qualitative_judge,
            judge_models,
        )
        critical_failures = detect_critical_failures(
            initial,
            final,
            actions,
            deliverables,
            active_failure_ids=episode.critical_failures_active,
            engagement_id=episode.engagement_id,
            judge_flags=judge_flags,
        )

        usage = _usage_from_agent(agent_result, cost_usd=cost_usd)
        scores = _layer_scores(
            episode,
            deterministic_results,
            qualitative_results,
            usage,
            reference_usage,
        )
        deterministic_passed = all(result.passed for result in deterministic_results)
        qualitative_passed = all(result.passed for result in qualitative_results)
        scored = score_evaluation(
            accounting=scores["accounting"],
            state=scores["state"],
            provenance=scores["provenance"],
            task_completion=scores["task_completion"],
            communication=scores["communication"],
            safety=scores["safety"],
            efficiency=scores["efficiency"],
            critical_failure_count=len(critical_failures),
            deterministic_passed=deterministic_passed,
            qualitative_passed=qualitative_passed,
        )
    return EvaluationResult(
        episode_id=episode.episode_id,
        run_id=final_snapshot.episode_run_id,
        model=model,
        critical_failures=critical_failures,
        criterion_results=[*deterministic_results, *qualitative_results],
        scores=scored.scores,
        overall=scored.overall,
        all_pass=scored.all_pass,
        usage=usage,
    )


def write_scores(result: EvaluationResult, destination: str | Path) -> Path:
    """Persist the extended per-run E.17 JSON schema as ``scores.json``."""
    return write_scores_artifact(result, destination)


def aggregate_scores(
    results: Sequence[EvaluationResult], *, k: int | None = None
) -> dict[str, float | int | str]:
    """Build the aggregate JSON payload for one episode/model grouping."""

    if not results:
        raise ValueError("at least one evaluation result is required")
    episode_ids = {result.episode_id for result in results}
    models = {result.model for result in results}
    if len(episode_ids) != 1 or len(models) != 1:
        raise ValueError("aggregate results must share one episode and model")
    aggregate = aggregate_results(results, k=k)
    payload: dict[str, float | int | str] = {
        "episode_id": results[0].episode_id,
        "model": results[0].model,
        "run_count": aggregate.run_count,
        "mean_overall": aggregate.mean_overall,
        "all_pass_rate": aggregate.all_pass_rate,
        "pass_at_k": aggregate.pass_at_k,
        "pass_to_k": aggregate.pass_to_k,
    }
    payload.update(reliability_summary(results))
    return payload


def write_aggregate_scores(
    results: Sequence[EvaluationResult],
    destination: str | Path,
    *,
    k: int | None = None,
) -> Path:
    """Persist the per-episode/model reliability aggregate as deterministic JSON."""

    destination_path = Path(destination)
    if destination_path.suffix != ".json":
        destination_path = destination_path / "aggregate.json"
    return write_json_atomic(destination_path, aggregate_scores(results, k=k))


def _criterion_params(
    grader_id: str, params: Mapping[str, object], episode: EpisodeManifest
) -> dict[str, object]:
    copied = dict(params)
    if grader_id == "expected_state" and "assertions" not in copied:
        copied["assertions"] = [
            assertion.model_dump(mode="json")
            for assertion in episode.expected_state.assertions
        ]
    return copied


def _grade_qualitative(
    episode: EpisodeManifest,
    final: SQLiteWorldView,
    deliverables: Deliverables,
    qualitative_judge: QualitativeJudge | ReferenceQualitativeValidator | None,
    judge_models: list[str] | None,
) -> tuple[list[CriterionResult], dict[str, bool]]:
    if not episode.qualitative_criteria:
        return [], {}
    if qualitative_judge is None or not judge_models:
        raise ValueError("qualitative criteria require a judge and one or two models")
    targets = {
        criterion.id: _targets_for(criterion.target, final, deliverables)
        for criterion in episode.qualitative_criteria
    }
    judged = qualitative_judge.judge_all(
        episode.qualitative_criteria, targets, judge_models
    )
    flags = {flag: True for result in judged for flag in result.critical_failure_flags}
    results = [
        CriterionResult(
            criterion_id=result.criterion_id,
            passed=result.passed,
            score=result.score,
            detail=result.detail,
            evidence_refs=result.evidence_refs,
            requires_review=result.requires_review,
        )
        for result in judged
    ]
    return results, flags


def _targets_for(
    target: str, final: SQLiteWorldView, deliverables: Deliverables
) -> list[str]:
    if target == "outbound_messages":
        return [
            message.body
            for message in final.list(Message)
            if message.direction == "outbound"
        ]
    if target == "escalations":
        return [note.body for note in final.list(ReviewNote)]
    return [deliverables.summary] if deliverables.summary is not None else []


def _usage_from_agent(agent_result: Mapping[str, object], *, cost_usd: float) -> Usage:
    metrics = agent_result.get("tool_metrics")
    metric_values = metrics if isinstance(metrics, Mapping) else {}
    return Usage(
        steps=_as_int(metric_values.get("tool_calls")),
        tokens_in=_as_int(agent_result.get("input_tokens")),
        tokens_out=_as_int(agent_result.get("output_tokens")),
        cost_usd=max(0.0, cost_usd),
        latency_s=max(0.0, _as_float(agent_result.get("wall_clock_seconds"))),
        world_days_elapsed=max(0.0, _as_float(metric_values.get("world_days_elapsed"))),
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: object) -> float:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def _layer_scores(
    episode: EpisodeManifest,
    deterministic_results: list[CriterionResult],
    qualitative_results: list[CriterionResult],
    usage: Usage,
    reference_usage: Usage | None,
) -> dict[str, float]:
    by_criterion = {result.criterion_id: result for result in deterministic_results}
    weighted: dict[str, list[tuple[float, float]]] = {
        "accounting": [],
        "state": [],
        "provenance": [],
        "task_completion": [],
        "safety": [],
        "efficiency": [],
    }
    for criterion in episode.deterministic_criteria:
        result = by_criterion.get(criterion.id)
        if result is not None:
            weighted[criterion.layer].append((result.score, criterion.weight))
    values = {layer: _weighted_average(entries) for layer, entries in weighted.items()}
    values["communication"] = _weighted_average(
        [
            (result.score, criterion.weight)
            for criterion, result in zip(
                episode.qualitative_criteria, qualitative_results, strict=True
            )
        ]
    )
    if reference_usage is None:
        values["efficiency"] = 1.0
    else:
        values["efficiency"] = efficiency_score(
            reference_steps=reference_usage.steps,
            steps_used=usage.steps,
            reference_world_days=reference_usage.world_days_elapsed,
            world_days_used=usage.world_days_elapsed,
        )
    return values


def _weighted_average(entries: list[tuple[float, float]]) -> float:
    if not entries:
        return 1.0
    total_weight = sum(weight for _, weight in entries)
    if total_weight <= 0:
        return 0.0
    return sum(score * weight for score, weight in entries) / total_weight
