"""Snapshot-backed execution of one authored stateful episode."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import EpisodeManifest, StateSnapshot
from mirrorfirm.harness.adapters.base import ModelAdapter
from mirrorfirm.harness.agent_loop import run_agent
from mirrorfirm.harness.stateful_episode import StatefulEpisodeAdapter
from mirrorfirm.reporting.artifacts import (
    ResultArtifactError,
    SecureExecutionDirectory,
    SecureExecutionFile,
    open_secure_execution_directory,
)
from mirrorfirm.tools import WorldToolEngine

_SYSTEM_PROMPT = (
    "You are operating a fictional Mirror Firm episode. Use only the supplied tools. "
    "Do not claim a proposed action was executed. Complete only by calling "
    "finish_episode with an accurate summary."
)


@dataclass(frozen=True)
class EpisodeRunResult:
    """Artifacts and normalized outcome of one isolated episode run."""

    episode_id: str
    run_id: str
    database_path: Path
    initial_snapshot: StateSnapshot
    final_snapshot: StateSnapshot
    agent_result: dict[str, object]
    completed: bool


class EpisodeRunner:
    """Copy a compiled world, snapshot it, run one agent, then snapshot it again."""

    def __init__(
        self,
        episode: EpisodeManifest,
        compiled_database_path: str | Path,
        *,
        world_root: str | Path,
        results_root: str | Path,
    ) -> None:
        self.episode = episode
        self.compiled_database_path = Path(compiled_database_path).resolve()
        self.world_root = Path(world_root).resolve()
        # Do not resolve a caller-controlled output path: resolving would silently
        # turn a symlinked requested root into its attacker-selected target.
        self.results_root = Path(results_root).absolute()
        if not self.compiled_database_path.is_file():
            raise FileNotFoundError(self.compiled_database_path)
        if not self.world_root.is_dir():
            raise NotADirectoryError(self.world_root)
        self._validate_budget()
        self._results_directory = open_secure_execution_directory(self.results_root)

    def run(self, adapter: ModelAdapter, *, run_id: str) -> EpisodeRunResult:
        """Run an isolated copy of the world and materialize both state snapshots."""

        self._results_directory.verify_current()
        secure_run_directory = self._prepare_run_directory(run_id)
        run_directory = secure_run_directory.path
        world_file: SecureExecutionFile | None = None
        initial_file: SecureExecutionFile | None = None
        final_file: SecureExecutionFile | None = None
        transcript_file: SecureExecutionFile | None = None
        terminal_engine_snapshot: SecureExecutionFile | None = None
        engine_snapshots: SecureExecutionDirectory | None = None
        try:
            self._results_directory.verify_child_directory(run_id, secure_run_directory)
            world_file = secure_run_directory.create_file_from(
                "world.db", self.compiled_database_path
            )
            database_path = world_file.path
            world_file.verify_current()
            secure_run_directory.verify_current()
            with WorldStore.open(database_path) as store:
                self._validate_world(store)
                world_file.verify_current()
                secure_run_directory.verify_current()
                initial_snapshot = store.snapshot_to_directory_fd(
                    secure_run_directory.descriptor,
                    "initial.db",
                    displayed_path=run_directory / "initial.db",
                    snapshot_id=f"snp-{run_id}-initial",
                    episode_run_id=run_id,
                    phase="initial",
                    verify_destination=secure_run_directory.verify_current,
                )
                initial_file = secure_run_directory.retain_regular_file("initial.db")
                engine_snapshots = secure_run_directory.create_child("engine-snapshots")
                try:

                    def verify_engine_snapshot_boundary() -> None:
                        self._results_directory.verify_child_directory(
                            run_id, secure_run_directory
                        )
                        secure_run_directory.verify_child_directory(
                            "engine-snapshots", engine_snapshots
                        )
                        world_file.verify_current()

                    def write_terminal_snapshot(snapshot_id: str) -> StateSnapshot:
                        nonlocal terminal_engine_snapshot
                        verify_engine_snapshot_boundary()
                        snapshot = store.snapshot_to_directory_fd(
                            engine_snapshots.descriptor,
                            f"{snapshot_id}.db",
                            displayed_path=engine_snapshots.path / f"{snapshot_id}.db",
                            snapshot_id=snapshot_id,
                            episode_run_id=run_id,
                            phase="final",
                            transactional=True,
                            verify_destination=verify_engine_snapshot_boundary,
                        )
                        terminal_engine_snapshot = engine_snapshots.retain_regular_file(
                            f"{snapshot_id}.db"
                        )
                        terminal_engine_snapshot.verify_current()
                        verify_engine_snapshot_boundary()
                        return snapshot

                    engine = WorldToolEngine(
                        store,
                        world_root=self.world_root,
                        actor_id=self.episode.agent_person_id,
                        engagement_id=self.episode.engagement_id,
                        episode_run_id=run_id,
                        snapshot_writer=write_terminal_snapshot,
                        active_event_ids=self.episode.event_ids,
                    )
                    tool_executor = StatefulEpisodeAdapter(
                        engine,
                        allowed_tools=frozenset(self.episode.allowed_tools),
                        max_steps=self.episode.budget.max_steps,
                        max_world_days=self.episode.budget.max_world_days,
                    )
                    transcript_file = secure_run_directory.create_empty_file(
                        "transcript.jsonl"
                    )
                    transcript_writer = transcript_file.open_text_writer()
                    try:
                        verify_engine_snapshot_boundary()
                        agent_result = run_agent(
                            adapter,
                            _SYSTEM_PROMPT,
                            self.episode.instruction,
                            tool_executor,
                            tool_executor.provider_tools,
                            max_turns=self.episode.budget.max_steps + 1,
                            max_tokens=self.episode.budget.max_tokens,
                            require_finish_episode=True,
                            transcript_file=transcript_writer,
                        )
                    finally:
                        transcript_writer.close()
                    transcript_file.verify_current()
                    verify_engine_snapshot_boundary()
                    final_snapshot = store.snapshot_to_directory_fd(
                        secure_run_directory.descriptor,
                        "final.db",
                        displayed_path=run_directory / "final.db",
                        snapshot_id=self._final_snapshot_id(store, agent_result)
                        if tool_executor.is_finished
                        else f"snp-{run_id}-final",
                        episode_run_id=run_id,
                        phase="final",
                        verify_destination=verify_engine_snapshot_boundary,
                    )
                    final_file = secure_run_directory.retain_regular_file("final.db")
                    final_file.verify_current()
                    if initial_file is not None:
                        initial_file.verify_current()
                    if terminal_engine_snapshot is not None:
                        terminal_engine_snapshot.verify_current()
                    verify_engine_snapshot_boundary()
                finally:
                    if engine_snapshots is not None:
                        engine_snapshots.close()
        except BaseException as error:
            # Retained no-follow descriptors ensure cleanup cannot follow an attacker
            # replacement that happened after initial output-root validation.
            secure_run_directory.clean_partial_contents()
            try:
                secure_run_directory.verify_current()
            except ResultArtifactError as integrity_error:
                raise integrity_error from error
            raise
        finally:
            for file in (
                terminal_engine_snapshot,
                transcript_file,
                final_file,
                initial_file,
                world_file,
            ):
                if file is not None:
                    file.close()
            secure_run_directory.close()

        return EpisodeRunResult(
            episode_id=self.episode.episode_id,
            run_id=run_id,
            database_path=database_path,
            initial_snapshot=initial_snapshot,
            final_snapshot=final_snapshot,
            agent_result=agent_result,
            completed=tool_executor.is_finished,
        )

    def _validate_budget(self) -> None:
        budget = self.episode.budget
        if budget.max_steps <= 0:
            raise ValueError("episode max_steps must be positive")
        if budget.max_tokens <= 0:
            raise ValueError("episode max_tokens must be positive")
        if budget.max_world_days <= 0:
            raise ValueError("episode max_world_days must be positive")
        if "finish_episode" not in self.episode.allowed_tools:
            raise ValueError("stateful episodes must allow finish_episode")

    def _prepare_run_directory(self, run_id: str) -> SecureExecutionDirectory:
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id must be a single path component")
        return self._results_directory.create_child(run_id)

    @staticmethod
    def _final_snapshot_id(
        store: WorldStore, agent_result: Mapping[str, object]
    ) -> str:
        """Recover the exact committed terminal identity for runner final snapshots."""

        with store.view() as view:
            actions = view.actions()
        if not actions:
            raise ResultArtifactError("finished episode has no terminal Action")
        terminal = actions[-1]
        payload = terminal.output_payload
        if terminal.tool != "finish_episode" or not isinstance(payload, Mapping):
            raise ResultArtifactError("finished episode has no terminal Action")
        snapshot_id = payload.get("final_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ResultArtifactError("terminal Action has no final snapshot identity")
        summary = agent_result.get("finish_summary")
        if not isinstance(summary, Mapping):
            raise ResultArtifactError("terminal Action contradicts agent completion")
        if summary.get("final_snapshot_id") != snapshot_id:
            raise ResultArtifactError("terminal Action contradicts agent completion")
        return snapshot_id

    def _validate_world(self, store: WorldStore) -> None:
        with store.view() as view:
            available_event_ids = frozenset(event.id for event in view.events())
        missing_events = frozenset(self.episode.event_ids) - available_event_ids
        if missing_events:
            names = ", ".join(sorted(missing_events))
            raise ValueError(f"episode declares missing event ids: {names}")

        engine = WorldToolEngine(
            store,
            world_root=self.world_root,
            actor_id=self.episode.agent_person_id,
            engagement_id=self.episode.engagement_id,
        )
        if engine.manifest.world_id != self.episode.world_id:
            raise ValueError("episode world_id does not match the compiled world")
        if engine.manifest.world_version != self.episode.world_version:
            raise ValueError("episode world_version does not match the compiled world")
