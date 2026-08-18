"""Release-boundary regressions reproduced from the WP-13 independent audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import mirrorfirm.cli as cli
import mirrorfirm.reporting.artifacts as artifact_module
from mirrorfirm.core.models import (
    CriterionResult,
    EpisodeManifest,
    EvaluationResult,
    LayerScores,
    Usage,
)
from mirrorfirm.episodes.manifests import load_episode_manifest
from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)
from mirrorfirm.harness.agent_loop import run_agent
from mirrorfirm.harness.episode_runner import EpisodeRunner
from mirrorfirm.reporting.artifacts import (
    ResultArtifactError,
    SnapshotArtifact,
    SnapshotExecutionBinding,
    _artifact_id,
    build_aggregate_artifact,
    load_run_artifact,
    open_secure_execution_directory,
    write_json_atomic,
    write_run_artifact,
    write_text_atomic,
)
from mirrorfirm.reporting.report import write_scorecard
from mirrorfirm.reporting.sweep import SweepConfig, run_sweep
from mirrorfirm.security import (
    SanitizationError,
    sanitize_for_persistence,
    sanitize_text,
)
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"
EPISODE_PATH = ROOT / "episodes" / "uk" / "ep-uk-01.yaml"
FINISH_ARGUMENTS = json.dumps(
    {
        "summary": "The fictional episode is complete.",
        "deliverable_refs": [],
        "unresolved_items": [],
    }
)


class _FinishAdapter(ModelAdapter):
    """One deterministic terminal call, with a visible call counter."""

    def __init__(self, *, text: str = "Finished.") -> None:
        super().__init__("scripted-audit-provider")
        self.calls = 0
        self._text = text

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        del messages, tools
        self.calls += 1
        return ModelResponse(
            message={"role": "assistant", "content": self._text},
            text=self._text,
            tool_calls=[ToolCall("finish", "finish_episode", FINISH_ARGUMENTS)],
            input_tokens=1,
            output_tokens=1,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


class _RaceFinishAdapter(_FinishAdapter):
    """Trigger a controlled output-directory substitution immediately before finish."""

    def __init__(self, attack: Callable[[], None]) -> None:
        super().__init__()
        self._attack = attack
        self._attacked = False

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        if not self._attacked:
            self._attacked = True
            self._attack()
        return super().chat(messages, tools)


class _SerializedSecretAdapter(_FinishAdapter):
    """A provider response carrying credential-shaped values in every hostile form."""

    def __init__(self) -> None:
        super().__init__(text='{"token":"ARG_TOKEN_CANARY"}')

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        response = super().chat(messages, tools)
        return ModelResponse(
            message={
                "role": "assistant",
                "content": response.text,
                "metadata": {"accessToken": "NESTED_TOKEN_CANARY"},
            },
            text=response.text,
            tool_calls=response.tool_calls,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )


class _NoopExecutor:
    """A finished-looking test executor for raw transcript persistence coverage."""

    def execute(self, name: str, arguments: str) -> str:
        raise AssertionError(f"unexpected tool {name}({arguments})")

    def reserve_tool_calls(self, count: int) -> bool:
        return count == 0

    def get_metrics(self) -> dict[str, object]:
        return {}

    @property
    def is_finished(self) -> bool:
        return False

    @property
    def budget_exhausted(self) -> bool:
        return False

    @property
    def finish_summary(self) -> dict[str, object] | None:
        return None


class _SecretResultExecutor(_NoopExecutor):
    """Return a secret-bearing serialized tool response for transcript coverage."""

    def execute(self, name: str, arguments: str) -> str:
        assert name == "get_current_time"
        assert arguments == "{}"
        return '{"token":"RESULT_TOKEN_CANARY"}'


class _OneToolThenStopAdapter(ModelAdapter):
    """Emit exactly one tool result, then a normal no-call completion turn."""

    def __init__(self) -> None:
        super().__init__("scripted-secret-result")
        self._turn = 0

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        del messages, tools
        self._turn += 1
        if self._turn == 1:
            return ModelResponse(
                message={"role": "assistant", "content": "Use the safe tool."},
                text="Use the safe tool.",
                tool_calls=[ToolCall("one", "get_current_time", "{}")],
                input_tokens=1,
                output_tokens=1,
            )
        return ModelResponse(
            message={"role": "assistant", "content": "Done."},
            text="Done.",
            tool_calls=[],
            input_tokens=1,
            output_tokens=1,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


def _episode() -> EpisodeManifest:
    return load_episode_manifest(EPISODE_PATH)


def _evaluation(episode_id: str, run_id: str, model: str) -> EvaluationResult:
    return EvaluationResult(
        episode_id=episode_id,
        run_id=run_id,
        model=model,
        critical_failures=[],
        criterion_results=[
            CriterionResult(
                criterion_id="fictional-complete",
                passed=True,
                score=1.0,
                detail="fictional regression result",
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


def _completed_persisted_run(
    tmp_path: Path,
    *,
    model: str = "fictional/model",
    run_id: str = "run-r000001",
    seed: int | None = None,
    baseline: bool = False,
    configuration_hash: str | None = None,
    sweep_id: str | None = None,
    sweep_run_index: int | None = None,
    fresh_world_id: str | None = None,
) -> Path:
    episode = _episode()
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    result = EpisodeRunner(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    ).run(_FinishAdapter(), run_id=run_id)
    evaluation = _evaluation(episode.episode_id, result.run_id, model)
    directory = tmp_path / "results" / run_id
    write_run_artifact(
        run_directory=directory,
        episode_id=episode.episode_id,
        model=model,
        world_id=episode.world_id,
        world_version=episode.world_version,
        configuration={"model": model, "baseline": baseline},
        configuration_hash=configuration_hash,
        judge_models=[],
        run_id=run_id,
        initial_snapshot=result.initial_snapshot,
        final_snapshot=result.final_snapshot,
        agent_result=result.agent_result,
        evaluation=evaluation,
        seed=seed,
        jurisdiction=episode.jurisdiction,
        sweep_id=sweep_id,
        sweep_run_index=sweep_run_index,
        fresh_world_id=fresh_world_id,
    )
    return directory


def _recompute_artifact_identity(payload: dict[str, object]) -> str:
    return _artifact_id(
        str(payload["episode_id"]),
        str(payload["run_id"]),
        str(payload["model"]),
        str(payload["configuration_hash"]),
        SnapshotArtifact.model_validate(payload["initial_snapshot"]),
        SnapshotArtifact.model_validate(payload["final_snapshot"]),
        SnapshotExecutionBinding.model_validate(payload["snapshot_binding"])
        if payload["snapshot_binding"] is not None
        else None,
        payload["sweep_id"] if isinstance(payload["sweep_id"], str) else None,
        (
            payload["sweep_run_index"]
            if isinstance(payload["sweep_run_index"], int)
            else None
        ),
        (
            payload["fresh_world_id"]
            if isinstance(payload["fresh_world_id"], str)
            else None
        ),
        payload["score_sha256"] if isinstance(payload["score_sha256"], str) else None,
        (
            payload["transcript_sha256"]
            if isinstance(payload["transcript_sha256"], str)
            else None
        ),
        payload["agent_result"],  # type: ignore[arg-type]
        str(payload["status"]),
        payload["error"] if isinstance(payload["error"], str) else None,
    )


def test_complete_snapshot_envelopes_cannot_be_swapped_or_reidentified(
    tmp_path: Path,
) -> None:
    """Run loading must bind snapshot roles to execution, not envelope labels alone."""

    directory = _completed_persisted_run(tmp_path)
    run_json = directory / "run.json"
    swapped = json.loads(run_json.read_text(encoding="utf-8"))
    swapped["initial_snapshot"], swapped["final_snapshot"] = (
        swapped["final_snapshot"],
        swapped["initial_snapshot"],
    )
    swapped["artifact_id"] = _recompute_artifact_identity(swapped)
    run_json.write_text(json.dumps(swapped), encoding="utf-8")
    with pytest.raises(
        ResultArtifactError, match="snapshot.*(role|execution)|terminal"
    ):
        load_run_artifact(directory)


def test_snapshot_identifier_mutation_cannot_be_accepted_as_execution_provenance(
    tmp_path: Path,
) -> None:
    """A changed snapshot ID cannot be made valid merely by recomputing an envelope."""

    directory = _completed_persisted_run(tmp_path)
    run_json = directory / "run.json"
    mutated = json.loads(run_json.read_text(encoding="utf-8"))
    mutated["final_snapshot"]["id"] = "snp-forged-terminal"
    mutated["artifact_id"] = _recompute_artifact_identity(mutated)
    run_json.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(
        ResultArtifactError, match="snapshot.*(binding|execution)|terminal"
    ):
        load_run_artifact(directory)


def test_runner_rejects_symlinked_results_root_before_provider_or_world_write(
    tmp_path: Path,
) -> None:
    """An untrusted results root cannot redirect runner-side intermediate artifacts."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    requested = tmp_path / "requested-results"
    requested.symlink_to(outside, target_is_directory=True)
    adapter = _FinishAdapter()
    with pytest.raises(ValueError, match="unsafe"):
        EpisodeRunner(
            _episode(),
            compiled.database_path,
            world_root=WORLD,
            results_root=requested,
        ).run(adapter, run_id="run-r000001")
    assert adapter.calls == 0
    assert list(outside.iterdir()) == []


