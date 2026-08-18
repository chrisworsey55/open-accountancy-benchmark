# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-13: sequential, pinned-world sweeps with no telemetry.
"""Declarative, preflighted model-matrix sweeps for persisted native results."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mirrorfirm.core.models import EpisodeManifest
from mirrorfirm.episodes.manifests import (
    EpisodeAuthoringError,
    load_episode_manifest,
    repository_root,
)
from mirrorfirm.harness.adapters.factory import (
    AdapterResolutionError,
    credentials_available,
    known_provider,
)
from mirrorfirm.reporting.artifacts import (
    REFERENCE_DISPLAY_LABEL,
    PublicRunConfiguration,
    canonical_configuration_hash,
    create_output_directory,
    load_run_artifact,
    write_json_atomic,
)
from mirrorfirm.security import sanitize_text
from mirrorfirm.worldgen.validate import validate_world


class SweepError(ValueError):
    """A sweep configuration cannot safely begin execution."""


class SweepConfig(BaseModel):
    """The intentionally small declarative WP-13 matrix configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episodes: tuple[str, ...]
    models: tuple[str, ...]
    runs: int = Field(gt=0)
    judge_models: tuple[str, ...] = ()
    output_dir: str
    seed: int | None = None
    baseline: bool = False


class SweepEntry(BaseModel):
    """One honest per-model/run attempt in a persisted sweep manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str
    model: str
    run_index: int
    status: Literal["complete", "failed"]
    run_path: str | None = None
    error: str | None = None


class SweepManifest(BaseModel):
    """Canonical non-secret configuration and all sequential sweep outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["mirrorfirm-sweep-v1"] = "mirrorfirm-sweep-v1"
    configuration: SweepConfig
    configuration_hash: str
    episode_pins: tuple[tuple[str, str, str], ...]
    entries: tuple[SweepEntry, ...]


