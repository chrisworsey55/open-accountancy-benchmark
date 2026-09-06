"""WP-09 deterministic grader registry and expected-state regression coverage."""

from __future__ import annotations

from conftest import SnapshotView, base_records, journal

from mirrorfirm.core.models import ProvenanceRecord
from mirrorfirm.evaluation.deterministic import (
    GRADER_IDS,
    get_grader,
    grade_registered,
)
from mirrorfirm.evaluation.state import Deliverables, ProvenanceGraph


def test_registry_exposes_every_specified_deterministic_grader() -> None:
    """The §H registry is complete, stable, and individually addressable."""

    expected = {
        "classification_map",
        "journal_exact",
        "recon_ties",
        "unresolved_flagged",
        "provenance_complete",
        "provenance_valid",
        "expected_state",
        "approval_path",
        "no_unauthorized_mutation",
        "isolation",
        "duplicate_guard",
        "message_equals_approved_draft",
        "summary_consistency",
    }

    assert GRADER_IDS == expected
    assert {get_grader(grader_id).grader_id for grader_id in GRADER_IDS} == expected


def test_every_registered_grader_executes_against_immutable_snapshot_state() -> None:
    """All registry entries have a concrete pure implementation, not a placeholder."""

    view = SnapshotView(base_records())
    params = {
        "classification_map": {},
        "journal_exact": {},
        "recon_ties": {},
        "unresolved_flagged": {"min_items": 0},
        "provenance_complete": {},
        "provenance_valid": {},
        "expected_state": {"assertions": []},
        "approval_path": {},
        "no_unauthorized_mutation": {},
        "isolation": {},
        "duplicate_guard": {},
        "message_equals_approved_draft": {},
        "summary_consistency": {},
    }
    provenance = ProvenanceGraph.from_world(view)
    deliverables = Deliverables(summary=None, references=(), unresolved_items=())

    results = {
        grader_id: grade_registered(
            grader_id,
            view,
            view,
            [],
            provenance,
            deliverables,
            params[grader_id],
        )
        for grader_id in GRADER_IDS
    }

    assert set(results) == GRADER_IDS
    assert all(result.passed for result in results.values())


def test_journal_exact_checks_the_status_and_each_posted_line() -> None:
    """The accounting grader compares full journal content, not only its identifier."""

    expected = journal()
    final = SnapshotView([*base_records(), expected])
    result = grade_registered(
        "journal_exact",
        final,
        final,
        [],
        ProvenanceGraph.from_world(final),
        Deliverables(summary=None, references=(), unresolved_items=()),
        {
            "expected_journals": {
                expected.id: {
                    "status": expected.status,
                    "lines": [line.model_dump(mode="json") for line in expected.lines],
                }
            }
        },
    )

    assert result.passed is True


def test_semantic_journal_match_checks_evidence_on_actual_output_id() -> None:
    """Generated identity is immaterial; the actual journal still needs its evidence."""
    actual = journal(identifier="jnl-agent-generated")
    initial = SnapshotView(base_records())
    record = ProvenanceRecord(
        id="prov-fictional",
        subject_ref=actual.id,
        basis_ref="doc-fictional-source",
        relation="supported_by",
    )
    final = SnapshotView([*base_records(), actual, record])
    params = {
        "match_created_by_content": True,
        "expected_journals": {
            "jnl-reference-generated": {
                "status": actual.status,
                "lines": [line.model_dump(mode="json") for line in actual.lines],
                "provenance_refs": [record.basis_ref],
            }
        },
    }
    for graph, passes in [
        (ProvenanceGraph.from_world(final), True),
        (ProvenanceGraph(records=()), False),
    ]:
        result = grade_registered(
            "journal_exact",
            initial,
            final,
            [],
            graph,
            Deliverables(summary=None, references=(), unresolved_items=()),
            params,
        )
        assert result.passed is passes
