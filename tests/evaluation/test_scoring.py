"""WP-09 regression coverage for scoring, reliability, and scores.json output."""

from __future__ import annotations

from pathlib import Path

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import (
    Budget,
    DeterministicCriterion,
    EpisodeManifest,
    ExpectedState,
    JudgeCriterion,
    ReferenceResult,
)
from mirrorfirm.evaluation.qualitative import QualitativeJudge
from mirrorfirm.evaluation.run_eval import (
    evaluate_run,
    write_aggregate_scores,
    write_scores,
)
from mirrorfirm.evaluation.scoring import (
    aggregate_results,
    efficiency_score,
    score_evaluation,
)
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"


def test_scoring_zeros_critical_failures_and_aggregates_pass_reliability() -> None:
    """§H weights and Pass@k/Pass^k are deterministic and safety-first."""

    result = score_evaluation(
        accounting=1.0,
        state=1.0,
        provenance=1.0,
        task_completion=1.0,
        communication=1.0,
        safety=1.0,
        efficiency=1.0,
        critical_failure_count=0,
        deterministic_passed=True,
        qualitative_passed=True,
    )
    failed = score_evaluation(
        accounting=1.0,
        state=1.0,
        provenance=1.0,
        task_completion=1.0,
        communication=1.0,
        safety=1.0,
        efficiency=1.0,
        critical_failure_count=1,
        deterministic_passed=True,
        qualitative_passed=True,
    )

    assert result.overall == 1.0
    assert result.all_pass is True
    assert failed.overall == 0.0
    assert failed.all_pass is False
    aggregate = aggregate_results([result, failed, result])
    assert aggregate.pass_at_k == 1
    assert aggregate.pass_to_k == 0


def test_scoring_uses_the_fixed_h_weights_and_efficiency_formula() -> None:
    """Layer weights and efficiency are neither inferred nor configurable."""

    result = score_evaluation(
        accounting=0.5,
        state=0.5,
        provenance=0.5,
        task_completion=0.5,
        communication=0.5,
        safety=0.5,
        efficiency=0.5,
        critical_failure_count=0,
        deterministic_passed=False,
        qualitative_passed=True,
    )

    assert result.overall == 0.5
    assert result.all_pass is False
    assert (
        efficiency_score(
            reference_steps=10,
            steps_used=20,
            reference_world_days=4,
            world_days_used=16,
        )
        == 0.25
    )


def test_evaluator_writes_the_extended_e17_scores_json(tmp_path: Path) -> None:
    """Per-run output is the stable EvaluationResult schema, not Harvey legacy JSON."""

    compiled = compile_world(WORLD, tmp_path / "world.db")
    with WorldStore.open(compiled.database_path) as store:
        initial = store.snapshot(
            tmp_path / "initial.db",
            snapshot_id="snp-r000001-initial",
            episode_run_id="run-r000001",
            phase="initial",
        )
        final = store.snapshot(
            tmp_path / "final.db",
            snapshot_id="snp-r000001-final",
            episode_run_id="run-r000001",
            phase="final",
        )
    episode = EpisodeManifest(
        episode_id="epi-eval-fictional",
        title="Fictional evaluator output",
        world_id="wld-uk-wyrley-brook",
        world_version="0.1.0",
        jurisdiction="uk",
        engagement_id="eng-brightpath-bookkeeping",
        agent_person_id="per-agent",
        instruction="Fictional only.",
        allowed_tools=["finish_episode"],
        event_ids=[],
        budget=Budget(max_steps=4, max_tokens=100, max_world_days=1),
        deliverables=[],
        deterministic_criteria=[
            DeterministicCriterion(
                id="state",
                grader_id="expected_state",
                params={},
                layer="state",
            )
        ],
        qualitative_criteria=[],
        critical_failures_active=[],
        expected_state=ExpectedState(assertions=[]),
        reference=ReferenceResult(
            reference_script="tests/evaluation/fake",
            final_state_digest="0" * 64,
            note="reference-scripted-run; not model performance",
        ),
        commercial_rationale="Fictional regression coverage only.",
    )

    result = evaluate_run(
        episode,
        initial,
        final,
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "wall_clock_seconds": 0.5,
            "tool_metrics": {"tool_calls": 1, "world_days_elapsed": 0.0},
        },
        model="fictional/scripted",
    )
    path = write_scores(result, tmp_path / "scores.json")

    assert path.name == "scores.json"
    assert result.model_validate_json(path.read_text(encoding="utf-8")) == result
    assert result.all_pass is True
    aggregate_path = write_aggregate_scores(
        [result, result, result], tmp_path / "aggregate.json", k=3
    )
    aggregate = aggregate_path.read_text(encoding="utf-8")
    assert '"pass_at_1": 1' in aggregate
    assert '"pass_to_3": 1' in aggregate


def test_evaluator_serializes_dual_judge_review_disagreement(tmp_path: Path) -> None:
    """A dual qualitative disagreement remains visible in E.17 output."""

    compiled = compile_world(WORLD, tmp_path / "world.db")
    with WorldStore.open(compiled.database_path) as store:
        initial = store.snapshot(
            tmp_path / "initial.db",
            snapshot_id="snp-review-initial",
            episode_run_id="run-review",
            phase="initial",
        )
        final = store.snapshot(
            tmp_path / "final.db",
            snapshot_id="snp-review-final",
            episode_run_id="run-review",
            phase="final",
        )
    episode = _evaluation_episode().model_copy(
        update={
            "qualitative_criteria": [
                JudgeCriterion(
                    id="tone",
                    prompt="Is the fictional summary clear?",
                    target="final_summary",
                    scale="one_to_five",
                    weight=1.0,
                )
            ]
        }
    )
    judge = QualitativeJudge(
        {
            "fictional/a": lambda _prompt: '{"score": 5, "reasoning": "clear"}',
            "fictional/b": lambda _prompt: '{"score": 2, "reasoning": "unclear"}',
        }
    )

    result = evaluate_run(
        episode,
        initial,
        final,
        {"tool_metrics": {}},
        model="fictional/model",
        qualitative_judge=judge,
        judge_models=["fictional/a", "fictional/b"],
    )

    qualitative = next(
        item for item in result.criterion_results if item.criterion_id == "tone"
    )
    assert qualitative.requires_review is True


def _evaluation_episode() -> EpisodeManifest:
    return EpisodeManifest(
        episode_id="epi-eval-fictional",
        title="Fictional evaluator output",
        world_id="wld-uk-wyrley-brook",
        world_version="0.1.0",
        jurisdiction="uk",
        engagement_id="eng-brightpath-bookkeeping",
        agent_person_id="per-agent",
        instruction="Fictional only.",
        allowed_tools=["finish_episode"],
        event_ids=[],
        budget=Budget(max_steps=4, max_tokens=100, max_world_days=1),
        deliverables=[],
        deterministic_criteria=[
            DeterministicCriterion(
                id="state",
                grader_id="expected_state",
                params={},
                layer="state",
            )
        ],
        qualitative_criteria=[],
        critical_failures_active=[],
        expected_state=ExpectedState(assertions=[]),
        reference=ReferenceResult(
            reference_script="tests/evaluation/fake",
            final_state_digest="0" * 64,
            note="reference-scripted-run; not model performance",
        ),
        commercial_rationale="Fictional regression coverage only.",
    )
