"""WP-10 regression coverage for authored UK episode references."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.core.models import (
    BankTransaction,
    Journal,
    JudgeCriterion,
    Message,
    Workpaper,
)
from mirrorfirm.episodes import (
    answer_key_path,
    load_answer_key,
    load_episode_manifest,
    load_uk_episode_manifests,
    run_reference_episode,
    validate_answer_key,
)
from mirrorfirm.episodes.manifests import reference_qualitative_expectations
from mirrorfirm.episodes.references import (
    ReferenceCall,
    ScriptedReferenceAdapter,
    reference_calls_for,
)
from mirrorfirm.evaluation import evaluate_run
from mirrorfirm.evaluation.qualitative import (
    REFERENCE_VALIDATION_MODEL,
    ReferenceQualitativeValidator,
)
from mirrorfirm.evaluation.state import ClientScopeIndex, Deliverables, ProvenanceGraph
from mirrorfirm.harness.episode_runner import EpisodeRunner
from mirrorfirm.worldgen import compile_world, validate_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"
EPISODES = ROOT / "episodes" / "uk"


def test_uk_episode_catalogue_loads_all_four_authored_manifests() -> None:
    """UK reference validation starts from four typed episode manifests."""

    manifests = load_uk_episode_manifests()

    assert [manifest.episode_id for manifest in manifests] == [
        "epi-uk-01",
        "epi-uk-02",
        "epi-uk-03",
        "epi-uk-04",
    ]


@pytest.mark.parametrize(
    "filename", [f"ep-uk-0{number}.yaml" for number in range(1, 5)]
)
def test_each_uk_answer_key_exactly_covers_its_manifest(filename: str) -> None:
    """Answer-key validation detects omissions and authored expectation drift."""

    manifest_path = EPISODES / filename
    manifest = load_episode_manifest(manifest_path)

    validate_answer_key(manifest, load_answer_key(answer_key_path(manifest_path)))


@pytest.mark.parametrize("episode_id", [f"epi-uk-0{number}" for number in range(1, 5)])
def test_each_uk_reference_runs_through_runner_and_graders_at_full_score(
    tmp_path: Path, episode_id: str
) -> None:
    """Every UK reference uses the WP-08 runner and WP-09 evaluator end to end."""

    manifest = next(
        item for item in load_uk_episode_manifests() if item.episode_id == episode_id
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")

    result = run_reference_episode(
        manifest,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    assert result.run.completed
    assert result.evaluation.critical_failures == []
    assert result.evaluation.overall == 1.0
    assert result.evaluation.all_pass
    assert (
        tmp_path / "results" / f"run-reference-{episode_id}" / "scores.json"
    ).is_file()


def test_full_uk_world_validation_runs_reference_and_answer_key_gates() -> None:
    """The WP-05 deferred gates are now executable for the authored UK world."""

    report = validate_world(WORLD)

    assert report.passed
    assert [(gate.gate_id, gate.status) for gate in report.gates][-2:] == [
        ("reference_runs", "passed"),
        ("answer_key_completeness", "passed"),
    ]


def test_reference_qualitative_validation_rejects_an_off_topic_target(
    tmp_path: Path,
) -> None:
    """Reference self-validation must inspect its target, never pass unconditionally."""

    source = next(
        item for item in load_uk_episode_manifests() if item.episode_id == "epi-uk-02"
    )
    altered = source.model_copy(
        update={
            "reference": source.reference.model_copy(
                update={
                    "final_state_digest": "397ff9acc25af12e1e9fc839e720816c4b0b892e83f2b98370c156252156e9eb"
                }
            ),
            "qualitative_criteria": [
                JudgeCriterion(
                    id="escalation_clarity",
                    prompt="Is this a clear escalation?",
                    target="final_summary",
                    scale="binary",
                    weight=1.0,
                )
            ],
        }
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")

    result = run_reference_episode(
        altered,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    assert not result.evaluation.all_pass
    criterion = next(
        item
        for item in result.evaluation.criterion_results
        if item.criterion_id == "escalation_clarity"
    )
    assert not criterion.passed
    assert "reference validation" in criterion.detail


def test_all_zero_rated_ep_uk_01_trajectory_fails_complete_classification_grading(
    tmp_path: Path,
) -> None:
    """Valid MCP calls cannot substitute zero-rated tax for required standard VAT."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == "epi-uk-01"
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    run = runner.run(
        ScriptedReferenceAdapter(
            _zero_rate_classifications(reference_calls_for(episode.episode_id))
        ),
        run_id="run-zero-rated-attack",
    )
    validator = ReferenceQualitativeValidator(
        reference_qualitative_expectations(episode)
    )
    evaluation = evaluate_run(
        episode,
        run.initial_snapshot,
        run.final_snapshot,
        run.agent_result,
        model="reference (scripted) - not model performance",
        qualitative_judge=validator,
        judge_models=[REFERENCE_VALIDATION_MODEL],
        reference_validation=True,
    )

    assert run.completed
    with SQLiteWorldView.open(run.final_snapshot.db_path) as final:
        assert all(
            not (
                isinstance(action.output_payload, dict)
                and "error" in action.output_payload
            )
            for action in final.actions()
        )
    classification = next(
        result
        for result in evaluation.criterion_results
        if result.criterion_id == "classification_map"
    )
    assert not classification.passed
    assert evaluation.all_pass is False


