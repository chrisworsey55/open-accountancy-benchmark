"""Typed loading and completeness checks for authored episode artefacts."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mirrorfirm.core.models import EpisodeManifest, ExpectedState
from mirrorfirm.evaluation.qualitative import ReferenceTargetExpectation


class EpisodeAuthoringError(ValueError):
    """An episode manifest or its companion answer key is not valid."""


class AnswerKey(BaseModel):
    """The complete deterministic expectation companion for one episode manifest."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    deterministic_criteria: dict[str, dict[str, JsonValue]]
    expected_state: list[dict[str, JsonValue]]
    qualitative_expectations: dict[str, ReferenceTargetExpectation] = Field(
        default_factory=dict
    )


def repository_root(start: str | Path | None = None) -> Path:
    """Find the repository root without coupling episode loading to a current directory."""

    candidate = (
        Path(start).resolve()
        if start is not None
        else Path(__file__).resolve().parents[2]
    )
    for root in (candidate, *candidate.parents):
        if (root / "episodes").is_dir() and (root / "worlds").is_dir():
            return root
    raise EpisodeAuthoringError("could not locate a Mirror Firm repository root")


def episode_root(jurisdiction: str, *, root: str | Path | None = None) -> Path:
    """Return the authored episode directory for one supported jurisdiction."""

    directory = repository_root(root) / "episodes" / jurisdiction
    if not directory.is_dir():
        raise EpisodeAuthoringError(
            f"episode directory for jurisdiction {jurisdiction!r} is missing"
        )
    return directory


def load_episode_manifest(path: str | Path) -> EpisodeManifest:
    """Load one §E.13 manifest from a YAML document."""

    manifest_path = Path(path)
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return EpisodeManifest.model_validate(raw)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise EpisodeAuthoringError(
            f"episode manifest {manifest_path} is invalid"
        ) from error


def load_answer_key(path: str | Path) -> AnswerKey:
    """Load one deterministic answer-key companion from YAML."""

    answer_key_path = Path(path)
    try:
        raw = yaml.safe_load(answer_key_path.read_text(encoding="utf-8"))
        return AnswerKey.model_validate(raw)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise EpisodeAuthoringError(
            f"answer key {answer_key_path} is invalid"
        ) from error


def load_uk_episode_manifests(
    *, root: str | Path | None = None
) -> tuple[EpisodeManifest, ...]:
    """Load the authored EP-UK-01 through EP-UK-04 catalogue in filename order."""

    manifests = tuple(
        load_episode_manifest(path)
        for path in sorted(episode_root("uk", root=root).glob("ep-uk-*.yaml"))
    )
    if not manifests:
        raise EpisodeAuthoringError("no UK episode manifests were found")
    return manifests


def load_us_episode_manifests(
    *, root: str | Path | None = None
) -> tuple[EpisodeManifest, ...]:
    """Load the authored EP-US-01 through EP-US-03 catalogue in filename order."""

    manifests = tuple(
        load_episode_manifest(path)
        for path in sorted(episode_root("us", root=root).glob("ep-us-*.yaml"))
    )
    if not manifests:
        raise EpisodeAuthoringError("no US episode manifests were found")
    return manifests


def answer_key_path(manifest_path: str | Path) -> Path:
    """Resolve the deliberately adjacent answer-key location for one manifest."""

    path = Path(manifest_path)
    return path.parent / "answer-keys" / path.name


def validate_answer_key(manifest: EpisodeManifest, answer_key: AnswerKey) -> None:
    """Require answer keys to exactly cover the manifest's authored expectations."""

    if answer_key.episode_id != manifest.episode_id:
        raise EpisodeAuthoringError(
            f"answer key belongs to {answer_key.episode_id!r}, not {manifest.episode_id!r}"
        )
    criteria = {
        criterion.id: criterion.params for criterion in manifest.deterministic_criteria
    }
    if set(answer_key.deterministic_criteria) != set(criteria):
        raise EpisodeAuthoringError(
            f"answer key criteria for {manifest.episode_id!r} do not exactly match the manifest"
        )
    for criterion_id, expected_params in criteria.items():
        if answer_key.deterministic_criteria[criterion_id] != expected_params:
            raise EpisodeAuthoringError(
                f"answer key criterion {criterion_id!r} does not match its manifest"
            )
    expected_state = [
        assertion.model_dump(mode="json")
        for assertion in manifest.expected_state.assertions
    ]
    key_expected_state = [
        assertion.model_dump(mode="json")
        for assertion in ExpectedState.model_validate(
            {"assertions": answer_key.expected_state}
        ).assertions
    ]
    if key_expected_state != expected_state:
        raise EpisodeAuthoringError(
            f"answer key expected_state for {manifest.episode_id!r} does not match its manifest"
        )
    qualitative_ids = {criterion.id for criterion in manifest.qualitative_criteria}
    if set(answer_key.qualitative_expectations) != qualitative_ids:
        raise EpisodeAuthoringError(
            f"answer key qualitative expectations for {manifest.episode_id!r} do not exactly match its manifest"
        )


def reference_qualitative_expectations(
    episode: EpisodeManifest,
) -> dict[str, ReferenceTargetExpectation]:
    """Load gold qualitative expectations only after a scripted reference has run."""

    for path in episode_root(episode.jurisdiction).glob("ep-*.yaml"):
        manifest = load_episode_manifest(path)
        if manifest.episode_id != episode.episode_id:
            continue
        answer_key = load_answer_key(answer_key_path(path))
        validate_answer_key(manifest, answer_key)
        return answer_key.qualitative_expectations
    raise EpisodeAuthoringError(
        f"no repository answer key exists for {episode.episode_id!r}"
    )
