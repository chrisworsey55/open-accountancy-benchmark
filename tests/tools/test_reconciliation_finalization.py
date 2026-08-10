"""Regression coverage for the derived Kestrel reconciliation-flag effect."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.core.models import Action, BankTransaction, Journal, Workpaper
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"


@pytest.fixture
def kestrel_engine(tmp_path: Path) -> WorldToolEngine:
    compiled = compile_world(WORLD, tmp_path / "world.db")
    store = WorldStore.open(compiled.database_path)
    try:
        yield WorldToolEngine(
            store,
            world_root=WORLD,
            actor_id="per-agent",
            engagement_id="eng-kestrel-bookkeeping",
        )
    finally:
        store.close()


def test_finalising_reconciliation_flags_referenced_duplicate_without_posting(
    kestrel_engine: WorldToolEngine,
) -> None:
    """The derived status is transactional metadata, not a new stable tool."""

    created = _call(kestrel_engine, "create_workpaper", _kestrel_reconciliation())
    workpaper_id = created["workpaper"]["id"]
    finalized = _call(
        kestrel_engine,
        "finalize_workpaper",
        {"workpaper_id": workpaper_id},
    )

    duplicate = kestrel_engine.store.load(BankTransaction, "btx-kestrel-005")
    workpaper = kestrel_engine.store.load(Workpaper, workpaper_id)
    final_action = kestrel_engine.store.load(Action, finalized["action_id"])

    assert duplicate.reconciliation_status == "flagged"
    assert duplicate.classification_status == "unclassified"
    assert workpaper.status == "final"
    assert workpaper.body.outstanding[0].ref == "doc-kestrel-outstanding-cheque"
    assert workpaper.body.unresolved[0].amount_minor == 1240
    assert not [
        journal
        for journal in kestrel_engine._list(Journal)  # noqa: SLF001
        if journal.source == "adjustment"
    ]
    assert any(
        mutation.entity_kind == "BankTransaction"
        and mutation.entity_id == duplicate.id
        and mutation.change == "status_changed"
        for mutation in final_action.mutations
    )


def test_cross_engagement_reconciliation_provenance_fails_safely(
    kestrel_engine: WorldToolEngine,
) -> None:
    """A Kestrel workpaper cannot flag a transaction from another engagement."""

    invalid = _kestrel_reconciliation()
    invalid["body"]["unresolved"][0]["provenance_refs"] = ["btx-brightpath-001"]

    result = kestrel_engine.call("create_workpaper", invalid)

    assert not result.ok
    assert result.error is not None and result.error.code == "SCOPE_VIOLATION"
    assert (
        kestrel_engine.store.load(
            BankTransaction, "btx-kestrel-005"
        ).reconciliation_status
        == "unmatched"
    )


def _kestrel_reconciliation() -> dict[str, object]:
    return {
        "task_id": "tsk-kestrel-may-recon",
        "body": {
            "kind": "bank_reconciliation",
            "bank_account_id": "bnk-kestrel",
            "period_id": "prd-kestrel-may",
            "statement_end_minor": 82400,
            "ledger_end_minor": 100000,
            "outstanding": [
                {
                    "ref": "doc-kestrel-outstanding-cheque",
                    "amount_minor": -17600,
                    "reason": "Outstanding cheque.",
                    "provenance_refs": ["doc-kestrel-outstanding-cheque"],
                }
            ],
            "unresolved": [
                {
                    "description": "Explicit £12.40 residual and duplicate feed line.",
                    "amount_minor": 1240,
                    "provenance_refs": [
                        "doc-kestrel-residual-difference",
                        "btx-kestrel-005",
                    ],
                }
            ],
        },
    }


def _call(
    engine: WorldToolEngine, name: str, payload: dict[str, object]
) -> dict[str, object]:
    result = engine.call(name, payload)
    assert result.ok, result.error
    assert isinstance(result.result, dict)
    assert result.action_id is not None
    return {**result.result, "action_id": result.action_id}
