"""WP-01 package import smoke tests."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "mirrorfirm",
        "mirrorfirm.core",
        "mirrorfirm.core.models",
        "mirrorfirm.worldgen",
        "mirrorfirm.tools",
        "mirrorfirm.harness",
        "mirrorfirm.harness.agent_loop",
        "mirrorfirm.harness.adapters",
        "mirrorfirm.harness.adapters.anthropic",
        "mirrorfirm.harness.adapters.openai",
        "mirrorfirm.harness.adapters.google",
        "mirrorfirm.harness.adapters.mistral",
        "mirrorfirm.harness.adapters.fireworks",
        "mirrorfirm.evaluation",
        "mirrorfirm.evaluation.deterministic",
        "mirrorfirm.evaluation.qualitative",
        "mirrorfirm.reporting",
        "mirrorfirm.reporting.report",
        "mirrorfirm.packs",
        "mirrorfirm.packs.apex_accounting",
        "mirrorfirm.learning",
    ],
)
def test_package_modules_import(module_name: str) -> None:
    """All WP-01 package modules import with their declared dependencies installed."""

    assert importlib.import_module(module_name)
