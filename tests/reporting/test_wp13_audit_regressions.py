"""Regression probes for the WP-13 audit integrity boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.digest import logical_state_digest
from mirrorfirm.core.models import (
    Action,
    CriterionResult,
    EvaluationResult,
    LayerScores,
    Usage,
)
from mirrorfirm.reporting.artifacts import (
    ResultArtifactError,
    build_aggregate_artifact,
    canonical_configuration_hash,
    load_run_artifact,
    write_run_artifact,
)
from mirrorfirm.reporting.report import write_comparison_report, write_scorecard
from mirrorfirm.reporting.sweep import SweepConfig, SweepError, preflight_sweep

_CREDENTIAL_CANARY = "CANARY_" + "PRIVATE_KEY"


def _result(model: str = "fictional/model") -> EvaluationResult:
    return EvaluationResult(
        episode_id="epi-report-fictional",
        run_id="run-r000001",
        model=model,
        critical_failures=[],
        criterion_results=[
            CriterionResult(
                criterion_id="complete",
                passed=True,
                score=1.0,
                detail="passed",
                evidence_refs=[],
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
        overall=1.0,
        all_pass=True,
        usage=Usage(
            steps=1,
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            latency_s=0.0,
            world_days_elapsed=0.0,
        ),
    )


def _persisted_run(
    tmp_path: Path,
    *,
    model: str = "fictional/model",
    run_id: str = "run-r000001",
    seed: int | None = None,
) -> tuple[Path, EvaluationResult]:
    directory = tmp_path / run_id
    directory.mkdir(parents=True)
    result = _result(model).model_copy(update={"run_id": run_id})
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
        episode_id=result.episode_id,
        model=model,
        world_id="wld-fictional",
        world_version="0.1.0",
        configuration={"temperature": 0, "private_key": f"{_CREDENTIAL_CANARY}_71d"},
        judge_models=[],
        run_id=result.run_id,
        initial_snapshot=initial,
        final_snapshot=final,
        agent_result={"finish_summary": completion},
        evaluation=result,
        seed=seed,
    )
    return directory, result


def test_run_configuration_and_snapshots_are_integrity_covered(tmp_path: Path) -> None:
    """A completed run cannot be altered or have either snapshot removed."""

    directory, _ = _persisted_run(tmp_path)
    run_json = directory / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))
    payload["configuration"] = {"seed": 999}
    payload["configuration_hash"] = "forged"
    run_json.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResultArtifactError, match="configuration"):
        load_run_artifact(directory)

    directory, _ = _persisted_run(tmp_path / "missing")
    (directory / "initial.db").unlink()
    with pytest.raises(ResultArtifactError, match="snapshot"):
        load_run_artifact(directory)

    directory, _ = _persisted_run(tmp_path / "modified")
    (directory / "final.db").write_bytes(b"substituted snapshot")
    with pytest.raises(ResultArtifactError, match="snapshot hash"):
        load_run_artifact(directory)

    directory, _ = _persisted_run(tmp_path / "digest")
    run_json = directory / "run.json"
    payload = json.loads(run_json.read_text(encoding="utf-8"))
    payload["final_snapshot"]["state_digest"] = "0" * 64
    # The stale identity is deliberately updated here: the logical DB digest itself
    # must still be independently checked, not only protected by envelope hashing.
    from mirrorfirm.reporting.artifacts import SnapshotExecutionBinding, _artifact_id

    payload["artifact_id"] = _artifact_id(
        payload["episode_id"],
        payload["run_id"],
        payload["model"],
        payload["configuration_hash"],
        type(load_run_artifact(directory)[0].initial_snapshot).model_validate(
            payload["initial_snapshot"]
        ),
        type(load_run_artifact(directory)[0].final_snapshot).model_validate(
            payload["final_snapshot"]
        ),
        SnapshotExecutionBinding.model_validate(payload["snapshot_binding"]),
        payload["sweep_id"],
        payload["sweep_run_index"],
        payload["fresh_world_id"],
        payload["score_sha256"],
        payload["transcript_sha256"],
        payload["agent_result"],
        payload["status"],
        payload["error"],
    )
    run_json.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ResultArtifactError, match="logical digest"):
        load_run_artifact(directory)

    directory, _ = _persisted_run(tmp_path / "linked")
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes((directory / "initial.db").read_bytes())
    (directory / "initial.db").unlink()
    (directory / "initial.db").symlink_to(replacement)
    with pytest.raises(ResultArtifactError, match="snapshot"):
        load_run_artifact(directory)


def test_aggregate_rejects_duplicate_run_identity(tmp_path: Path) -> None:
    """The same persisted run cannot inflate an aggregate twice."""

    directory, _ = _persisted_run(tmp_path)
    loaded = load_run_artifact(directory)
    with pytest.raises(ResultArtifactError, match="duplicate"):
        build_aggregate_artifact([loaded, loaded])

    copied = tmp_path / "copy"
    copied.mkdir()
    for child in directory.iterdir():
        (copied / child.name).write_bytes(child.read_bytes())
    with pytest.raises(ResultArtifactError, match="duplicate"):
        build_aggregate_artifact([loaded, load_run_artifact(copied)])


def test_comparison_rejects_different_material_configuration(tmp_path: Path) -> None:
    """Different seeds are not silently presented as one comparable baseline."""

    first, _ = _persisted_run(tmp_path / "one", seed=1)
    second, _ = _persisted_run(tmp_path / "two", run_id="run-r000002", seed=2)
    aggregate = build_aggregate_artifact([load_run_artifact(first)])
    incompatible = build_aggregate_artifact([load_run_artifact(second)])
    with pytest.raises(ResultArtifactError, match="configuration"):
        write_comparison_report([aggregate, incompatible], tmp_path / "compare.html")


def test_reference_identity_is_not_inferred_from_model_display_text(
    tmp_path: Path,
) -> None:
    """A reference-looking model identifier never creates a native aggregate."""

    with pytest.raises(ResultArtifactError, match="reference"):
        _persisted_run(tmp_path, model="reference-scripted")


def test_report_rejects_symlinked_output_parent(tmp_path: Path) -> None:
    """Report output must not escape through a symlinked ancestor."""

    directory, _ = _persisted_run(tmp_path / "source")
    aggregate = build_aggregate_artifact([load_run_artifact(directory)])
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = tmp_path / "requested"
    requested.mkdir()
    (requested / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ResultArtifactError, match="unsafe|symlink"):
        write_scorecard([aggregate], requested / "reports" / "escaped.html")
    assert not (outside / "escaped.html").exists()


def test_configuration_never_persists_private_key_and_hashes_public_fields_only(
    tmp_path: Path,
) -> None:
    """Credential-shaped inputs cannot become persisted reproducibility metadata."""

    directory, _ = _persisted_run(tmp_path)
    payload = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    assert "private_key" not in payload["configuration"]
    assert canonical_configuration_hash(
        {"seed": 1, "private-key": "first", "provider_credentials": {"token": "x"}}
    ) == canonical_configuration_hash(
        {"seed": 1, "private_key": "second", "provider_credentials": {"token": "y"}}
    )
    assert canonical_configuration_hash({"seed": 1}) != canonical_configuration_hash(
        {"seed": 2}
    )


def test_reference_scripted_cannot_be_a_model_baseline() -> None:
    """Scripted references are controls, never five-run model baselines."""

    config = SweepConfig(
        episodes=("epi-uk-01",),
        models=("reference-scripted",),
        runs=5,
        baseline=True,
        output_dir="results/audit-reference-baseline",
    )
    with pytest.raises(SweepError, match="reference"):
        preflight_sweep(config, require_credentials=False)
