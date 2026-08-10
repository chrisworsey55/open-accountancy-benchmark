"""Snapshot-backed execution of one authored stateful episode."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import EpisodeManifest, StateSnapshot
from mirrorfirm.harness.adapters.base import ModelAdapter
from mirrorfirm.harness.agent_loop import run_agent
from mirrorfirm.harness.stateful_episode import StatefulEpisodeAdapter
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
        self.results_root = Path(results_root).resolve()
        if not self.compiled_database_path.is_file():
            raise FileNotFoundError(self.compiled_database_path)
        if not self.world_root.is_dir():
            raise NotADirectoryError(self.world_root)
        self._validate_budget()

    def run(self, adapter: ModelAdapter, *, run_id: str) -> EpisodeRunResult:
        """Run an isolated copy of the world and materialize both state snapshots."""

        run_directory = self._prepare_run_directory(run_id)
        database_path = run_directory / "world.db"
        shutil.copy2(self.compiled_database_path, database_path)

        with WorldStore.open(database_path) as store:
            self._validate_world(store)
            initial_snapshot = store.snapshot(
                run_directory / "initial.db",
                snapshot_id=f"snp-{run_id}-initial",
                episode_run_id=run_id,
                phase="initial",
            )
            engine = WorldToolEngine(
                store,
                world_root=self.world_root,
                actor_id=self.episode.agent_person_id,
                engagement_id=self.episode.engagement_id,
                episode_run_id=run_id,
                snapshot_dir=run_directory / "engine-snapshots",
                active_event_ids=self.episode.event_ids,
            )
            tool_executor = StatefulEpisodeAdapter(
                engine,
                allowed_tools=frozenset(self.episode.allowed_tools),
                max_steps=self.episode.budget.max_steps,
                max_world_days=self.episode.budget.max_world_days,
            )
            agent_result = run_agent(
                adapter,
                _SYSTEM_PROMPT,
                self.episode.instruction,
                tool_executor,
                tool_executor.provider_tools,
                max_turns=self.episode.budget.max_steps + 1,
                max_tokens=self.episode.budget.max_tokens,
                require_finish_episode=True,
                transcript_path=str(run_directory / "transcript.jsonl"),
            )
            final_snapshot = store.snapshot(
                run_directory / "final.db",
                snapshot_id=f"snp-{run_id}-final",
                episode_run_id=run_id,
                phase="final",
            )

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

    def _prepare_run_directory(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id must be a single path component")
        run_directory = self.results_root / run_id
        if run_directory.exists():
            raise FileExistsError(run_directory)
        run_directory.mkdir(parents=True)
        return run_directory

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