class SweepRunPlan(BaseModel):
    """Immutable public provenance expected for exactly one sweep callback run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sweep_id: str
    episode_id: str
    world_id: str
    world_version: str
    jurisdiction: str
    model: str
    persisted_model: str
    run_kind: Literal["native_model", "scripted_reference"]
    run_index: int = Field(gt=0)
    configuration: PublicRunConfiguration
    configuration_hash: str
    fresh_world_id: str


RunOne = Callable[[EpisodeManifest, str, int, SweepConfig, SweepRunPlan], Path]


def load_sweep_config(path: str | Path) -> SweepConfig:
    """Load a strict JSON-or-YAML declarative sweep configuration."""

    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        return SweepConfig.model_validate(raw)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise SweepError(f"sweep configuration {source} is invalid") from error


def discover_episodes(*, root: str | Path | None = None) -> tuple[EpisodeManifest, ...]:
    """Discover every authored stateful episode without hard-coding the current seven."""

    repository = repository_root(root)
    paths = sorted((repository / "episodes").glob("*/ep-*.yaml"))
    try:
        episodes = tuple(load_episode_manifest(path) for path in paths)
    except EpisodeAuthoringError as error:
        raise SweepError("an authored episode manifest is invalid") from error
    if not episodes:
        raise SweepError("no authored episodes were found")
    if len({episode.episode_id for episode in episodes}) != len(episodes):
        raise SweepError("authored episode ids must be unique")
    return episodes


def preflight_sweep(
    config: SweepConfig,
    *,
    root: str | Path | None = None,
    require_credentials: bool = True,
) -> tuple[tuple[EpisodeManifest, ...], str]:
    """Validate the entire matrix before a provider can be constructed or called."""

    if config.baseline and config.runs != 5:
        raise SweepError("model baseline sweeps require exactly five fresh runs")
    if not config.episodes or not config.models:
        raise SweepError("sweeps require at least one episode and one model")
    if len(config.judge_models) not in {0, 1, 2}:
        raise SweepError("judge configuration must contain one or two models")
    if "reference-scripted" in config.judge_models:
        raise SweepError("reference-scripted is not a live qualitative judge")
    if config.baseline and "reference-scripted" in config.models:
        raise SweepError(
            "scripted references are controls and cannot be model baselines"
        )
    repository = repository_root(root)
    by_id = {
        episode.episode_id: episode for episode in discover_episodes(root=repository)
    }
    missing = sorted(set(config.episodes) - set(by_id))
    if missing:
        raise SweepError("unknown episodes: " + ", ".join(missing))
    selected = tuple(by_id[episode_id] for episode_id in sorted(config.episodes))
    if len(set(config.episodes)) != len(config.episodes):
        raise SweepError("sweep episode ids must be unique")
    if len(set(config.models)) != len(config.models):
        raise SweepError("sweep model identifiers must be unique")
    for episode in selected:
        world_dir = _world_directory(repository, episode.world_id)
        validation = validate_world(world_dir)
        if not validation.passed:
            raise SweepError(f"world validation failed for {episode.world_id}")
    for model in (*config.models, *config.judge_models):
        if model == "reference-scripted":
            continue
        try:
            known_provider(model)
        except AdapterResolutionError as error:
            raise SweepError(str(error)) from error
        if require_credentials and not credentials_available(model):
            raise SweepError("credentials unavailable for requested live provider")
    if "reference-scripted" in config.models and len(config.models) > 1:
        raise SweepError(
            "reference trajectories cannot be mixed with model performance"
        )
    if "reference-scripted" in config.models and config.judge_models:
        raise SweepError("reference trajectories use their pinned reference validator")
    output = Path(config.output_dir)
    if not output.is_absolute() and ".." in output.parts:
        raise SweepError("relative sweep output_dir must not contain parent traversal")
    resolved_output = (
        output if output.is_absolute() else repository / output
    ).resolve()
    if resolved_output in {Path("/"), repository}:
        raise SweepError("sweep output_dir is too broad")
    pins = tuple(
        (episode.episode_id, episode.world_id, episode.world_version)
        for episode in selected
    )
    configuration = config.model_dump(mode="json")
    configuration["episode_pins"] = [list(pin) for pin in pins]
    return selected, canonical_configuration_hash(configuration)


def run_sweep(
    config: SweepConfig,
    run_one: RunOne,
    *,
    root: str | Path | None = None,
    require_credentials: bool = True,
) -> tuple[SweepManifest, Path]:
    """Run a fully preflighted matrix sequentially and preserve partial failures honestly."""

    episodes, configuration_hash = preflight_sweep(
        config, root=root, require_credentials=require_credentials
    )
    repository = repository_root(root)
    configured_output = Path(config.output_dir)
    output_directory = (
        configured_output
        if configured_output.is_absolute()
        else repository / configured_output
    )
    try:
        create_output_directory(output_directory)
    except (OSError, ValueError) as error:
        raise SweepError(
            "sweep output directory is unsafe or already exists"
        ) from error
    # The shared atomic writer creates only regular directories and rejects symlinks.
    # Create the manifest after all entries, so a failed preflight performs no writes.
    entries: list[SweepEntry] = []
    accepted_run_ids: set[str] = set()
    accepted_world_ids: set[str] = set()
    for episode in episodes:
        for model in sorted(config.models):
            for run_index in range(1, config.runs + 1):
                plan = _run_plan(
                    episode,
                    model,
                    run_index,
                    config,
                    sweep_configuration_hash=configuration_hash,
                )
                try:
                    run_path = run_one(episode, model, run_index, config, plan)
                    try:
                        run_path.absolute().relative_to(output_directory.absolute())
                    except ValueError as error:
                        raise SweepError(
                            "sweep callback wrote a run outside the configured output directory"
                        ) from error
                    artifact, _ = load_run_artifact(run_path)
                    if not _matches_run_plan(artifact, plan):
                        raise SweepError(
                            "sweep callback wrote a run outside the preflighted configuration"
                        )
                    if artifact.run_id in accepted_run_ids:
                        raise SweepError(
                            "sweep callback produced a duplicate run identity"
                        )
                    if artifact.fresh_world_id in accepted_world_ids:
                        raise SweepError("sweep callback reused a fresh world identity")
                    accepted_run_ids.add(artifact.run_id)
                    if artifact.fresh_world_id is not None:
                        accepted_world_ids.add(artifact.fresh_world_id)
                except Exception as error:
                    entries.append(
                        SweepEntry(
                            episode_id=episode.episode_id,
                            model=model,
                            run_index=run_index,
                            status="failed",
                            error=_safe_error(str(error)),
                        )
                    )
                else:
                    entries.append(
                        SweepEntry(
                            episode_id=episode.episode_id,
                            model=model,
                            run_index=run_index,
                            status="complete",
                            run_path=run_path.as_posix(),
                        )
                    )
    manifest = SweepManifest(
        configuration=config,
        configuration_hash=configuration_hash,
        episode_pins=tuple(
            (episode.episode_id, episode.world_id, episode.world_version)
            for episode in episodes
        ),
        entries=tuple(entries),
    )
    return manifest, write_json_atomic(
        output_directory / "sweep-manifest.json",
        manifest.model_dump(mode="json"),
        output_root=output_directory,
    )


def _run_plan(
    episode: EpisodeManifest,
    model: str,
    run_index: int,
    config: SweepConfig,
    *,
    sweep_configuration_hash: str,
) -> SweepRunPlan:
    """Derive exactly the non-secret persisted public configuration for one run."""

    configuration = PublicRunConfiguration(
        episode_id=episode.episode_id,
        model_identifier=model,
        world_id=episode.world_id,
        world_version=episode.world_version,
        jurisdiction=episode.jurisdiction,
        run_kind=(
            "scripted_reference" if model == "reference-scripted" else "native_model"
        ),
        judge_models=tuple(sorted(config.judge_models)),
        seed=config.seed,
        baseline=config.baseline,
    )
    configuration_hash = canonical_configuration_hash(
        configuration.model_dump(mode="json")
    )
    fresh_world_id = hashlib.sha256(
        f"{sweep_configuration_hash}:{episode.episode_id}:{model}:{run_index}".encode(
            "utf-8"
        )
    ).hexdigest()
    return SweepRunPlan(
        sweep_id=sweep_configuration_hash,
        episode_id=episode.episode_id,
        world_id=episode.world_id,
        world_version=episode.world_version,
        jurisdiction=episode.jurisdiction,
        model=model,
        persisted_model=(
            REFERENCE_DISPLAY_LABEL if model == "reference-scripted" else model
        ),
        run_kind=configuration.run_kind,
        run_index=run_index,
        configuration=configuration,
        configuration_hash=configuration_hash,
        fresh_world_id=fresh_world_id,
    )


def _matches_run_plan(artifact: object, plan: SweepRunPlan) -> bool:
    """Fail closed unless a callback persisted exactly its immutable run plan."""

    from mirrorfirm.reporting.artifacts import RunArtifact

    if not isinstance(artifact, RunArtifact):  # pragma: no cover - defensive
        return False
    return (
        artifact.episode_id == plan.episode_id
        and artifact.world_id == plan.world_id
        and artifact.world_version == plan.world_version
        and artifact.model == plan.persisted_model
        and artifact.run_kind == plan.run_kind
        and artifact.configuration == plan.configuration
        and artifact.configuration_hash == plan.configuration_hash
        and artifact.sweep_id == plan.sweep_id
        and artifact.sweep_run_index == plan.run_index
        and artifact.fresh_world_id == plan.fresh_world_id
    )


def _world_directory(repository: Path, world_id: str) -> Path:
    for candidate in sorted((repository / "worlds").glob("*/world.yaml")):
        try:
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise SweepError(f"could not load world manifest {candidate}") from error
        if isinstance(raw, dict) and raw.get("world_id") == world_id:
            return candidate.parent
    raise SweepError(f"world {world_id!r} is unavailable")


def _safe_error(value: str) -> str:
    sanitized = sanitize_text(value)
    return "sweep run failed" if sanitized != value else sanitized
