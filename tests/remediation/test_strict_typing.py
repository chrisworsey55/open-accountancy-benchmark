"""Regression coverage for strict typing without package-level suppressions."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_production_mypy_configuration_has_no_package_diagnostic_suppressions() -> None:
    """Completed production modules must not hide strict-mode diagnostics."""

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    configuration = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    overrides = configuration["tool"]["mypy"].get("overrides", [])
    assert overrides == []
