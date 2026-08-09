"""Shared fixture world for the bounded WP-01--WP-07 remediation regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirrorfirm.core.db import WorldStore
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"


@pytest.fixture
def engine(tmp_path: Path) -> WorldToolEngine:
    """Open a fresh fictional BrightPath tool session for each regression."""

    compiled = compile_world(WORLD, tmp_path / "world.db")
    store = WorldStore.open(compiled.database_path)
    try:
        yield WorldToolEngine(
            store,
            world_root=WORLD,
            actor_id="per-agent",
            engagement_id="eng-brightpath-bookkeeping",
        )
    finally:
        store.close()