def test_transcript_secret_is_redacted_before_persistence_and_hashing(
    tmp_path: Path,
) -> None:
    """Credential-like provider content cannot reach the canonical transcript bytes."""

    transcript = tmp_path / "transcript.jsonl"
    run_agent(
        _FinishAdapter(text="CANARY_PRIVATE_KEY_6f4af"),
        "system",
        "prompt",
        _NoopExecutor(),
        [],
        transcript_path=str(transcript),
    )

    assert "CANARY_PRIVATE_KEY_6f4af" not in transcript.read_text(encoding="utf-8")


def test_transcript_sanitizer_handles_nested_plaintext_pem_and_bearer_values(
    tmp_path: Path,
) -> None:
    """Every persisted provider surface uses the same recursive secret boundary."""

    nested = {
        "metadata": {
            "private-key": "CANARY_PRIVATE_KEY_6f4af",
            "authorization": "Bearer canary-token-value",
        },
        "text": "-----BEGIN PRIVATE KEY-----\nCANARY\n-----END PRIVATE KEY-----",
    }
    sanitized = sanitize_for_persistence(nested)
    rendered = json.dumps(sanitized, sort_keys=True)
    for secret in ("CANARY_PRIVATE_KEY_6f4af", "canary-token-value", "CANARY"):
        assert secret not in rendered
    with pytest.raises(SanitizationError, match="keys"):
        sanitize_for_persistence({1: "unsafe"})

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    run_agent(
        _FinishAdapter(text="CANARY_PRIVATE_KEY_first"),
        "system",
        "prompt",
        _NoopExecutor(),
        [],
        transcript_path=str(first),
    )
    run_agent(
        _FinishAdapter(text="CANARY_PRIVATE_KEY_second"),
        "system",
        "prompt",
        _NoopExecutor(),
        [],
        transcript_path=str(second),
    )
    assert first.read_bytes() == second.read_bytes()
    assert (
        hashlib.sha256(first.read_bytes()).hexdigest()
        == hashlib.sha256(second.read_bytes()).hexdigest()
    )


