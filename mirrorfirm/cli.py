"""WP-13 local CLI for validated worlds, runs, persisted results, and sweeps."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import yaml

from mirrorfirm.core.digest import canonical_json
from mirrorfirm.core.models import EpisodeManifest, EvaluationResult, WorldManifest
from mirrorfirm.episodes import (
    EpisodeAuthoringError,
    ReferenceScriptError,
    run_reference_episode,
)
from mirrorfirm.evaluation import evaluate_run
from mirrorfirm.evaluation.qualitative import QualitativeJudge
from mirrorfirm.harness import EpisodeRunner
from mirrorfirm.harness.adapters import (
    AdapterResolutionError,
    resolve_live_adapter,
)
from mirrorfirm.packs.apex_accounting import (
    APEX_ACCOUNTING_LABEL,
    ApexImportError,
    install_apex_accounting,
    installed_pack_path,
    show_gold_output,
)
from mirrorfirm.reporting.artifacts import (
    AggregateArtifact,
    ResultArtifactError,
    RunArtifact,
    SecureExecutionDirectory,
    build_aggregate_artifact,
    load_aggregate_artifact,
    load_external_apex_artifact,
    load_run_artifact,
    open_secure_execution_directory,
    write_aggregate_artifact,
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
    discover_episodes,
    load_sweep_config,
    run_sweep,
)
from mirrorfirm.security import sanitize_text
from mirrorfirm.worldgen import compile_world, validate_world


class CLIError(ValueError):
    """A user-facing command could not safely complete."""


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the public local-only Mirror Firm command surface."""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        return _dispatch(arguments)
    except (
        CLIError,
        ResultArtifactError,
        SweepError,
        AdapterResolutionError,
        ApexImportError,
    ) as error:
        print(f"mirror-firm: {sanitize_text(str(error))}", file=sys.stderr)
        return 2
    except (EpisodeAuthoringError, ReferenceScriptError, OSError, ValueError) as error:
        print(f"mirror-firm: {sanitize_text(str(error))}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mirror-firm", description="Mirror Firm local evaluation CLI"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list authored worlds or episodes")
    listing.add_argument("resource", choices=("worlds", "episodes"))
    listing.add_argument("--json", action="store_true")

    validate = commands.add_parser(
        "validate", help="run ordered world validation gates"
    )
    validate.add_argument("--world", required=True)
    validate.add_argument("--json", action="store_true")

    run = commands.add_parser(
        "run", help="run one fresh stateful episode and persist artifacts"
    )
    run.add_argument("--episode", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--results-root", type=Path, default=Path("results"))
    run.add_argument("--run-id")
    run.add_argument("--judge-model", action="append", default=[])
    run.add_argument("--seed", type=int)
    run.add_argument("--json", action="store_true")

    evaluate = commands.add_parser(
        "evaluate", help="verify a persisted WP-09 evaluation result"
    )
    evaluate.add_argument("path", type=Path)
    evaluate.add_argument("--json", action="store_true")

    report = commands.add_parser(
        "report", help="render an offline scorecard from persisted artifacts"
    )
    report.add_argument("results_path", type=Path)
    report.add_argument("--output", required=True, type=Path)
    report.add_argument("--json", action="store_true")

    compare = commands.add_parser(
        "compare", help="compare compatible native persisted aggregates"
    )
    compare.add_argument("results_paths", nargs="+", type=Path)
    compare.add_argument("--output", required=True, type=Path)
    compare.add_argument("--json", action="store_true")

    sweep = commands.add_parser(
        "sweep", help="preflight and sequentially run a model matrix"
    )
    sweep.add_argument("config_path", type=Path)
    sweep.add_argument("--json", action="store_true")

    packs = commands.add_parser("packs", help="manage external episode packs")
    pack_commands = packs.add_subparsers(dest="pack_command", required=True)
    install = pack_commands.add_parser("install", help="install a pinned external pack")
    install.add_argument("pack", choices=["apex-accounting"])
    install.add_argument("--revision", required=True)
    install.add_argument("--cache-root", type=Path)
    install.add_argument("--json", action="store_true")
    show_gold = pack_commands.add_parser(
        "show-gold", help="explicitly inspect one public development gold output"
    )
    show_gold.add_argument("pack", choices=["apex-accounting"])
    show_gold.add_argument("task_id")
    show_gold.add_argument("--revision", required=True)
    show_gold.add_argument("--cache-root", type=Path)
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "list":
        _list_resources(arguments.resource, as_json=arguments.json)
        return 0
    if arguments.command == "validate":
        report = validate_world(_world_directory(arguments.world))
        payload = {
            "world": _world_manifest(_world_directory(arguments.world)).world_id,
            "passed": report.passed,
            "gates": [gate.__dict__ for gate in report.gates],
        }
        _emit(payload, as_json=arguments.json)
        return 0 if report.passed else 1
    if arguments.command == "run":
        output = _run_episode_command(
            episode_id=arguments.episode,
            model=arguments.model,
            results_root=arguments.results_root,
            run_id=arguments.run_id,
            judge_models=arguments.judge_model,
            seed=arguments.seed,
        )
        _emit(output, as_json=arguments.json)
        return 0
    if arguments.command == "evaluate":
        payload = _evaluate_persisted_input(arguments.path)
        _emit(payload, as_json=arguments.json)
        return 0
    if arguments.command == "report":
        output_path = Path(arguments.output).absolute()
        secure_output = open_secure_execution_directory(output_path.parent)
        try:
            secure_output.checkpoint()
            try:
                external = load_external_apex_artifact(arguments.results_path)
            except ResultArtifactError:
                aggregates = _load_aggregate_inputs(arguments.results_path)
                secure_output.checkpoint()
                html_path, json_path = write_scorecard(
                    aggregates,
                    output_path.name,
                    secure_directory=secure_output,
                )
            else:
                secure_output.checkpoint()
                html_path, json_path = write_external_apex_report(
                    external,
                    output_path.name,
                    secure_directory=secure_output,
                )
            secure_output.checkpoint()
            _emit(
                {"html": str(html_path), "json": str(json_path)},
                as_json=arguments.json,
            )
            return 0
        finally:
            secure_output.close()
    if arguments.command == "compare":
        output_path = Path(arguments.output).absolute()
        secure_output = open_secure_execution_directory(output_path.parent)
        try:
            secure_output.checkpoint()
            comparison_aggregates: list[AggregateArtifact] = [
                aggregate
                for path in arguments.results_paths
                for aggregate in _load_aggregate_inputs(path)
            ]
            secure_output.checkpoint()
            html_path, json_path = write_comparison_report(
                comparison_aggregates,
                output_path.name,
                secure_directory=secure_output,
            )
            secure_output.checkpoint()
            _emit(
                {"html": str(html_path), "json": str(json_path)},
                as_json=arguments.json,
            )
            return 0
        finally:
            secure_output.close()
    if arguments.command == "sweep":
        config = load_sweep_config(arguments.config_path)
        manifest, path = run_sweep(config, _run_sweep_one)
        _emit(
            {
                "manifest": str(path),
                "configuration_hash": manifest.configuration_hash,
                "entries": len(manifest.entries),
            },
            as_json=arguments.json,
        )
        return 0 if all(entry.status == "complete" for entry in manifest.entries) else 1
    if arguments.command == "packs":
        return _dispatch_pack(arguments)
    raise CLIError("unknown command")


def _dispatch_pack(arguments: argparse.Namespace) -> int:
    if arguments.pack_command == "install":
        pack = install_apex_accounting(
            revision=arguments.revision, cache_root=arguments.cache_root
        )
        _emit(
            {
                "pack_id": "apex-accounting",
                "revision": pack.revision,
                "root": str(pack.root),
                "contamination": pack.contamination,
                "report_label": APEX_ACCOUNTING_LABEL,
            },
            as_json=True,
        )
        return 0
    root = installed_pack_path(arguments.revision, cache_root=arguments.cache_root)
    print(show_gold_output(root, arguments.task_id))
    return 0


def _list_resources(resource: str, *, as_json: bool) -> None:
    if resource == "worlds":
        worlds = [
            {
                "world_id": manifest.world_id,
                "version": manifest.world_version,
                "jurisdiction": manifest.jurisdiction,
                "path": str(path),
            }
            for path, manifest in _worlds()
        ]
        if as_json:
            _emit(worlds, as_json=True)
        else:
            for world in worlds:
                print(
                    f"{world['world_id']}\t{world['jurisdiction']}\t{world['version']}"
                )
        return
    episodes = [
        {
            "episode_id": episode.episode_id,
            "jurisdiction": episode.jurisdiction,
            "world_id": episode.world_id,
            "world_version": episode.world_version,
            "type": "stateful",
            "reference_scripted": True,
            "external": False,
        }
        for episode in discover_episodes()
    ]
    if as_json:
        _emit(episodes, as_json=True)
    else:
        for episode in episodes:
            print(
                f"{episode['episode_id']}\t{episode['jurisdiction']}\t"
                f"{episode['world_id']}@{episode['world_version']}\tstateful\treference-scripted"
            )


def _run_episode_command(
    *,
    episode_id: str,
    model: str,
    results_root: Path,
    run_id: str | None,
    judge_models: Sequence[str],
    seed: int | None,
    configuration_hash: str | None = None,
) -> dict[str, object]:
    episode = _episode(episode_id)
    if run_id is None:
        # Do not inspect a caller-controlled output tree to choose a sequence number
        # until it has passed the same no-follow boundary used by execution.
        run_parent = results_root / "runs" / episode_id / _path_component(model)
        secure_parent = open_secure_execution_directory(run_parent)
        try:
            generated_run_id = _next_run_id(results_root, episode_id, model)
        finally:
            secure_parent.close()
    else:
        generated_run_id = run_id
    return _run_episode(
        episode,
        model,
        results_root=results_root,
        run_id=generated_run_id,
        judge_models=judge_models,
        seed=seed,
        configuration_hash=configuration_hash,
    )


def _run_episode(
    episode: EpisodeManifest,
    model: str,
    *,
    results_root: Path,
    run_id: str,
    judge_models: Sequence[str],
    seed: int | None,
    configuration_hash: str | None,
    baseline: bool = False,
    sweep_plan: SweepRunPlan | None = None,
) -> dict[str, object]:
    if not _RUN_ID.fullmatch(run_id):
        raise CLIError("run id must be a single safe path component")
    if len(judge_models) not in {0, 1, 2}:
        raise CLIError("judge configuration must contain one or two models")
    run_parent = results_root / "runs" / episode.episode_id / _path_component(model)
    configuration: dict[str, object] = {
        "episode_id": episode.episode_id,
        "model": model,
        "world_id": episode.world_id,
        "world_version": episode.world_version,
        "judge_models": sorted(judge_models),
        "seed": seed,
        "baseline": baseline,
    }
    # Retain both the requested result root and its per-model run parent until after
    # aggregate publication and the final success response.  The parent alone cannot
    # protect the sibling ``aggregates/`` tree once execution has completed.
    secure_publication_root = open_secure_execution_directory(results_root)
    secure_results_root = open_secure_execution_directory(run_parent)
    aggregate_directories: list[SecureExecutionDirectory] = []
    secure_aggregate_directory: SecureExecutionDirectory | None = None
    published_aggregate: Path | None = None

    def checkpoint() -> None:
        """Verify both retained publication boundaries before each phase."""

        secure_publication_root.checkpoint()
        secure_results_root.checkpoint()

    try:
        world_dir = _world_directory_for_id(episode.world_id)
        validation = validate_world(world_dir)
        # ``validate_world`` is intentionally comprehensive and may be slow.  It
        # receives no result-path authority, so check that the caller-visible root
        # still names the exact retained directory before entering compilation.
        checkpoint()
        if not validation.passed:
            raise CLIError(f"world validation failed for {episode.world_id}")
        evaluation: EvaluationResult | None
        run_kind: Literal["native_model", "scripted_reference"]
        with tempfile.TemporaryDirectory(prefix="mirrorfirm-cli-") as temporary:
            compiled = compile_world(world_dir, Path(temporary) / "world.db")
            # Compilation is another path-only subsystem.  Do not let a replacement
            # during it reach reference execution, adapter resolution, or a provider.
            checkpoint()
            if model == "reference-scripted":
                # The reference runner creates the run directory as its first write.
                checkpoint()
                reference = run_reference_episode(
                    episode,
                    compiled.database_path,
                    world_root=world_dir,
                    results_root=run_parent,
                    run_id=run_id,
                )
                run = reference.run
                evaluation = reference.evaluation
                # A terminal reference outcome is not accepted across a root swap.
                checkpoint()
                persisted_model = "reference (scripted) - not model performance"
                run_kind = "scripted_reference"
            else:
                if episode.qualitative_criteria and not judge_models:
                    raise CLIError("qualitative episodes require --judge-model")
                adapter = resolve_live_adapter(model)
                adapters = {
                    judge: resolve_live_adapter(judge) for judge in judge_models
                }
                judge = (
                    QualitativeJudge.from_model_adapters(adapters) if adapters else None
                )
                # Adapter construction may consult local configuration; recheck
                # immediately before the runner can create a directory or call it.
                checkpoint()
                runner = EpisodeRunner(
                    episode,
                    compiled.database_path,
                    world_root=world_dir,
                    results_root=run_parent,
                )
                # ``run`` creates the child directory before its first provider turn.
                checkpoint()
                run = runner.run(adapter, run_id=run_id)
                persisted_model = model
                run_kind = "native_model"
                # Do not accept terminal state before re-establishing output-root
                # identity; a provider can otherwise race this boundary.
                checkpoint()
                evaluation = (
                    evaluate_run(
                        episode,
                        run.initial_snapshot,
                        run.final_snapshot,
                        run.agent_result,
                        model=model,
                        qualitative_judge=judge,
                        judge_models=list(judge_models) if judge_models else None,
                    )
                    if run.completed
                    else None
                )
            # Judge work is also external/provider-facing.  Publishing begins only
            # if the retained root and every secured ancestor still match.
            checkpoint()
            run_directory = run_parent / run_id
            artifact = write_run_artifact(
                run_directory=run_directory,
                episode_id=episode.episode_id,
                model=persisted_model,
                world_id=episode.world_id,
                world_version=episode.world_version,
                configuration=configuration,
                configuration_hash=configuration_hash,
                judge_models=judge_models,
                run_id=run_id,
                initial_snapshot=run.initial_snapshot,
                final_snapshot=run.final_snapshot,
                agent_result=run.agent_result,
                evaluation=evaluation,
                seed=seed,
                error=None if evaluation is not None else "episode did not finish",
                run_kind=run_kind,
                jurisdiction=episode.jurisdiction,
                sweep_id=sweep_plan.sweep_id if sweep_plan is not None else None,
                sweep_run_index=(
                    sweep_plan.run_index if sweep_plan is not None else None
                ),
                fresh_world_id=(
                    sweep_plan.fresh_world_id if sweep_plan is not None else None
                ),
            )
        if evaluation is None:
            raise CLIError(
                "episode run is incomplete; no completed evaluation was written"
            )
        # Build the aggregate only through descriptor-relative children of the retained
        # requested root.  A later checkpoint failure removes the just-published file
        # through that same descriptor, never through the substituted visible path.
        checkpoint()
        secure_aggregate_directory, aggregate_directories = _aggregate_directory(
            secure_publication_root, artifact
        )
        checkpoint()
        aggregate_path = _write_run_aggregate(
            artifact,
            evaluation,
            secure_results_root=secure_publication_root,
            secure_run_parent=secure_results_root,
            secure_aggregate_directory=secure_aggregate_directory,
        )
        published_aggregate = aggregate_path
        checkpoint()
        secure_aggregate_directory.checkpoint()
        response = {
            "run": str(run_directory),
            "scores": str(run_directory / "scores.json"),
            "aggregate": str(aggregate_path),
            "episode_id": episode.episode_id,
            "model": persisted_model,
            "overall": evaluation.overall,
            "all_pass": evaluation.all_pass,
            "configuration_hash": artifact.configuration_hash,
        }
        # Do not hand paths to the caller after a final replacement race.
        checkpoint()
        return response
    except BaseException:
        if secure_aggregate_directory is not None and published_aggregate is not None:
            secure_aggregate_directory.remove_file_if_present(published_aggregate.name)
        raise
    finally:
        for directory in reversed(aggregate_directories):
            directory.close()
        secure_results_root.close()
        secure_publication_root.close()


def _run_sweep_one(
    episode: EpisodeManifest,
    model: str,
    run_index: int,
    config: SweepConfig,
    plan: SweepRunPlan,
) -> Path:
    output_root = Path(config.output_dir)
    response = _run_episode(
        episode,
        model,
        results_root=output_root,
        run_id=f"run-r{run_index:06d}",
        judge_models=config.judge_models,
        seed=config.seed,
        configuration_hash=plan.configuration_hash,
        baseline=config.baseline,
        sweep_plan=plan,
    )
    return Path(str(response["run"]))


def _write_run_aggregate(
    artifact: RunArtifact,
    evaluation: EvaluationResult,
    *,
    secure_results_root: SecureExecutionDirectory,
    secure_run_parent: SecureExecutionDirectory,
    secure_aggregate_directory: SecureExecutionDirectory,
) -> Path:
    """Write the cumulative compatible episode/model aggregate for this run.

    Each run directory is immutable.  Rebuilding the aggregate from the verified run
    envelopes therefore lets a five-run baseline acquire its reliability columns one
    fresh run at a time without ever rewriting a completed per-run result.
    """

    secure_results_root.checkpoint()
    secure_run_parent.checkpoint()
    compatible: list[tuple[RunArtifact, EvaluationResult]] = [(artifact, evaluation)]
    for name in sorted(os.listdir(secure_run_parent.descriptor)):
        if name == artifact.run_id:
            continue
        candidate: SecureExecutionDirectory | None = None
        try:
            candidate = secure_run_parent.open_child(name)
            secure_results_root.checkpoint()
            secure_run_parent.checkpoint()
            candidate.checkpoint()
            try:
                existing_artifact, existing_result = load_run_artifact(candidate.path)
            except ResultArtifactError:
                # Invalid completed-run candidates are not aggregate inputs.  A root
                # race is distinguished by the retained-boundary checkpoints below.
                secure_results_root.checkpoint()
                secure_run_parent.checkpoint()
                continue
            secure_results_root.checkpoint()
            secure_run_parent.checkpoint()
            candidate.checkpoint()
            if (
                existing_artifact.episode_id,
                existing_artifact.model,
                existing_artifact.world_id,
                existing_artifact.world_version,
                existing_artifact.configuration_hash,
                existing_artifact.judge_models,
            ) == (
                artifact.episode_id,
                artifact.model,
                artifact.world_id,
                artifact.world_version,
                artifact.configuration_hash,
                artifact.judge_models,
            ):
                compatible.append((existing_artifact, existing_result))
        finally:
            if candidate is not None:
                candidate.close()
    aggregate = build_aggregate_artifact(compatible)
    filename = f"{aggregate.configuration_hash[:12]}-runs-{aggregate.run_count}.json"
    secure_results_root.checkpoint()
    secure_aggregate_directory.checkpoint()
    path = write_aggregate_artifact(
        aggregate,
        secure_directory=secure_aggregate_directory,
        filename=filename,
    )
    secure_results_root.checkpoint()
    secure_aggregate_directory.checkpoint()
    return path


def _aggregate_directory(
    secure_results_root: SecureExecutionDirectory, artifact: RunArtifact
) -> tuple[SecureExecutionDirectory, list[SecureExecutionDirectory]]:
    """Open the aggregate output tree entirely through retained descriptors."""

    directories: list[SecureExecutionDirectory] = []
    current = secure_results_root
    try:
        for name in (
            "aggregates",
            artifact.episode_id,
            _path_component(artifact.model),
        ):
            current = current.ensure_child(name)
            directories.append(current)
        current.checkpoint()
        return current, directories
    except BaseException:
        for directory in reversed(directories):
            directory.close()
        raise


def _evaluate_persisted_input(path: Path) -> dict[str, object]:
    """Return only score data already produced by the WP-09 evaluation pipeline."""

    try:
        artifact, result = load_run_artifact(path)
    except ResultArtifactError:
        aggregates = _load_aggregate_inputs(path)
        return {
            "kind": "aggregate-evaluation",
            "aggregates": [
                aggregate.model_dump(mode="json") for aggregate in aggregates
            ],
        }
    return {
        "kind": "run-evaluation",
        "episode_id": artifact.episode_id,
        "run_id": artifact.run_id,
        "model": artifact.model,
        "evaluation": result.model_dump(mode="json"),
    }


def _load_aggregate_inputs(path: Path) -> tuple[AggregateArtifact, ...]:
    """Load a verified run, aggregate, or deterministic results-root collection."""

    if path.is_dir():
        direct = path / "aggregate.json"
        if direct.is_file():
            return (load_aggregate_artifact(direct),)
        aggregate_root = path / "aggregates"
        if aggregate_root.is_dir():
            candidates = sorted(aggregate_root.rglob("*.json"))
            if candidates:
                return tuple(
                    load_aggregate_artifact(candidate) for candidate in candidates
                )
    if path.name == "aggregate.json" or path.suffix == ".json":
        try:
            return (load_aggregate_artifact(path),)
        except ResultArtifactError:
            pass
    artifact, result = load_run_artifact(path)
    return (build_aggregate_artifact([(artifact, result)]),)


def _episode(episode_id: str) -> EpisodeManifest:
    for episode in discover_episodes():
        if episode.episode_id == episode_id:
            return episode
    raise CLIError(f"unknown episode {episode_id!r}")


def _worlds() -> tuple[tuple[Path, WorldManifest], ...]:
    candidates: list[tuple[Path, WorldManifest]] = []
    for path in sorted(_repository_root().joinpath("worlds").glob("*/world.yaml")):
        candidates.append((path.parent, _world_manifest(path.parent)))
    if not candidates:
        raise CLIError("no authored worlds were found")
    return tuple(sorted(candidates, key=lambda item: item[1].world_id))


def _world_directory(name: str) -> Path:
    for path, manifest in _worlds():
        if name in {manifest.world_id, path.name}:
            return path
    raise CLIError(f"unknown world {name!r}")


def _world_directory_for_id(world_id: str) -> Path:
    return _world_directory(world_id)


def _world_manifest(directory: Path) -> WorldManifest:
    try:
        raw = yaml.safe_load((directory / "world.yaml").read_text(encoding="utf-8"))
        return WorldManifest.model_validate(raw)
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise CLIError(
            f"world manifest {directory / 'world.yaml'} is invalid"
        ) from error


def _repository_root() -> Path:
    from mirrorfirm.episodes.manifests import repository_root

    return repository_root()


def _next_run_id(results_root: Path, episode_id: str, model: str) -> str:
    parent = results_root / "runs" / episode_id / _path_component(model)
    existing = sorted(path.name for path in parent.iterdir()) if parent.is_dir() else []
    return f"run-r{len(existing) + 1:06d}"


def _path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "model"


def _emit(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(canonical_json(payload))
        return
    if isinstance(payload, Mapping):
        for key in sorted(payload):
            print(f"{key}: {payload[key]}")
        return
    if isinstance(payload, Iterable) and not isinstance(payload, str | bytes):
        for item in payload:
            print(item)
        return
    print(payload)


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
