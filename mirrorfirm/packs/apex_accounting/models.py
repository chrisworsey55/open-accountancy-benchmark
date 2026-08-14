"""Typed, pack-local representations for imported APEX static tasks.

These models deliberately live outside the stable stateful domain schema.  APEX tasks
have no accounting-world mutation surface, and extending ``EpisodeManifest`` would
incorrectly make a public development pack part of the core world contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mirrorfirm.core.models import CriterionResult, JudgeCriterion, Sha256


class StaticTaskBudget(BaseModel):
    """The fixed APEX execution ceiling from SPEC §J."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=500, gt=0)
    max_tokens: int = Field(default=5_000_000, gt=0)


class SourceFile(BaseModel):
    """One content-addressed file retained in the downloaded pack cache."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class GeneratedTaskArtifact(BaseModel):
    """One generated, agent-visible task document pinned by the pack manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    sha256: Sha256


class RuntimeFile(BaseModel):
    """One source-verified asset mounted into a task's flat read-only root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_path: str
    origin: Literal["world", "task_files"]
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class TaskAssetMapping(RuntimeFile):
    """Manifest-only source provenance for a mounted static-task asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    source_path: str


class StaticTaskEpisode(BaseModel):
    """A read-only external task mapped from one APEX dataset row.

    No gold answer field is present by design.  The model is what reaches a model
    adapter and is also what is persisted under ``tasks/`` in the installed pack.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_type: Literal["static_task"] = "static_task"
    episode_id: str
    task_id: str
    source_task_name: str
    world_id: str
    instruction: str
    qualitative_criteria: tuple[JudgeCriterion, ...]
    allowed_tools: tuple[str, ...]
    budget: StaticTaskBudget
    world_files: tuple[str, ...]
    task_files: tuple[str, ...]
    runtime_files: tuple[RuntimeFile, ...]
    source_revision: str

    def model_visible_context(self) -> dict[str, object]:
        """Return the complete approved agent context without evaluator-only data."""

        return {
            "episode_id": self.episode_id,
            "episode_type": self.episode_type,
            "instruction": self.instruction,
            "allowed_tools": list(self.allowed_tools),
            "authorised_runtime_files": [
                runtime_file.runtime_path for runtime_file in self.runtime_files
            ],
            "world_files": list(self.world_files),
            "task_files": list(self.task_files),
            "criteria": [
                {
                    "id": criterion.id,
                    "prompt": criterion.prompt,
                    "scale": criterion.scale,
                }
                for criterion in self.qualitative_criteria
            ],
        }


class StaticTaskEvaluation(BaseModel):
    """Typed, contamination-labelled qualitative result for one static task run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    source_revision: str
    report_label: str
    contamination: Literal["public-reference-answers"]
    criterion_results: tuple[CriterionResult, ...]
    score: float = Field(ge=0.0, le=1.0)
    passed: bool


class StaticTaskExecutionProvenance(BaseModel):
    """Digests of the immutable installed bytes used by one static execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_artifact: GeneratedTaskArtifact
    runtime_assets: tuple[RuntimeFile, ...]


class ApexPackManifest(BaseModel):
    """The reproducible installed-pack manifest and contamination declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: Literal["apex-accounting"] = "apex-accounting"
    repository: Literal["mercor/apex-accounting"] = "mercor/apex-accounting"
    revision: str
    source_license: Literal["CC-BY-4.0"] = "CC-BY-4.0"
    paper: Literal["arXiv:2607.27189"] = "arXiv:2607.27189"
    contamination: Literal["public-reference-answers"]
    report_label: str
    task_count: int = Field(ge=0)
    world_file_count: int = Field(ge=0)
    task_file_count: int = Field(ge=0)
    source_files: tuple[SourceFile, ...]
    source_digest: Sha256
    task_artifacts: tuple[GeneratedTaskArtifact, ...]
    criterion_count: int = Field(ge=0)
    source_metadata: dict[str, JsonValue]
    task_asset_mappings: tuple[TaskAssetMapping, ...]


class ApexPackLock(BaseModel):
    """The active cache lock, tying a revision to one exact installed manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: Literal["apex-accounting"] = "apex-accounting"
    repository: Literal["mercor/apex-accounting"] = "mercor/apex-accounting"
    revision: str
    manifest_sha256: Sha256
    source_digest: Sha256


class InstalledApexPack:
    """A verified cached pack root with only safe public accessors."""

    def __init__(
        self,
        root: Path,
        manifest: ApexPackManifest,
        *,
        verified_tasks: Mapping[str, StaticTaskEpisode] | None = None,
        verified_task_bytes: Mapping[str, bytes] | None = None,
        verified_runtime_bytes: Mapping[str, Mapping[str, bytes]] | None = None,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self._verified_tasks = MappingProxyType(dict(verified_tasks or {}))
        self._verified_task_bytes = MappingProxyType(dict(verified_task_bytes or {}))
        self._verified_runtime_bytes = MappingProxyType(
            {
                task_id: MappingProxyType(dict(assets))
                for task_id, assets in (verified_runtime_bytes or {}).items()
            }
        )

    @property
    def repository(self) -> str:
        return self.manifest.repository

    @property
    def revision(self) -> str:
        return self.manifest.revision

    @property
    def contamination(self) -> str:
        return self.manifest.contamination

    @property
    def report_label(self) -> str:
        return self.manifest.report_label

    @property
    def task_count(self) -> int:
        return self.manifest.task_count

    @property
    def world_file_count(self) -> int:
        return self.manifest.world_file_count

    @property
    def task_file_count(self) -> int:
        return self.manifest.task_file_count

    def verified_task(
        self, task_id: str
    ) -> tuple[StaticTaskEpisode, GeneratedTaskArtifact, bytes]:
        """Return the parsed task and the exact artifact bytes already verified."""

        try:
            task = self._verified_tasks[task_id]
            data = self._verified_task_bytes[task_id]
        except KeyError as error:
            raise KeyError(
                f"verified static task {task_id!r} was not loaded"
            ) from error
        artifact = next(
            (
                item
                for item in self.manifest.task_artifacts
                if item.relative_path == f"tasks/{task_id}.json"
            ),
            None,
        )
        if artifact is None:
            raise KeyError(f"verified static task artifact {task_id!r} is unavailable")
        return task, artifact, data

    def verified_runtime_assets(self, task_id: str) -> Mapping[str, bytes]:
        """Return immutable bytes for the exact task-scoped runtime asset set."""

        try:
            return self._verified_runtime_bytes[task_id]
        except KeyError as error:
            raise KeyError(
                f"verified static runtime assets for {task_id!r} were not loaded"
            ) from error

    def assert_suite_eligible(self) -> None:
        """Refuse the contaminated public dev pack from headline aggregation."""

        from .importer import PackContaminationError

        raise PackContaminationError(
            "APEX-Accounting has public reference answers and is not eligible for "
            "Mirror Firm headline-suite aggregation"
        )