def test_runner_detects_post_validation_results_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path replacement after root opening cannot redirect later runner writes."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = EpisodeRunner(
        _episode(), compiled.database_path, world_root=WORLD, results_root=results
    )
    original_validate = runner._validate_world  # noqa: SLF001

    def replace_run_directory(store: object) -> None:
        original_validate(store)  # type: ignore[arg-type]
        run_directory = results / "run-r000001"
        relocated = tmp_path / "relocated-run"
        run_directory.replace(relocated)
        run_directory.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(runner, "_validate_world", replace_run_directory)
    adapter = _FinishAdapter()
    with pytest.raises(ValueError, match="replaced|unsafe"):
        runner.run(adapter, run_id="run-r000001")
    assert adapter.calls == 0
    assert list(outside.iterdir()) == []

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "redirect").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        EpisodeRunner(
            _episode(),
            compiled.database_path,
            world_root=WORLD,
            results_root=nested / "redirect" / "runs",
        )


def test_baseline_sweep_rejects_callbacks_with_mismatched_plan_provenance(
    tmp_path: Path,
) -> None:
    """A five-run baseline cannot accept callbacks that claim non-baseline provenance."""

    config = SweepConfig(
        episodes=("epi-uk-01",),
        models=("openai/fictional",),
        runs=5,
        baseline=True,
        output_dir=str(tmp_path / "sweep"),
        seed=7,
    )

    def callback(
        episode: EpisodeManifest,
        model: str,
        run_index: int,
        sweep: SweepConfig,
        _configuration_hash: str,
    ) -> Path:
        directory = Path(sweep.output_dir) / f"run-r{run_index:06d}"
        directory.mkdir(parents=True)
        # The current callback boundary accepts this intentionally different public
        # configuration and omits the sweep's canonical hash.
        return _completed_persisted_run(
            Path(sweep.output_dir) / f"callback-{run_index}",
            model=model,
            seed=sweep.seed,
        )

    manifest, _ = run_sweep(config, callback, require_credentials=False)
    assert all(entry.status == "failed" for entry in manifest.entries)
    assert all(
        "preflighted configuration" in (entry.error or "") for entry in manifest.entries
    )


