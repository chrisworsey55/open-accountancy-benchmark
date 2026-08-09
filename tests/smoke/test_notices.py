"""WP-01 attribution-file tests."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c"
DERIVED_FILES = (
    "mirrorfirm/harness/adapters/__init__.py",
    "mirrorfirm/harness/adapters/base.py",
    "mirrorfirm/harness/adapters/anthropic.py",
    "mirrorfirm/harness/adapters/openai.py",
    "mirrorfirm/harness/adapters/google.py",
    "mirrorfirm/harness/adapters/mistral.py",
    "mirrorfirm/harness/adapters/fireworks.py",
    "mirrorfirm/harness/agent_loop.py",
    "mirrorfirm/reporting/report.py",
)


def test_required_notice_files_exist() -> None:
    """The source notice and verbatim upstream licence are present."""

    assert (ROOT / "LICENSE").is_file()
    assert (ROOT / "NOTICE").is_file()
    assert (ROOT / "LICENSES" / "harvey-labs.MIT.txt").is_file()


def test_harvey_derived_files_are_noticed_and_attributed() -> None:
    """Every adapted Harvey file is listed and carries the required header."""

    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert SOURCE_COMMIT in notice
    for relative_path in DERIVED_FILES:
        contents = (ROOT / relative_path).read_text(encoding="utf-8")
        assert relative_path in notice
        assert "SPDX-License-Identifier: MIT" in contents
        assert "Derived from harveyai/harvey-labs (MIT), commit" in contents
        assert SOURCE_COMMIT in contents
