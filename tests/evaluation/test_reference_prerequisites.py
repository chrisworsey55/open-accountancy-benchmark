"""Regression tests for WP-10's grader prerequisites."""

from __future__ import annotations

from datetime import date

from conftest import NOW, SnapshotView, action, base_records

from mirrorfirm.core.models import (
    Document,
    Journal,
    JournalLine,
    Mutation,
    ProvenanceRecord,
)
from mirrorfirm.evaluation.deterministic import grade_registered
from mirrorfirm.evaluation.state import Deliverables, ProvenanceGraph


def test_classification_map_uses_counter_accounts_for_ordinary_vat_and_split_journals() -> (
    None
):
    """The bank control line is never a classification answer account."""

    ordinary = _classification_journal(
        "jnl-ordinary",
        "btx-ordinary",
        ["acc-expense"],
    )
    vat = _classification_journal(
        "jnl-vat",
        "btx-vat",
        ["acc-travel", "acc-vat-input"],
    )
    split = _classification_journal(
        "jnl-split",
        "btx-split",
        ["acc-materials", "acc-equipment"],
    )
    view = SnapshotView([*base_records(), ordinary, vat, split])

    result = grade_registered(
        "classification_map",
        view,
        view,
        [],
        ProvenanceGraph.from_world(view),
        _deliverables(),
        {
            "expected_accounts": {
                "btx-ordinary": "acc-expense",
                "btx-vat": "acc-travel",
                "btx-split": "acc-equipment",
            }
        },
    )

    assert result.passed


def test_classification_map_rejects_the_bank_control_line_as_a_classification_answer() -> (
    None
):
    """A malformed key cannot pass merely because every journal links its bank line."""

    journal = _classification_journal(
        "jnl-ordinary",
        "btx-ordinary",
        ["acc-expense"],
    )
    view = SnapshotView([*base_records(), journal])

    result = grade_registered(
        "classification_map",
        view,
        view,
        [],
        ProvenanceGraph.from_world(view),
        _deliverables(),
        {"expected_accounts": {"btx-ordinary": "acc-bank"}},
    )

    assert not result.passed


def test_provenance_complete_ignores_untouched_historical_journals() -> None:
    """Pre-episode state does not become an agent provenance obligation."""

    historical = _classification_journal(
        "jnl-historical",
        "btx-historical",
        ["acc-expense"],
    )
    view = SnapshotView([*base_records(), historical])

    result = grade_registered(
        "provenance_complete",
        view,
        view,
        [],
        ProvenanceGraph.from_world(view),
        _deliverables(),
        {},
    )

    assert result.passed


def test_provenance_complete_requires_evidence_for_episode_created_outputs() -> None:
    """A new non-opening journal remains a provenance obligation."""

    created = _classification_journal(
        "jnl-created",
        "btx-created",
        ["acc-expense"],
    )
    initial = SnapshotView(base_records())
    final_without_basis = SnapshotView([*base_records(), created])
    created_action = action("propose_journal").model_copy(
        update={
            "mutations": [
                Mutation(
                    entity_kind="Journal",
                    entity_id=created.id,
                    change="created",
                    summary="episode proposed journal",
                )
            ]
        }
    )

    missing = grade_registered(
        "provenance_complete",
        initial,
        final_without_basis,
        [created_action],
        ProvenanceGraph.from_world(final_without_basis),
        _deliverables(),
        {},
    )

    proven = ProvenanceRecord(
        id="prv-created",
        subject_ref=created.id,
        basis_ref="doc-fictional-a",
        relation="supported_by",
    )
    final_with_basis = SnapshotView([*base_records(), created, proven])
    complete = grade_registered(
        "provenance_complete",
        initial,
        final_with_basis,
        [created_action],
        ProvenanceGraph.from_world(final_with_basis),
        _deliverables(),
        {},
    )

    assert not missing.passed
    assert complete.passed


def test_provenance_complete_honours_explicit_manifest_targets() -> None:
    """An author may explicitly require provenance for an unchanged target."""

    historical = _classification_journal(
        "jnl-targeted",
        "btx-targeted",
        ["acc-expense"],
    )
    view = SnapshotView([*base_records(), historical])

    result = grade_registered(
        "provenance_complete",
        view,
        view,
        [],
        ProvenanceGraph.from_world(view),
        _deliverables(),
        {"target_refs": [historical.id]},
    )

    assert not result.passed


def test_provenance_valid_rejects_missing_and_cross_client_episode_bases() -> None:
    """The narrowed completeness scope does not permit fabricated provenance."""

    created = _classification_journal(
        "jnl-created",
        "btx-created",
        ["acc-expense"],
    )
    documents = [
        _document("doc-fictional-a", "cli-fictional-a"),
        _document("doc-fictional-b", "cli-fictional-b"),
    ]
    for basis_ref in ("doc-missing", "doc-fictional-b"):
        record = ProvenanceRecord(
            id=f"prv-{basis_ref}",
            subject_ref=created.id,
            basis_ref=basis_ref,
            relation="supported_by",
        )
        view = SnapshotView([*base_records(), *documents, created, record])
        result = grade_registered(
            "provenance_valid",
            view,
            view,
            [],
            ProvenanceGraph.from_world(view),
            _deliverables(),
            {},
        )
        assert not result.passed


def _classification_journal(
    identifier: str, transaction_id: str, counter_accounts: list[str]
) -> Journal:
    return Journal(
        id=identifier,
        entity_id="ent-fictional-a",
        date=date(2026, 5, 28),
        memo="Fictional classification",
        source="feed_classification",
        status="proposed",
        lines=[
            JournalLine(
                account_id="acc-bank",
                direction="cr",
                amount_minor=100 * len(counter_accounts),
                currency="GBP",
                bank_transaction_id=transaction_id,
            ),
            *[
                JournalLine(
                    account_id=account_id,
                    direction="dr",
                    amount_minor=100,
                    currency="GBP",
                )
                for account_id in counter_accounts
            ],
        ],
        proposed_by="per-agent",
        approval_id=None,
    )


def _document(identifier: str, client_id: str) -> Document:
    return Document(
        id=identifier,
        sha256="a" * 64,
        filename=f"{identifier}.txt",
        mime="text/plain",
        kind="receipt",
        client_id=client_id,
        source="fixture",
        received_world_time=NOW,
    )


def _deliverables() -> Deliverables:
    return Deliverables(summary=None, references=(), unresolved_items=())