def test_sweep_plan_requires_matching_hash_seed_model_and_fresh_worlds(
    tmp_path: Path,
) -> None:
    """Callback artifacts must match the exact immutable plan before aggregation."""

    config = SweepConfig(
        episodes=("epi-uk-01",),
        models=("openai/fictional",),
        runs=5,
        baseline=True,
        output_dir=str(tmp_path / "valid-sweep"),
        seed=11,
    )

    def matching_callback(
        _episode_value: EpisodeManifest,
        model: str,
        _run_index: int,
        sweep: SweepConfig,
        plan: object,
    ) -> Path:
        from mirrorfirm.reporting.sweep import SweepRunPlan

        assert isinstance(plan, SweepRunPlan)
        return _completed_persisted_run(
            Path(sweep.output_dir) / f"callback-{plan.run_index}",
            model=model,
            run_id=f"run-r{plan.run_index:06d}",
            seed=sweep.seed,
            baseline=sweep.baseline,
            configuration_hash=plan.configuration_hash,
            sweep_id=plan.sweep_id,
            sweep_run_index=plan.run_index,
            fresh_world_id=plan.fresh_world_id,
        )

    manifest, _ = run_sweep(config, matching_callback, require_credentials=False)
    assert [entry.status for entry in manifest.entries] == ["complete"] * 5

    invalid = config.model_copy(update={"output_dir": str(tmp_path / "invalid-sweep")})

    def wrong_seed_callback(
        _episode_value: EpisodeManifest,
        model: str,
        _run_index: int,
        sweep: SweepConfig,
        plan: object,
    ) -> Path:
        from mirrorfirm.reporting.sweep import SweepRunPlan

        assert isinstance(plan, SweepRunPlan)
        return _completed_persisted_run(
            Path(sweep.output_dir) / f"callback-{plan.run_index}",
            model=model,
            run_id=f"run-r{plan.run_index:06d}",
            seed=(sweep.seed or 0) + 1,
            baseline=sweep.baseline,
            sweep_id=plan.sweep_id,
            sweep_run_index=plan.run_index,
            fresh_world_id="reused-world",
        )

    rejected, _ = run_sweep(invalid, wrong_seed_callback, require_credentials=False)
    assert [entry.status for entry in rejected.entries] == ["failed"] * 5


@pytest.mark.parametrize("attack_kind", ["symlink", "directory", "unlink"])
def test_engine_snapshot_directory_replacement_cannot_escape_results_root(
    tmp_path: Path, attack_kind: str
) -> None:
    """A finish-time replacement must not redirect the engine's terminal snapshot."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    snapshot_dir = results / "run-r000001" / "engine-snapshots"
    relocated = tmp_path / "relocated-engine-snapshots"

    def attack() -> None:
        snapshot_dir.replace(relocated)
        if attack_kind == "symlink":
            snapshot_dir.symlink_to(outside, target_is_directory=True)
        elif attack_kind == "directory":
            snapshot_dir.mkdir()
        else:
            assert attack_kind == "unlink"

    runner = EpisodeRunner(
        _episode(), compiled.database_path, world_root=WORLD, results_root=results
    )
    adapter = _RaceFinishAdapter(attack)
    with pytest.raises(ResultArtifactError, match="snapshot|directory|unsafe|replaced"):
        runner.run(adapter, run_id="run-r000001")
    assert adapter.calls == 1
    assert list(outside.iterdir()) == []
    assert not (results / "run-r000001" / "run.json").exists()


def test_engine_snapshot_parent_replacement_cannot_complete_run(tmp_path: Path) -> None:
    """Replacing the run parent must fail before a terminal result can exist."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = results / "run-r000001"
    relocated = tmp_path / "relocated-run"

    def attack() -> None:
        run_dir.replace(relocated)
        run_dir.symlink_to(outside, target_is_directory=True)

    runner = EpisodeRunner(
        _episode(), compiled.database_path, world_root=WORLD, results_root=results
    )
    with pytest.raises(ResultArtifactError, match="directory|unsafe|replaced"):
        runner.run(_RaceFinishAdapter(attack), run_id="run-r000001")
    assert list(outside.iterdir()) == []
    assert not (results / "run-r000001" / "run.json").exists()