@pytest.mark.parametrize("episode_id", [f"epi-uk-0{number}" for number in range(1, 5)])
def test_reference_mutation_evidence_is_first_discoverable_through_allowed_tools(
    tmp_path: Path, episode_id: str
) -> None:
    """Every source used by a reference mutation has an earlier visible tool path."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == episode_id
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    result = run_reference_episode(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    with SQLiteWorldView.open(result.run.final_snapshot.db_path) as final:
        assert _sources_are_discoverable(episode.allowed_tools, final.actions())


def test_evidence_discoverability_fails_if_a_required_read_path_is_removed(
    tmp_path: Path,
) -> None:
    """The guard fails before a manifest can hide a necessary source from a model."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == "epi-uk-01"
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    result = run_reference_episode(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    removed = [tool for tool in episode.allowed_tools if tool != "aggregate_table"]
    with SQLiteWorldView.open(result.run.final_snapshot.db_path) as final:
        assert not _sources_are_discoverable(removed, final.actions())


def test_reference_answer_keys_never_enter_model_visible_context(
    tmp_path: Path,
) -> None:
    """Gold expectations load after the scripted adapter has received its prompts."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == "epi-uk-03"
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    adapter = ScriptedReferenceAdapter(reference_calls_for(episode.episode_id))
    runner = EpisodeRunner(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    run = runner.run(adapter, run_id="run-no-answer-key-context")
    visible_context = json.dumps(adapter.observed_messages, ensure_ascii=False)

    assert run.completed
    assert "qualitative_expectations" not in visible_context
    assert "answer-keys" not in visible_context
    assert "required_text" not in visible_context


_BANK_DISCOVERY_TOOLS = frozenset({"aggregate_table", "list_bank_transactions"})
_DOCUMENT_DISCOVERY_TOOLS = frozenset(
    {"list_documents", "search_documents", "read_document"}
)
_THREAD_DISCOVERY_TOOLS = frozenset({"list_threads", "read_thread"})
_MUTATION_TOOLS = frozenset(
    {
        "propose_classification",
        "propose_journal",
        "create_workpaper",
        "draft_information_request",
        "draft_reply",
    }
)


def _sources_are_discoverable(
    allowed_tools: list[str], actions: tuple[object, ...]
) -> bool:
    """Check source refs in the immutable Action log against earlier model-visible calls."""

    allowed = frozenset(allowed_tools)
    visible: set[str] = set()
    for action in actions:
        tool = getattr(action, "tool")
        if tool.startswith("event."):
            continue
        if tool not in allowed:
            return False
        if tool in _MUTATION_TOOLS:
            refs = _source_refs(getattr(action, "input_payload"))
            if any(ref.startswith("btx-") for ref in refs) and not (
                visible & _BANK_DISCOVERY_TOOLS
            ):
                return False
            if any(ref.startswith("doc-") for ref in refs) and not (
                visible & _DOCUMENT_DISCOVERY_TOOLS
            ):
                return False
            if any(ref.startswith("thr-") for ref in refs) and not (
                visible & _THREAD_DISCOVERY_TOOLS
            ):
                return False
        visible.add(tool)
    return True


def _source_refs(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for nested in value for item in _source_refs(nested)}
    if isinstance(value, dict):
        return {item for nested in value.values() for item in _source_refs(nested)}
    return set()


def _zero_rate_classifications(
    calls: tuple[ReferenceCall, ...],
) -> tuple[ReferenceCall, ...]:
    def replace_tax(value: object) -> object:
        if isinstance(value, list):
            return [replace_tax(item) for item in value]
        if not isinstance(value, dict):
            return value
        replaced = {key: replace_tax(item) for key, item in value.items()}
        tax = replaced.get("tax")
        if isinstance(tax, dict) and tax.get("code") == "20-std":
            replaced["tax"] = {"kind": "uk_vat", "code": "0-zero", "rate_bp": 0}
        return replaced

    def altered_arguments(call: ReferenceCall):
        def build(results: dict[str, object]) -> dict[str, object]:
            source = (
                call.arguments(results) if callable(call.arguments) else call.arguments
            )
            replaced = replace_tax(source)
            assert isinstance(replaced, dict)
            return replaced

        return build

    return tuple(
        replace(call, arguments=altered_arguments(call))
        if call.name == "propose_classification"
        else call
        for call in calls
    )


@pytest.mark.parametrize("episode_id", [f"epi-uk-0{number}" for number in range(1, 5)])
def test_each_uk_reference_is_deterministic_across_two_fresh_runs(
    tmp_path: Path, episode_id: str
) -> None:
    """A pinned reference trajectory produces the same logical state twice."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == episode_id
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    first = run_reference_episode(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
        run_id=f"run-first-{episode_id}",
    )
    second = run_reference_episode(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
        run_id=f"run-second-{episode_id}",
    )

    assert first.run.final_snapshot.state_digest == episode.reference.final_state_digest
    assert (
        second.run.final_snapshot.state_digest == episode.reference.final_state_digest
    )
    assert (
        first.run.final_snapshot.state_digest == second.run.final_snapshot.state_digest
    )
    assert first.evaluation.overall == second.evaluation.overall == 1.0
    assert not first.evaluation.critical_failures
    assert not second.evaluation.critical_failures


@pytest.mark.parametrize("episode_id", [f"epi-uk-0{number}" for number in range(1, 5)])
def test_each_uk_reference_keeps_snapshots_actions_provenance_and_deliverables_consistent(
    tmp_path: Path, episode_id: str
) -> None:
    """Reference artifacts remain internally coherent across the integration boundary."""

    episode = next(
        item for item in load_uk_episode_manifests() if item.episode_id == episode_id
    )
    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    result = run_reference_episode(
        episode,
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    with (
        SQLiteWorldView.open(result.run.initial_snapshot.db_path) as initial,
        SQLiteWorldView.open(result.run.final_snapshot.db_path) as final,
    ):
        actions = final.actions()
        deliverables = Deliverables.from_actions(actions)
        index = ClientScopeIndex.from_world(final)
        provenance = ProvenanceGraph.from_world(final)
        final_references = {
            *{journal.id for journal in final.list(Journal)},
            *{message.id for message in final.list(Message)},
            *{workpaper.id for workpaper in final.list(Workpaper)},
        }

        assert initial.state_digest() == result.run.initial_snapshot.state_digest
        assert final.state_digest() == result.run.final_snapshot.state_digest
        assert initial.state_digest() != final.state_digest()
        assert actions[-1].tool == "finish_episode"
        assert set(deliverables.references).issubset(final_references)
        assert all(
            index.has_reference(record.subject_ref)
            and index.has_reference(record.basis_ref)
            for record in provenance.records
        )
        if episode_id == "epi-uk-02":
            assert (
                final.get(BankTransaction, "btx-kestrel-005").reconciliation_status
                == "flagged"
            )
