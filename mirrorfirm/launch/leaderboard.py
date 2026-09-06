"""Deterministically transform verified result aggregates into public rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from mirrorfirm.reporting.artifacts import (
    REFERENCE_DISPLAY_LABEL,
    AggregateArtifact,
    ResultArtifactError,
    load_aggregate_artifact,
)

Jurisdiction = Literal["uk", "us"]


@dataclass(frozen=True)
class LeaderboardEntry:
    """One transparent public projection of a verified aggregate artifact."""

    identifier: str
    source_path: Path
    submission_name: str
    organisation: str | None
    jurisdiction: Jurisdiction
    workflow: str
    benchmark_version: str
    scoring_version: str
    overall: float
    accuracy: float
    evidence: float
    safety: float
    cost_usd: float | None
    human_review: str
    critical_failures: tuple[str, ...]
    run_date: str | None
    is_reference: bool
    eligible_for_ranking: bool
    cohort: str = ""
    run_count: int = 0
    configuration: str = ""
    reliability: str = ""
    rank: int | None = None


@dataclass(frozen=True)
class Leaderboard:
    """All accepted entries plus intentionally non-fatal source-loading issues."""

    ranked: tuple[LeaderboardEntry, ...]
    disqualified: tuple[LeaderboardEntry, ...]
    references: tuple[LeaderboardEntry, ...]
    issues: tuple[str, ...]
    experiments: tuple[LeaderboardEntry, ...] = ()

    def for_jurisdiction(self, jurisdiction: str) -> "Leaderboard":
        """Return a filtered projection without changing stored rank semantics."""

        if jurisdiction not in {"all", "uk", "us"}:
            raise ValueError("jurisdiction must be all, uk, or us")
        if jurisdiction == "all":
            return self
        return Leaderboard(
            ranked=tuple(
                row for row in self.ranked if row.jurisdiction == jurisdiction
            ),
            disqualified=tuple(
                row for row in self.disqualified if row.jurisdiction == jurisdiction
            ),
            references=tuple(
                row for row in self.references if row.jurisdiction == jurisdiction
            ),
            issues=self.issues,
            experiments=tuple(
                row for row in self.experiments if row.jurisdiction == jurisdiction
            ),
        )


def load_leaderboard(results_root: str | Path | None) -> Leaderboard:
    """Load all valid aggregates below ``results_root`` without rerunning anything.

    Invalid or incomplete artifact files are reported as issues rather than silently
    converted into rows.  The public result schema has no organisation, run-date, or
    human-review metric, so those values remain unavailable rather than being guessed.
    """

    if results_root is None:
        return Leaderboard((), (), (), ())
    root = Path(results_root)
    if not root.exists():
        return Leaderboard((), (), (), ())
    if not root.is_dir() or root.is_symlink():
        return Leaderboard((), (), (), ("configured results root is unsafe",))

    entries: list[LeaderboardEntry] = []
    issues: list[str] = []
    for candidate in _aggregate_candidates(root):
        if candidate.is_symlink():
            issues.append(f"ignored symlinked aggregate: {candidate.name}")
            continue
        try:
            aggregate = load_aggregate_artifact(candidate)
            entries.append(_entry_from_aggregate(aggregate, candidate))
        except (OSError, ResultArtifactError, ValueError) as error:
            issues.append(f"ignored malformed aggregate {candidate.name}: {error}")

    ranked_candidates = [
        row for row in entries if not row.is_reference and row.eligible_for_ranking
    ]
    ranked = _rank(ranked_candidates)
    disqualified = sorted(
        (row for row in entries if not row.is_reference and row.critical_failures),
        key=_display_order,
    )
    references = sorted(
        (row for row in entries if row.is_reference), key=_display_order
    )
    return Leaderboard(
        tuple(ranked),
        tuple(disqualified),
        tuple(references),
        tuple(issues),
        tuple(
            sorted(
                (
                    row
                    for row in entries
                    if not row.is_reference
                    and not row.eligible_for_ranking
                    and not row.critical_failures
                ),
                key=_display_order,
            )
        ),
    )


def _aggregate_candidates(root: Path) -> tuple[Path, ...]:
    """Find both legacy and canonical hashed aggregate publication names."""

    candidates: list[Path] = []
    root_is_aggregate_directory = root.name == "aggregates"
    for candidate in root.rglob("*.json"):
        relative_parts = candidate.relative_to(root).parts
        inside_aggregate_directory = (
            root_is_aggregate_directory or "aggregates" in relative_parts[:-1]
        )
        if candidate.name == "aggregate.json" or inside_aggregate_directory:
            candidates.append(candidate)
    return tuple(sorted(candidates))


def _entry_from_aggregate(
    aggregate: AggregateArtifact, source_path: Path
) -> LeaderboardEntry:
    jurisdiction = aggregate.configuration.jurisdiction.casefold()
    if jurisdiction not in {"uk", "us"}:
        raise ResultArtifactError("aggregate has no supported jurisdiction")
    is_reference = aggregate.kind == "scripted_reference"
    identifier = hashlib.sha256(source_path.read_bytes()).hexdigest()[:16]
    identity = aggregate.comparison_identity.model_dump(mode="json")
    configuration = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    cohort = hashlib.sha256(configuration.encode()).hexdigest()[:16]
    fresh_worlds = {run.fresh_world_id for run in aggregate.runs}
    baseline_complete = (
        aggregate.configuration.baseline
        and aggregate.run_count == 5
        and len(fresh_worlds) == 5
        and None not in fresh_worlds
        and all(run.status == "complete" for run in aggregate.runs)
    )
    return LeaderboardEntry(
        identifier=identifier,
        source_path=source_path,
        submission_name=(REFERENCE_DISPLAY_LABEL if is_reference else aggregate.model),
        organisation=None,
        jurisdiction=cast(Jurisdiction, jurisdiction),
        workflow=aggregate.episode_id,
        benchmark_version=aggregate.configuration.suite_version,
        scoring_version=aggregate.configuration.scoring_version,
        overall=_mean_overall(aggregate),
        accuracy=aggregate.layer_means.accounting,
        evidence=aggregate.layer_means.provenance,
        safety=aggregate.layer_means.safety,
        cost_usd=aggregate.usage_means.cost_usd
        if is_reference
        or all(
            getattr(run, "agent_result", {}).get("cost_measured") is True
            for run in aggregate.runs
        )
        else None,
        human_review="N/A (not measured by this benchmark version)",
        critical_failures=aggregate.critical_failure_ids,
        run_date=None,
        is_reference=is_reference,
        eligible_for_ranking=(
            not is_reference
            and not aggregate.critical_failure_ids
            and baseline_complete
        ),
        cohort=cohort,
        run_count=aggregate.run_count,
        configuration=configuration,
        reliability=json.dumps(aggregate.reliability, sort_keys=True),
    )


def _mean_overall(aggregate: AggregateArtifact) -> float:
    return sum(result.overall for result in aggregate.results) / len(aggregate.results)


def _rank(entries: list[LeaderboardEntry]) -> list[LeaderboardEntry]:
    """Rank only comparable workflow/version cohorts, with competition-style ties."""

    by_group: dict[str, list[LeaderboardEntry]] = {}
    for entry in entries:
        group = entry.cohort
        by_group.setdefault(group, []).append(entry)

    ranked: list[LeaderboardEntry] = []
    for group_entries in by_group.values():
        prior_score: float | None = None
        prior_rank = 0
        for position, entry in enumerate(
            sorted(group_entries, key=_ranking_order), start=1
        ):
            rank = (
                position
                if prior_score is None or entry.overall != prior_score
                else prior_rank
            )
            ranked.append(replace(entry, rank=rank))
            prior_score = entry.overall
            prior_rank = rank
    return sorted(ranked, key=_display_order)


def _ranking_order(entry: LeaderboardEntry) -> tuple[float, float, float, float, str]:
    return (
        -entry.overall,
        -entry.accuracy,
        -entry.evidence,
        -entry.safety,
        entry.identifier,
    )


def _display_order(entry: LeaderboardEntry) -> tuple[str, str, float, str]:
    return (entry.jurisdiction, entry.workflow, -entry.overall, entry.identifier)