def test_cli_rejects_unsafe_results_root_before_validation_or_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsafe root is a syntax/security failure, not a late execution failure."""

    outside = tmp_path / "outside"
    outside.mkdir()
    requested = tmp_path / "requested"
    requested.symlink_to(outside, target_is_directory=True)
    calls = {
        "validate": 0,
        "compile": 0,
        "reference": 0,
        "provider": 0,
        "artifact": 0,
    }

    def validate(_: Path) -> SimpleNamespace:
        calls["validate"] += 1
        return SimpleNamespace(passed=True)

    def compile(_: Path, __: Path) -> object:
        calls["compile"] += 1
        raise AssertionError("compile must not run for an unsafe output root")

    monkeypatch.setattr(cli, "validate_world", validate)
    monkeypatch.setattr(cli, "compile_world", compile)
    monkeypatch.setattr(
        cli,
        "run_reference_episode",
        lambda *args, **kwargs: calls.__setitem__("reference", calls["reference"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "resolve_live_adapter",
        lambda *_: calls.__setitem__("provider", calls["provider"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "write_run_artifact",
        lambda **_: calls.__setitem__("artifact", calls["artifact"] + 1),
    )

    with pytest.raises(ResultArtifactError, match="unsafe"):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "openai/fictional",
            results_root=requested,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )
    assert calls == {
        "validate": 0,
        "compile": 0,
        "reference": 0,
        "provider": 0,
        "artifact": 0,
    }
    assert list(outside.iterdir()) == []


def test_cli_rejects_results_root_replacement_during_validation_before_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation cannot create a window to replace the retained results root."""

    results = tmp_path / "results"
    run_parent = results / "runs" / _episode().episode_id / "reference-scripted"
    outside = tmp_path / "outside"
    outside.mkdir()
    relocated = tmp_path / "relocated-results-root"
    calls = {
        "validate": 0,
        "compile": 0,
        "reference": 0,
        "provider": 0,
        "artifact": 0,
    }

    def validate(_: Path) -> SimpleNamespace:
        calls["validate"] += 1
        run_parent.replace(relocated)
        run_parent.symlink_to(outside, target_is_directory=True)
        return SimpleNamespace(passed=True)

    def compile(_: Path, __: Path) -> object:
        calls["compile"] += 1
        raise AssertionError("compile must not run after results-root replacement")

    monkeypatch.setattr(cli, "validate_world", validate)
    monkeypatch.setattr(cli, "compile_world", compile)
    monkeypatch.setattr(
        cli,
        "run_reference_episode",
        lambda *args, **kwargs: calls.__setitem__("reference", calls["reference"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "resolve_live_adapter",
        lambda *_: calls.__setitem__("provider", calls["provider"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "write_run_artifact",
        lambda **_: calls.__setitem__("artifact", calls["artifact"] + 1),
    )

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )
    assert calls == {
        "validate": 1,
        "compile": 0,
        "reference": 0,
        "provider": 0,
        "artifact": 0,
    }
    assert list(outside.iterdir()) == []
    assert not (results / "aggregates").exists()


def _replace_cli_results_root(root: Path, outside: Path, relocated: Path) -> None:
    """Replace one visible run parent without touching its retained descriptor."""

    root.replace(relocated)
    root.symlink_to(outside, target_is_directory=True)


def test_cli_rejects_results_root_replacement_during_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compilation cannot hand a substituted root to reference execution."""

    results = tmp_path / "results"
    root = results / "runs" / _episode().episode_id / "reference-scripted"
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = {"reference": 0, "artifact": 0}

    def compile_and_replace(source: Path, destination: Path) -> object:
        compiled = compile_world(source, destination)
        _replace_cli_results_root(root, outside, tmp_path / "relocated-after-compile")
        return compiled

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "compile_world", compile_and_replace)
    monkeypatch.setattr(
        cli,
        "run_reference_episode",
        lambda *args, **kwargs: calls.__setitem__("reference", calls["reference"] + 1),
    )
    monkeypatch.setattr(
        cli,
        "write_run_artifact",
        lambda **_: calls.__setitem__("artifact", calls["artifact"] + 1),
    )

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )
    assert calls == {"reference": 0, "artifact": 0}
    assert list(outside.iterdir()) == []
    assert not (results / "aggregates").exists()


def test_cli_rejects_results_root_replacement_during_provider_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider-side substitution cannot be accepted as a completed native run."""

    episode = _episode().model_copy(update={"qualitative_criteria": []})
    results = tmp_path / "results"
    root = results / "runs" / episode.episode_id / "openai-fictional"
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = _RaceFinishAdapter(
        lambda: _replace_cli_results_root(
            root, outside, tmp_path / "relocated-during-provider"
        )
    )
    calls = {"artifact": 0}

    class ProviderBoundaryRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def run(self, supplied: ModelAdapter, *, run_id: str) -> object:
            del run_id
            supplied.chat([], [])
            return SimpleNamespace()

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "EpisodeRunner", ProviderBoundaryRunner)
    monkeypatch.setattr(cli, "resolve_live_adapter", lambda _: adapter)
    monkeypatch.setattr(
        cli,
        "write_run_artifact",
        lambda **_: calls.__setitem__("artifact", calls["artifact"] + 1),
    )

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            episode,
            "openai/fictional",
            results_root=results,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )
    assert adapter.calls == 1
    assert calls == {"artifact": 0}
    assert list(outside.iterdir()) == []
    assert not (results / "aggregates").exists()


def test_cli_rejects_results_root_replacement_before_artifact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-run evaluation cannot publish through a substituted root."""

    episode = _episode().model_copy(update={"qualitative_criteria": []})
    results = tmp_path / "results"
    root = results / "runs" / episode.episode_id / "openai-fictional"
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = {"artifact": 0}

    class CompletedRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def run(self, _adapter: ModelAdapter, *, run_id: str) -> object:
            return SimpleNamespace(
                completed=True,
                initial_snapshot=object(),
                final_snapshot=object(),
                agent_result={},
                run_id=run_id,
            )

    def evaluate(*_: object, **__: object) -> EvaluationResult:
        _replace_cli_results_root(root, outside, tmp_path / "relocated-before-publish")
        return _evaluation(episode.episode_id, "run-r000001", "openai/fictional")

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "EpisodeRunner", CompletedRunner)
    monkeypatch.setattr(cli, "resolve_live_adapter", lambda _: _FinishAdapter())
    monkeypatch.setattr(cli, "evaluate_run", evaluate)
    monkeypatch.setattr(
        cli,
        "write_run_artifact",
        lambda **_: calls.__setitem__("artifact", calls["artifact"] + 1),
    )

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            episode,
            "openai/fictional",
            results_root=results,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )
    assert calls == {"artifact": 0}
    assert list(outside.iterdir()) == []
    assert not (results / "aggregates").exists()


def test_cli_root_checkpoints_allow_an_untouched_scripted_reference_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extra identity checks leave an ordinary verified run unchanged."""

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    result = cli._run_episode(  # noqa: SLF001
        _episode(),
        "reference-scripted",
        results_root=tmp_path / "results",
        run_id="run-r000001",
        judge_models=(),
        seed=None,
        configuration_hash=None,
    )
    run_directory = Path(str(result["run"]))
    assert run_directory.joinpath("run.json").is_file()
    assert run_directory.joinpath("final.db").is_file()


def test_cli_rejects_requested_root_ancestor_substitution_before_aggregate_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine completed run cannot publish through a later root substitution."""

    requested_ancestor = tmp_path / "requested"
    results_root = requested_ancestor / "results"
    relocated_ancestor = tmp_path / "relocated-requested"
    attacker_ancestor = tmp_path / "prepared-attacker-root"
    attacker_ancestor.mkdir()
    original_aggregate_writer = cli._write_run_aggregate  # noqa: SLF001

    def substitute_then_publish(*args: object, **kwargs: object) -> Path:
        requested_ancestor.replace(relocated_ancestor)
        attacker_ancestor.replace(requested_ancestor)
        return original_aggregate_writer(*args, **kwargs)

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "_write_run_aggregate", substitute_then_publish)

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results_root,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )

    relocated_results = relocated_ancestor / "results"
    run_directory = (
        relocated_results / "runs" / "epi-uk-01" / "reference-scripted" / "run-r000001"
    )
    # The run was already completely and safely materialized before the late attack.
    load_run_artifact(run_directory)
    for root in (relocated_results, requested_ancestor / "results"):
        if root.exists():
            assert not tuple(root.joinpath("aggregates").rglob("*.json"))
            assert not tuple(root.rglob("*.html"))


def _substitute_real_directory(
    visible: Path, relocated: Path, prepared_replacement: Path
) -> None:
    """Replace a visible directory with a prepared real directory, never a link."""

    visible.replace(relocated)
    prepared_replacement.replace(visible)


def _assert_no_publication(*roots: Path) -> None:
    """Ensure an attack left neither an aggregate nor a report behind."""

    for root in roots:
        if root.exists():
            assert not tuple(root.joinpath("aggregates").rglob("*.json"))
            assert not tuple(root.rglob("*.html"))


def test_cli_rejects_direct_results_root_substitution_before_aggregate_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requested result root itself cannot be replaced before publication."""

    results_root = tmp_path / "results"
    relocated_root = tmp_path / "relocated-results"
    prepared_replacement = tmp_path / "prepared-results"
    prepared_replacement.mkdir()
    original_aggregate_writer = cli._write_run_aggregate  # noqa: SLF001

    def substitute_then_publish(*args: object, **kwargs: object) -> Path:
        _substitute_real_directory(results_root, relocated_root, prepared_replacement)
        return original_aggregate_writer(*args, **kwargs)

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "_write_run_aggregate", substitute_then_publish)

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results_root,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )

    run_directory = (
        relocated_root / "runs" / "epi-uk-01" / "reference-scripted" / "run-r000001"
    )
    load_run_artifact(run_directory)
    _assert_no_publication(relocated_root, results_root)


