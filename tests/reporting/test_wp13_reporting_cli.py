"""WP-13 regression coverage for persisted result artifacts, CLI, and scorecards."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mirrorfirm.cli import main as cli_main
from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Action,
    CriterionResult,
    EvaluationResult,
    LayerScores,
    StateSnapshot,
    Usage,
)
from mirrorfirm.reporting.artifacts import (
    ResultArtifactError,
    build_aggregate_artifact,
    canonical_configuration_hash,
    load_aggregate_artifact,
    load_run_artifact,
    write_aggregate_artifact,
    write_external_apex_artifact,
    write_run_artifact,
)
from mirrorfirm.reporting.report import (
    write_comparison_report,
    write_external_apex_report,
    write_scorecard,
)
from mirrorfirm.reporting.sweep import (
    SweepConfig,
    SweepError,
    SweepRunPlan,
    preflight_sweep,
    run_sweep,
)


def _result(
    *, model: str = "fictional/model", failed: bool = False
) -> EvaluationResult:
    return EvaluationResult(
        episode_id="epi-report-fictional",
        run_id="run-r000001",
        model=model,
        critical_failures=[],
        criterion_results=[
            CriterionResult(
                criterion_id="hostile" if failed else "complete",
                passed=not failed,
                score=0.0 if failed else 1.0,
                detail="<script>untrusted model content</script>"
                if failed
                else "passed",
                evidence_refs=["doc-fictional"],
            )
        ],
        scores=LayerScores(
            task_completion=1.0,
            accounting=1.0,
            state=1.0,
            safety=1.0,
            provenance=1.0,
            communication=1.0,
            efficiency=1.0,
        ),
        overall=0.0 if failed else 1.0,
        all_pass=not failed,
        usage=Usage(
            steps=3,
            tokens_in=5,
            tokens_out=7,
            cost_usd=0.01,
            latency_s=0.2,
            world_days_elapsed=0.0,
        ),
    )


def _persisted_run(
    tmp_path: Path, *, model: str = "fictional/model", failed: bool = False
) -> tuple[Path, EvaluationResult]:
    directory = tmp_path / "run-r000001"
    directory.mkdir(parents=True)
    result = _result(model=model, failed=failed)
    completion = {
        "summary": "Fictional completion.",
        "deliverable_refs": [],
        "unresolved_items": [],
        "final_snapshot_id": f"snp-{result.run_id}",
    }
    now = datetime.now(UTC)
    with WorldStore.create(directory / "world.db") as store:
        initial = store.snapshot(
            directory / "initial.db",
            snapshot_id=f"snp-{result.run_id}-initial",
            episode_run_id=result.run_id,
            phase="initial",
        )
        store.save(
            Action(
                id=f"act-{result.run_id}-finish",
                step=1,
                actor="per-fictional",
                tool="finish_episode",
                input_digest=logical_state_digest({}),
                output_digest=logical_state_digest(completion),
                input_payload={},
                output_payload=completion,
                engagement_id="eng-fictional",
                world_time_before=now,
                world_time_after=now,
                mutations=[],
            )
        )
        final = store.snapshot(
            directory / "final.db",
            snapshot_id=f"snp-{result.run_id}",
            episode_run_id=result.run_id,
            phase="final",
        )
    scripted = model == "reference (scripted) - not model performance"
    write_run_artifact(
        run_directory=directory,
        episode_id=result.episode_id,
        model=model,
        world_id="wld-fictional",
        world_version="0.1.0",
        configuration={
            "temperature": 0,
            "model": "reference-scripted" if scripted else model,
            "api_key": "never-persist",
        },
        judge_models=[],
        run_id=result.run_id,
        initial_snapshot=initial,
        final_snapshot=final,
        agent_result={
            "tool_metrics": {"tool_calls": 3},
            "finish_summary": completion,
        },
        evaluation=result,
        run_kind="scripted_reference" if scripted else "native_model",
    )
    return directory, result


def test_persisted_e17_artifacts_are_atomic_canonical_and_secret_free(
    tmp_path: Path,
) -> None:
    """A completed run is a verified E.17 score plus an atomic provenance envelope."""

    directory, result = _persisted_run(tmp_path)
    artifact, restored = load_run_artifact(directory)

    assert restored == result
    assert "api_key" not in artifact.configuration.model_dump(mode="json")
    assert "never-persist" not in (directory / "run.json").read_text(encoding="utf-8")
    assert canonical_configuration_hash(
        {"max_tokens": 100, "api_key": "first"}
    ) != canonical_configuration_hash({"max_tokens": 200, "api_key": "second"})
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_run_artifact(
            run_directory=directory,
            episode_id=result.episode_id,
            model=result.model,
            world_id="wld-fictional",
            world_version="0.1.0",
            configuration={},
            judge_models=[],
            run_id=result.run_id,
            initial_snapshot=StateSnapshot(
                id="snp-other-initial",
                episode_run_id=result.run_id,
                phase="initial",
                db_path=str(directory / "initial.db"),
                state_digest="1" * 64,
            ),
            final_snapshot=StateSnapshot(
                id="snp-other-final",
                episode_run_id=result.run_id,
                phase="final",
                db_path=str(directory / "final.db"),
                state_digest="2" * 64,
            ),
            agent_result={},
            evaluation=result,
        )


def test_incomplete_runs_cannot_be_evaluated_or_reported(tmp_path: Path) -> None:
    """An interrupted run remains explicitly incomplete and has no E.17 score."""

    directory = tmp_path / "incomplete"
    directory.mkdir()
    for name in ("initial.db", "final.db"):
        (directory / name).write_bytes(b"fictional snapshot")
    snapshot = StateSnapshot(
        id="snp-incomplete",
        episode_run_id="run-incomplete",
        phase="initial",
        db_path=str(directory / "initial.db"),
        state_digest="5" * 64,
    )
    final = snapshot.model_copy(
        update={
            "id": "snp-incomplete-final",
            "phase": "final",
            "db_path": str(directory / "final.db"),
        }
    )
    write_run_artifact(
        run_directory=directory,
        episode_id="epi-report-fictional",
        model="fictional/model",
        world_id="wld-fictional",
        world_version="0.1.0",
        configuration={},
        judge_models=[],
        run_id="run-incomplete",
        initial_snapshot=snapshot,
        final_snapshot=final,
        agent_result={},
        evaluation=None,
        error="provider API_KEY failure should never persist",
    )
    assert "API_KEY" not in (directory / "run.json").read_text(encoding="utf-8")
    with pytest.raises(ResultArtifactError, match="incomplete"):
        load_run_artifact(directory)


def test_report_escapes_hostile_content_and_separates_references(
    tmp_path: Path,
) -> None:
    """All requested scorecard columns render offline and untrusted text is escaped."""

    native_dir, native_result = _persisted_run(tmp_path / "native", failed=True)
    native = build_aggregate_artifact([load_run_artifact(native_dir)])
    reference_dir, _ = _persisted_run(
        tmp_path / "reference", model="reference (scripted) - not model performance"
    )
    reference = build_aggregate_artifact([load_run_artifact(reference_dir)])

    first_html, first_json = write_scorecard(
        [native, reference], tmp_path / "first.html"
    )
    second_html, second_json = write_scorecard(
        [native, reference], tmp_path / "second.html"
    )
    document = first_html.read_text(encoding="utf-8")
    assert first_html.read_bytes() == second_html.read_bytes()
    assert first_json.read_bytes() == second_json.read_bytes()
    for heading in (
        "Task",
        "Accounting",
        "State",
        "Safety",
        "Provenance",
        "Communication",
        "Efficiency",
        "Pass@1",
        "Pass@5",
        "Pass^3 (headline)",
        "Pass^5",
        "Input/output tokens",
        "World-days",
        "Configuration",
    ):
        assert heading in document
    assert "&lt;script&gt;untrusted model content&lt;/script&gt;" in document
    assert "Reference trajectories — not model performance" in document
    assert native_result.overall == 0.0


def test_comparison_rejects_reference_rows_and_requires_native_compatibility(
    tmp_path: Path,
) -> None:
    """Reference rows cannot masquerade as model-performance comparison rows."""

    native_dir, _ = _persisted_run(tmp_path / "native")
    native = build_aggregate_artifact([load_run_artifact(native_dir)])
    reference_dir, _ = _persisted_run(
        tmp_path / "reference", model="reference (scripted) - not model performance"
    )
    reference = build_aggregate_artifact([load_run_artifact(reference_dir)])

    with pytest.raises(ResultArtifactError, match="reference"):
        write_comparison_report([native, reference], tmp_path / "comparison.html")
    incompatible = native.model_copy(update={"world_version": "different"})
    with pytest.raises(ResultArtifactError, match="world pins"):
        write_comparison_report([native, incompatible], tmp_path / "pins.html")
    html_path, json_path = write_comparison_report([native], tmp_path / "native.html")
    assert html_path.is_file() and json_path.is_file()


def test_aggregate_loader_rejects_incomplete_or_inconsistent_rows(
    tmp_path: Path,
) -> None:
    """Report/compare never accept malformed aggregate data as verified results."""

    directory, _ = _persisted_run(tmp_path)
    aggregate = build_aggregate_artifact([load_run_artifact(directory)])
    destination = write_aggregate_artifact(aggregate, tmp_path / "aggregate.json")
    raw = json.loads(destination.read_text(encoding="utf-8"))
    raw["run_count"] = 2
    destination.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ResultArtifactError, match="inconsistent"):
        load_aggregate_artifact(destination)


def test_external_apex_result_has_a_separate_labelled_report(tmp_path: Path) -> None:
    """Public-reference APEX output cannot be rendered as a native result row."""

    from mirrorfirm.packs.apex_accounting.models import StaticTaskEvaluation

    evaluation = StaticTaskEvaluation(
        task_id="task-fictional",
        source_revision="a" * 40,
        report_label=(
            "APEX-Accounting public dev set via Mirror Firm importer - external; "
            "not comparable to the official APEX leaderboard"
        ),
        contamination="public-reference-answers",
        criterion_results=(),
        score=0.0,
        passed=False,
    )
    artifact_path = write_external_apex_artifact(evaluation, tmp_path / "apex.json")
    from mirrorfirm.reporting.artifacts import load_external_apex_artifact

    html_path, _ = write_external_apex_report(
        load_external_apex_artifact(artifact_path), tmp_path / "apex-report.html"
    )
    report = html_path.read_text(encoding="utf-8")
    assert evaluation.source_revision in report
    assert evaluation.report_label in report
    assert "excluded from all headline aggregation" in report


def test_cli_discovers_authored_resources_and_runs_reference_to_persisted_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI list/run/evaluate/report consume the normal runner and persisted results."""

    assert cli_main(["list", "episodes", "--json"]) == 0
    assert '"episode_id":"epi-us-03"' in capsys.readouterr().out
    assert cli_main(["validate", "--world", "uk-wyrley-brook", "--json"]) == 0
    assert '"passed":true' in capsys.readouterr().out
    assert cli_main(["validate", "--world", "unknown-world", "--json"]) == 2
    assert "unknown world" in capsys.readouterr().err
    assert (
        cli_main(
            [
                "run",
                "--episode",
                "epi-uk-02",
                "--model",
                "reference-scripted",
                "--results-root",
                str(tmp_path),
                "--run-id",
                "run-r000001",
                "--json",
            ]
        )
        == 0
    )
    run_payload = capsys.readouterr().out
    assert '"all_pass":true' in run_payload
    run_path = tmp_path / "runs" / "epi-uk-02" / "reference-scripted" / "run-r000001"
    assert cli_main(["evaluate", str(run_path), "--json"]) == 0
    assert '"all_pass":true' in capsys.readouterr().out
    report = tmp_path / "scorecard.html"
    assert cli_main(["report", str(tmp_path), "--output", str(report), "--json"]) == 0
    assert report.is_file() and report.with_suffix(".json").is_file()
    assert '"html":' in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ("--help",),
        ("list", "--help"),
        ("validate", "--help"),
        ("run", "--help"),
        ("evaluate", "--help"),
        ("report", "--help"),
        ("compare", "--help"),
        ("sweep", "--help"),
        ("packs", "--help"),
        ("packs", "install", "--help"),
        ("packs", "show-gold", "--help"),
    ],
)
def test_cli_help_is_available_for_every_public_command(
    argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    """All public commands retain argparse's stable zero-exit help surface."""

    with pytest.raises(SystemExit) as error:
        cli_main(list(argv))
    assert error.value.code == 0
    assert "usage: mirror-firm" in capsys.readouterr().out


def test_cli_report_compare_and_errors_are_deterministic_and_secret_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI consumes verified results without rerunning and redacts failure surfaces."""

    directory, _ = _persisted_run(tmp_path / "source")
    aggregate = build_aggregate_artifact([load_run_artifact(directory)])
    aggregate_path = write_aggregate_artifact(
        aggregate, tmp_path / "results" / "aggregate.json"
    )
    report = tmp_path / "report.html"
    comparison = tmp_path / "comparison.html"
    assert cli_main(["report", str(aggregate_path), "--output", str(report)]) == 0
    assert cli_main(["compare", str(aggregate_path), "--output", str(comparison)]) == 0
    assert report.is_file() and comparison.is_file()
    comparison_html = comparison.read_text(encoding="utf-8")
    for heading in (
        "CF rate",
        "CF identifiers",
        "Task",
        "Accounting",
        "Pass^3",
        "World-days",
    ):
        assert heading in comparison_html

    monkeypatch.setenv("MIRRORFIRM_TEST_SECRET", "do-not-print")
    assert cli_main(["run", "--episode", "epi-uk-01", "--model", "bad/model"]) == 2
    assert "do-not-print" not in capsys.readouterr().err


def test_cli_sweep_runs_one_fresh_scripted_reference_and_persists_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public sweep command uses the normal fresh-world runner and artifact gate."""

    configuration = tmp_path / "sweep.json"
    output = tmp_path / "sweep-results"
    configuration.write_text(
        json.dumps(
            {
                "episodes": ["epi-uk-02"],
                "models": ["reference-scripted"],
                "runs": 1,
                "output_dir": str(output),
                "seed": 17,
            }
        ),
        encoding="utf-8",
    )
    assert cli_main(["sweep", str(configuration), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"] == 1
    manifest = json.loads((output / "sweep-manifest.json").read_text(encoding="utf-8"))
    assert manifest["entries"] == [
        {
            "episode_id": "epi-uk-02",
            "error": None,
            "model": "reference-scripted",
            "run_index": 1,
            "run_path": str(
                output / "runs" / "epi-uk-02" / "reference-scripted" / "run-r000001"
            ),
            "status": "complete",
        }
    ]


def test_sweep_preflight_rejects_bad_provider_before_any_live_execution() -> None:
    """Unknown provider configuration fails during preflight, before a model can run."""

    config = SweepConfig(
        episodes=("epi-uk-01",),
        models=("unknown/future",),
        runs=1,
        output_dir="results/wp13-test",
    )
    with pytest.raises(SweepError, match="known provider"):
        preflight_sweep(config)
    assert canonical_configuration_hash(
        {"api_key": "x", "seed": 1}
    ) == canonical_configuration_hash({"api_key": "different", "seed": 1})


def test_sweep_preflight_requires_five_baseline_runs_and_records_partial_failure(
    tmp_path: Path,
) -> None:
    """Sequential sweeps preflight first and preserve a failed model/run honestly."""

    invalid = SweepConfig(
        episodes=("epi-uk-01",),
        models=("reference-scripted",),
        runs=1,
        baseline=True,
        output_dir=str(tmp_path / "invalid"),
    )
    with pytest.raises(SweepError, match="exactly five"):
        preflight_sweep(invalid)

    config = SweepConfig(
        episodes=("epi-uk-01",),
        models=("reference-scripted",),
        runs=1,
        output_dir=str(tmp_path / "sweep"),
        seed=42,
    )
    called: list[tuple[str, str, int]] = []

    def failed_run(
        episode: object,
        model: str,
        run_index: int,
        _config: object,
        _hash: str,
    ) -> Path:
        episode_id = getattr(episode, "episode_id")
        assert isinstance(episode_id, str)
        called.append((episode_id, model, run_index))
        raise RuntimeError("fictional provider failure")

    manifest, path = run_sweep(config, failed_run)
    assert called == [("epi-uk-01", "reference-scripted", 1)]
    assert manifest.entries[0].status == "failed"
    assert path.is_file()

    missing_artifact = config.model_copy(
        update={"output_dir": str(tmp_path / "missing-artifact")}
    )

    def dishonest_success(
        _episode: object,
        _model: str,
        _run_index: int,
        config_value: object,
        _hash: str,
    ) -> Path:
        output_dir = getattr(config_value, "output_dir")
        assert isinstance(output_dir, str)
        return Path(output_dir) / "does-not-exist"

    missing_manifest, _ = run_sweep(missing_artifact, dishonest_success)
    assert missing_manifest.entries[0].status == "failed"
    assert "unsafe" in (missing_manifest.entries[0].error or "")

    valid_config = config.model_copy(
        update={"output_dir": str(tmp_path / "verified-sweep")}
    )

    def verified_run(
        episode: object,
        model: str,
        run_index: int,
        config_value: SweepConfig,
        plan: SweepRunPlan,
    ) -> Path:
        episode_id = getattr(episode, "episode_id")
        world_id = getattr(episode, "world_id")
        world_version = getattr(episode, "world_version")
        assert isinstance(episode_id, str)
        assert isinstance(world_id, str)
        assert isinstance(world_version, str)
        run_id = f"run-r{run_index:06d}"
        directory = Path(config_value.output_dir) / "runs" / run_id
        directory.mkdir(parents=True)
        result = _result(
            model="reference (scripted) - not model performance"
        ).model_copy(update={"episode_id": episode_id, "run_id": run_id})
        completion = {
            "summary": "Fictional completion.",
            "deliverable_refs": [],
            "unresolved_items": [],
            "final_snapshot_id": f"snp-{run_id}",
        }
        now = datetime.now(UTC)
        with WorldStore.create(directory / "world.db") as store:
            initial = store.snapshot(
                directory / "initial.db",
                snapshot_id=f"snp-{run_id}-initial",
                episode_run_id=run_id,
                phase="initial",
            )
            store.save(
                Action(
                    id=f"act-{run_id}-finish",
                    step=1,
                    actor="per-fictional",
                    tool="finish_episode",
                    input_digest=logical_state_digest({}),
                    output_digest=logical_state_digest(completion),
                    input_payload={},
                    output_payload=completion,
                    engagement_id="eng-fictional",
                    world_time_before=now,
                    world_time_after=now,
                    mutations=[],
                )
            )
            final = store.snapshot(
                directory / "final.db",
                snapshot_id=f"snp-{run_id}",
                episode_run_id=run_id,
                phase="final",
            )
        write_run_artifact(
            run_directory=directory,
            episode_id=episode_id,
            model=result.model,
            world_id=world_id,
            world_version=world_version,
            configuration={"model": model, "baseline": config_value.baseline},
            configuration_hash=plan.configuration_hash,
            judge_models=[],
            run_id=run_id,
            initial_snapshot=initial,
            final_snapshot=final,
            agent_result={"finish_summary": completion},
            evaluation=result,
            seed=config_value.seed,
            run_kind="scripted_reference",
            jurisdiction=getattr(episode, "jurisdiction"),
            sweep_id=plan.sweep_id,
            sweep_run_index=plan.run_index,
            fresh_world_id=plan.fresh_world_id,
        )
        return directory

    verified_manifest, _ = run_sweep(valid_config, verified_run)
    assert verified_manifest.entries[0].status == "complete"
