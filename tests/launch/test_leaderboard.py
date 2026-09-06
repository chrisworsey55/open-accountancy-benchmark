"""Public leaderboard projections retain the benchmark artifact integrity boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mirrorfirm.launch import leaderboard as leaderboard_module
from mirrorfirm.launch.leaderboard import load_leaderboard
from mirrorfirm.reporting.artifacts import (
    AggregateArtifact,
    ComparisonIdentity,
    ResultArtifactError,
)


def _aggregate(
    *,
    model: str = "fictional/agent",
    jurisdiction: str = "uk",
    overall: float = 0.8,
    critical_failures: tuple[str, ...] = (),
    kind: str = "native_model",
    requires_review: bool = False,
    runs: int = 5,
    world_version: str = "0.1.1",
    max_steps: int = 100,
    judges: tuple[str, ...] = ("fictional/judge",),
) -> AggregateArtifact:
    return cast(
        AggregateArtifact,
        SimpleNamespace(
            configuration=SimpleNamespace(
                jurisdiction=jurisdiction,
                suite_version="fictional-suite-v1",
                scoring_version="fictional-score-v1",
                baseline=runs == 5,
            ),
            comparison_identity=ComparisonIdentity(
                suite_version="fictional-suite-v1",
                scoring_version="fictional-score-v1",
                episode_id="epi-fictional",
                world_id="wld-fictional",
                world_version=world_version,
                jurisdiction=jurisdiction,
                run_kind=kind,
                judge_models=judges,
                seed=7,
                temperature=0,
                max_steps=max_steps,
                max_tokens=400000,
                max_world_days=10,
                baseline=runs == 5,
            ),
            runs=tuple(
                SimpleNamespace(
                    fresh_world_id=f"fresh-{i}",
                    status="complete",
                    agent_result={"cost_measured": True},
                )
                for i in range(runs)
            ),
            run_count=runs,
            reliability={"all_pass_rate": 0.0},
            kind=kind,
            model=model,
            episode_id="epi-fictional",
            results=(
                SimpleNamespace(
                    overall=overall,
                    criterion_results=(
                        SimpleNamespace(requires_review=requires_review),
                    ),
                ),
            ),
            layer_means=SimpleNamespace(accounting=0.7, provenance=0.6, safety=0.9),
            usage_means=SimpleNamespace(cost_usd=1.25),
            critical_failure_ids=critical_failures,
        ),
    )


def _root(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "artifacts"
    for name in names:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "aggregate.json").write_text("{}", encoding="utf-8")
    return root


def test_score_extraction_and_unmeasured_dimensions_are_truthful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "first")
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: _aggregate()
    )

    result = load_leaderboard(root)

    row = result.ranked[0]
    assert (row.overall, row.accuracy, row.evidence, row.safety, row.cost_usd) == (
        0.8,
        0.7,
        0.6,
        0.9,
        1.25,
    )
    assert row.human_review == "N/A (not measured by this benchmark version)"
    assert row.run_date is None


def test_canonical_hashed_cli_aggregate_paths_are_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "results"
    aggregate = root / "aggregates" / "epi-fictional" / "agent"
    aggregate.mkdir(parents=True)
    (aggregate / "abc123-runs-1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: _aggregate()
    )

    assert len(load_leaderboard(root).ranked) == 1


def test_tied_scores_share_a_rank_and_sort_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "a", "b", "c")
    aggregates = iter(
        (
            _aggregate(model="fictional/a", overall=0.9),
            _aggregate(model="fictional/b", overall=0.9),
            _aggregate(model="fictional/c", overall=0.7),
        )
    )
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: next(aggregates)
    )

    result = load_leaderboard(root)

    assert [row.rank for row in result.ranked] == [1, 1, 3]


def test_jurisdiction_filter_keeps_uk_and_us_rows_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "uk", "us")
    aggregates = iter((_aggregate(jurisdiction="uk"), _aggregate(jurisdiction="us")))
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: next(aggregates)
    )

    result = load_leaderboard(root)

    assert [row.jurisdiction for row in result.for_jurisdiction("uk").ranked] == ["uk"]
    assert [row.jurisdiction for row in result.for_jurisdiction("us").ranked] == ["us"]


def test_critical_failures_are_visible_but_ineligible_for_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "failure")
    monkeypatch.setattr(
        leaderboard_module,
        "load_aggregate_artifact",
        lambda _: _aggregate(critical_failures=("CF-5",)),
    )

    result = load_leaderboard(root)

    assert result.ranked == ()
    assert result.disqualified[0].critical_failures == ("CF-5",)
    assert result.disqualified[0].rank is None


def test_scripted_references_are_separate_and_labelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "reference")
    monkeypatch.setattr(
        leaderboard_module,
        "load_aggregate_artifact",
        lambda _: _aggregate(kind="scripted_reference"),
    )

    result = load_leaderboard(root)

    assert result.ranked == ()
    assert (
        result.references[0].submission_name
        == "reference (scripted) - not model performance"
    )


def test_malformed_artifacts_are_not_silently_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "bad")
    monkeypatch.setattr(
        leaderboard_module,
        "load_aggregate_artifact",
        lambda _: (_ for _ in ()).throw(ResultArtifactError("bad artifact")),
    )

    result = load_leaderboard(root)

    assert result.ranked == ()
    assert result.issues and "malformed aggregate" in result.issues[0]


def test_unsupported_jurisdiction_is_reported_not_ranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path, "unknown")
    monkeypatch.setattr(
        leaderboard_module,
        "load_aggregate_artifact",
        lambda _: _aggregate(jurisdiction="unknown"),
    )

    assert load_leaderboard(root).issues


def test_invalid_filter_is_rejected() -> None:
    with pytest.raises(ValueError, match="jurisdiction"):
        leaderboard_module.Leaderboard((), (), (), ()).for_jurisdiction("global")


def test_single_run_is_visible_but_unranked(tmp_path, monkeypatch):
    root = _root(tmp_path, "experiment")
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: _aggregate(runs=1)
    )
    board = load_leaderboard(root)
    assert board.ranked == ()
    assert board.experiments[0].run_count == 1
    assert board.experiments[0].rank is None


@pytest.mark.parametrize(
    "difference",
    [
        {"world_version": "0.2"},
        {"max_steps": 10},
        {"judges": ("fictional/other-judge",)},
    ],
)
def test_different_conditions_receive_separate_cohorts(
    tmp_path, monkeypatch, difference
):
    root = _root(tmp_path, "a", "b")
    aggregates = iter((_aggregate(overall=0.9), _aggregate(overall=0.7, **difference)))
    monkeypatch.setattr(
        leaderboard_module, "load_aggregate_artifact", lambda _: next(aggregates)
    )
    rows = load_leaderboard(root).ranked
    assert [row.rank for row in rows] == [1, 1]
    assert rows[0].cohort != rows[1].cohort