@pytest.mark.parametrize("substitute_ancestor", [False, True])
def test_cli_discards_aggregate_after_post_publication_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitute_ancestor: bool,
) -> None:
    """A late substitution removes the retained-descriptor aggregate before success."""

    requested = tmp_path / "requested"
    results_root = requested / "results"
    target = requested if substitute_ancestor else results_root
    relocated = tmp_path / "relocated"
    replacement = tmp_path / "prepared-replacement"
    replacement.mkdir()
    original_aggregate_writer = cli._write_run_aggregate  # noqa: SLF001

    def publish_then_substitute(*args: object, **kwargs: object) -> Path:
        published = original_aggregate_writer(*args, **kwargs)
        _substitute_real_directory(target, relocated, replacement)
        return published

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(cli, "_write_run_aggregate", publish_then_substitute)

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results_root,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )

    relocated_results = relocated / "results" if substitute_ancestor else relocated
    run_directory = (
        relocated_results / "runs" / "epi-uk-01" / "reference-scripted" / "run-r000001"
    )
    load_run_artifact(run_directory)
    _assert_no_publication(relocated_results, requested / "results")


def test_secure_aggregate_atomic_publication_rejects_mid_link_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A substitution immediately after aggregate link publication is cleaned safely."""

    results_root = tmp_path / "results"
    relocated_root = tmp_path / "relocated-results"
    prepared_replacement = tmp_path / "prepared-results"
    prepared_replacement.mkdir()
    original_link = artifact_module.os.link
    attacked = False

    def link_then_substitute(source: str, destination: str, **kwargs: object) -> None:
        nonlocal attacked
        original_link(source, destination, **kwargs)
        if not attacked and "-runs-" in destination:
            attacked = True
            _substitute_real_directory(
                results_root, relocated_root, prepared_replacement
            )

    monkeypatch.setattr(cli, "validate_world", lambda _: SimpleNamespace(passed=True))
    monkeypatch.setattr(artifact_module.os, "link", link_then_substitute)

    with pytest.raises(
        ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
    ):
        cli._run_episode(  # noqa: SLF001
            _episode(),
            "reference-scripted",
            results_root=results_root,
            run_id="run-r000001",
            judge_models=(),
            seed=None,
            configuration_hash=None,
        )

    assert attacked
    run_directory = (
        relocated_root / "runs" / "epi-uk-01" / "reference-scripted" / "run-r000001"
    )
    load_run_artifact(run_directory)
    _assert_no_publication(relocated_root, results_root)


def _reference_aggregate(tmp_path: Path) -> object:
    """Create one genuine completed reference run for secure report publication tests."""

    directory = _completed_persisted_run(tmp_path)
    return build_aggregate_artifact([load_run_artifact(directory)])


def test_secure_report_publication_rejects_substitution_before_write(
    tmp_path: Path,
) -> None:
    """A report writer checks the retained directory before its first artifact."""

    aggregate = _reference_aggregate(tmp_path / "run")
    requested = tmp_path / "reports"
    secure = open_secure_execution_directory(requested)
    relocated = tmp_path / "relocated-reports"
    replacement = tmp_path / "prepared-reports"
    replacement.mkdir()
    try:
        _substitute_real_directory(requested, relocated, replacement)
        with pytest.raises(
            ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
        ):
            write_scorecard([aggregate], "scorecard.html", secure_directory=secure)
        _assert_no_publication(relocated, requested)
    finally:
        secure.close()


def test_secure_report_atomic_publication_rejects_mid_link_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report JSON link is removed if the output root changes immediately after it."""

    aggregate = _reference_aggregate(tmp_path / "run")
    requested = tmp_path / "reports"
    secure = open_secure_execution_directory(requested)
    relocated = tmp_path / "relocated-reports"
    replacement = tmp_path / "prepared-reports"
    replacement.mkdir()
    original_link = artifact_module.os.link
    attacked = False

    def link_then_substitute(source: str, destination: str, **kwargs: object) -> None:
        nonlocal attacked
        original_link(source, destination, **kwargs)
        if not attacked and destination == "scorecard.json":
            attacked = True
            _substitute_real_directory(requested, relocated, replacement)

    monkeypatch.setattr(artifact_module.os, "link", link_then_substitute)
    try:
        with pytest.raises(
            ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
        ):
            write_scorecard([aggregate], "scorecard.html", secure_directory=secure)
        assert attacked
        assert not tuple(relocated.rglob("scorecard.*"))
        assert not tuple(requested.rglob("scorecard.*"))
    finally:
        secure.close()


