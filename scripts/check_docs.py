"""Small dependency-free documentation build check for the v0.1 release packet."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCUMENTS: dict[str, tuple[str, ...]] = {
    "README.md": (
        "Franklin & McGrath",
        "reference (scripted) - not model performance",
        "make demo",
    ),
    "SECURITY.md": ("Threat model", "Known limitations"),
    "TRADEMARKS.md": ("Franklin & McGrath", "Third-party marks"),
    "CONTRIBUTING.md": ("fictional", "tests"),
    "docs/architecture.md": ("Data flow", "StateSnapshot"),
    "docs/authoring-worlds.md": ("fictional", "validate"),
    "docs/adding-jurisdictions.md": ("jurisdiction", "deterministic"),
    "docs/evaluation.md": ("Critical failures", "Pass^k"),
    "docs/safety-model.md": ("untrusted", "approval"),
    "docs/tool-mcp.md": ("37", "MCP"),
    "docs/running-models.md": ("five", "reference (scripted) - not model performance"),
    "docs/reporting.md": ("compare", "sweep"),
    "docs/applied-boundary.md": ("Applied", "private"),
    "docs/development.md": ("uv", "deterministic"),
    "docs/apex-compat.md": ("contamination", "not comparable"),
    "docs/LAUNCH_AUDIT_2026-09-01.md": ("Status", "Codex fixed"),
    "docs/LAUNCH_CHECKLIST_2026-09-01.md": ("Before publishing", "Do not launch"),
    "docs/LAUNCH_COPY.md": ("Developer alpha", "Practitioner review"),
    "docs/LAUNCH_STATUS.md": ("Evidence gates", "Real-agent baselines"),
    "docs/learning-contract.md": ("HumanEffort", "applied-only"),
    "docs/LEADERBOARD_METHODOLOGY.md": (
        "Reference (scripted) - not model performance",
        "Critical failures",
    ),
    "docs/LEGACY_NAMING.md": ("Retained legacy naming", "mirrorfirm"),
    "docs/OPEN_SOURCE_BOUNDARY.md": ("Public preparation scope", "Private by design"),
    "docs/V1_SCOPE_FREEZE_2026-09-01.md": ("Proposition", "Explicitly excluded"),
}


def _check_document(path: Path, required_text: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing documentation file: {path.relative_to(ROOT)}"]
    text = path.read_text(encoding="utf-8")
    if "WP-01 scaffold only" in text:
        errors.append(f"stale scaffold-only text in {path.relative_to(ROOT)}")
    for phrase in required_text:
        if phrase.casefold() not in text.casefold():
            errors.append(
                f"{path.relative_to(ROOT)} is missing required release text {phrase!r}"
            )
    for target in re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", text):
        if "://" in target or target.startswith("mailto:"):
            continue
        destination = (path.parent / target).resolve()
        if not destination.is_file():
            errors.append(
                f"broken local documentation link {target!r} in {path.relative_to(ROOT)}"
            )
    return errors


def check_docs(root: Path = ROOT) -> tuple[str, ...]:
    """Return deterministic validation errors for the authored release documents."""

    if root != ROOT:
        raise ValueError("documentation checker must run from this repository")
    errors = [
        error
        for relative, required in sorted(REQUIRED_DOCUMENTS.items())
        for error in _check_document(ROOT / relative, required)
    ]
    return tuple(errors)


def main() -> int:
    """Run the local no-network documentation build gate."""

    errors = check_docs()
    if errors:
        print("documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"documentation check passed: {len(REQUIRED_DOCUMENTS)} files")
    return 0


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
