"""Launch probes exercise accounting meaning through normal tools and snapshots."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from mirrorfirm.episodes.manifests import (
    load_uk_episode_manifests,
    reference_qualitative_expectations,
)
from mirrorfirm.episodes.references import ScriptedReferenceAdapter, reference_calls_for
from mirrorfirm.evaluation import evaluate_run
from mirrorfirm.evaluation.qualitative import (
    REFERENCE_VALIDATION_MODEL,
    ReferenceQualitativeValidator,
)
from mirrorfirm.harness import EpisodeRunner
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("control", True),
        ("shift_balances", False),
        ("wrong_residual", False),
        ("wrong_outstanding", False),
        ("unrelated_evidence", False),
        ("negation", True),
        ("paraphrase", True),
        ("reverse_lines", True),
        ("wrong_journal_amount", False),
    ],
)
def test_accounting_meaning_is_not_reference_formatting(
    tmp_path: Path, variant: str, expected: bool
) -> None:
    eid = (
        "epi-uk-03"
        if variant in {"reverse_lines", "wrong_journal_amount"}
        else "epi-uk-02"
    )
    episode = next(e for e in load_uk_episode_manifests() if e.episode_id == eid)
    calls = []
    for call in reference_calls_for(eid):
        original = call.arguments
        if call.call_id == "create_recon":

            def change(results, original=original):
                args = copy.deepcopy(
                    original(results) if callable(original) else original
                )
                body = args["body"]
                if variant == "shift_balances":
                    body["statement_end_minor"] += 100000
                    body["ledger_end_minor"] += 100000
                elif variant == "wrong_residual":
                    body["unresolved"][0]["amount_minor"] = 1200
                elif variant == "wrong_outstanding":
                    body["outstanding"][0]["amount_minor"] = 17500
                elif variant == "unrelated_evidence":
                    body["outstanding"][0]["provenance_refs"] = [
                        "doc-kestrel-statement"
                    ]
                elif variant == "paraphrase":
                    body["outstanding"][0]["reason"] = (
                        "Issued cheque awaiting presentation."
                    )
                    body["unresolved"][0]["description"] = (
                        "Unidentified credit requires investigation."
                    )
                    body["unresolved"][0]["provenance_refs"].reverse()
                return args

            call = replace(call, arguments=change)
        if call.call_id == "finish" and variant == "negation":

            def finish(results, original=original):
                args = copy.deepcopy(original(results))
                args["summary"] += " No plug journal was proposed."
                return args

            call = replace(call, arguments=finish)
        if call.call_id == "propose_correction":
            args = copy.deepcopy(original)
            if variant == "reverse_lines":
                args["lines"].reverse()
            if variant == "wrong_journal_amount":
                for line in args["lines"]:
                    line["amount_minor"] += 100
            call = replace(call, arguments=args)
        calls.append(call)
    world = ROOT / "worlds/uk-wyrley-brook"
    compiled = compile_world(world, tmp_path / "compiled.db")
    run = EpisodeRunner(
        episode,
        compiled.database_path,
        world_root=world,
        results_root=tmp_path / "runs",
    ).run(ScriptedReferenceAdapter(calls), run_id="regression")
    result = evaluate_run(
        episode,
        run.initial_snapshot,
        run.final_snapshot,
        run.agent_result,
        model="reference (scripted) - not model performance",
        qualitative_judge=ReferenceQualitativeValidator(
            reference_qualitative_expectations(episode)
        ),
        judge_models=[REFERENCE_VALIDATION_MODEL],
        reference_validation=True,
    )
    assert run.completed
    assert result.all_pass is expected
    if not expected:
        assert any(
            not criterion.passed
            and criterion.criterion_id in {"recon_ties", "journal_exact"}
            for criterion in result.criterion_results
        )