def test_secure_results_root_checkpoint_rejects_ancestor_substitution(
    tmp_path: Path,
) -> None:
    """A retained root also detects replacement of any visible ancestor."""

    root = tmp_path / "requested" / "nested" / "run-root"
    secured = open_secure_execution_directory(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = root.parent
    relocated = tmp_path / "relocated-parent"
    try:
        parent.replace(relocated)
        parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(
            ResultArtifactError, match="directory.*(replaced|unsafe)|unsafe"
        ):
            secured.checkpoint()
        assert list(outside.iterdir()) == []
    finally:
        secured.close()


def test_serialized_json_and_assignment_credentials_are_removed_everywhere(
    tmp_path: Path,
) -> None:
    """Strings that carry JSON/assignment credentials must cross the same boundary."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    # The terminal summary is itself persisted in the Action, agent result, transcript,
    # and run envelope.  It is intentionally assignment-shaped rather than a mapping.
    secret_finish = json.dumps(
        {
            "summary": "token=RUN_TOKEN_CANARY",
            "deliverable_refs": [],
            "unresolved_items": [],
        }
    )

    class SecretFinishAdapter(_SerializedSecretAdapter):
        def chat(
            self, messages: list[ProviderPayload], tools: list[ProviderPayload]
        ) -> ModelResponse:
            response = super().chat(messages, tools)
            return ModelResponse(
                message=response.message,
                text=response.text,
                tool_calls=[ToolCall("finish", "finish_episode", secret_finish)],
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )

    run = runner.run(SecretFinishAdapter(), run_id="run-r000001")
    artifact = write_run_artifact(
        run_directory=tmp_path / "results" / "run-r000001",
        episode_id=run.episode_id,
        model="fictional/model",
        world_id=_episode().world_id,
        world_version=_episode().world_version,
        configuration={"model": "fictional/model"},
        judge_models=(),
        run_id=run.run_id,
        initial_snapshot=run.initial_snapshot,
        final_snapshot=run.final_snapshot,
        agent_result=run.agent_result,
        evaluation=_evaluation(run.episode_id, run.run_id, "fictional/model"),
    )
    assert artifact.transcript_path == "transcript.jsonl"
    rendered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            tmp_path / "results" / "run-r000001" / "transcript.jsonl",
            tmp_path / "results" / "run-r000001" / "run.json",
        )
    )
    for canary in ("ARG_TOKEN_CANARY", "NESTED_TOKEN_CANARY", "RUN_TOKEN_CANARY"):
        assert canary not in rendered
    assert "token=RUN_TOKEN_CANARY" not in sanitize_text("token=RUN_TOKEN_CANARY")

    # Direct tool results are strings too; recursively parse and redact serialized JSON.
    assert "RESULT_TOKEN_CANARY" not in sanitize_text('{"token":"RESULT_TOKEN_CANARY"}')
    assert "NESTED_TOKEN_CANARY" not in sanitize_text(
        '{"nested":{"access-token":"NESTED_TOKEN_CANARY"}}'
    )


def test_transcript_tool_result_and_text_variants_share_the_secret_boundary(
    tmp_path: Path,
) -> None:
    """Serialized outputs, malformed JSON, and prose retain only safe content."""

    transcript = tmp_path / "secret-result.jsonl"
    run_agent(
        _OneToolThenStopAdapter(),
        "system",
        "prompt",
        _SecretResultExecutor(),
        [],
        transcript_path=str(transcript),
    )
    persisted = transcript.read_text(encoding="utf-8")
    assert "RESULT_TOKEN_CANARY" not in persisted
    assert "RESULT_TOKEN_CANARY" not in sanitize_text('{"token":"RESULT_TOKEN_CANARY"}')
    assert "MALFORMED_TOKEN_CANARY" not in sanitize_text(
        '{"accessToken":"MALFORMED_TOKEN_CANARY"'
    )
    assert "RUN_TOKEN_CANARY" not in sanitize_text("token: RUN_TOKEN_CANARY")
    assert sanitize_text("The fictional token ledger entry is ordinary prose.") == (
        "The fictional token ledger entry is ordinary prose."
    )
    assert sanitize_for_persistence(
        {"token_count": 3, "input_tokens": 4, "output_tokens": 5, "tokens_used": 6}
    ) == {"token_count": 3, "input_tokens": 4, "output_tokens": 5, "tokens_used": 6}


def test_persisted_json_and_rendered_text_share_the_secret_boundary(
    tmp_path: Path,
) -> None:
    """Report and error-like output files cannot reintroduce sanitized credentials."""

    json_path = write_json_atomic(
        tmp_path / "result.json", {"summary": "token=RUN_TOKEN_CANARY"}
    )
    html_path = write_text_atomic(
        tmp_path / "report.html", "<p>Authorization: Bearer HTML_TOKEN_CANARY</p>"
    )
    assert "RUN_TOKEN_CANARY" not in json_path.read_text(encoding="utf-8")
    assert "HTML_TOKEN_CANARY" not in html_path.read_text(encoding="utf-8")
