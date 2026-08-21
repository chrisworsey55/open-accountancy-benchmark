"""Run the credential-free, deterministic Mirror Firm reference demonstration."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Sequence

from mirrorfirm import cli
from mirrorfirm.reporting import ResultArtifactError, load_run_artifact

REFERENCE_LABEL = "reference (scripted) - not model performance"
EPISODE_ID = "epi-uk-01"
RUN_ID = "run-demo-reference"


class DemoError(ValueError):
    """The local offline demonstration could not complete safely."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic, credential-free Mirror Firm reference demo."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "a new empty directory for demo artifacts; defaults to a new directory "
            "under the operating-system temporary directory"
        ),
    )
    return parser


def _output_directory(value: Path | None) -> Path:
    """Choose a fresh output location without touching tracked repository files."""

    selected = value
    if selected is None:
        configured = os.environ.get("MIRRORFIRM_DEMO_OUTPUT_DIR")
        selected = Path(configured) if configured else None
    if selected is None:
        return Path(tempfile.mkdtemp(prefix="mirrorfirm-reference-demo-"))
    candidate = selected.expanduser().absolute()
    if candidate.exists() or candidate.is_symlink():
        raise DemoError("demo output directory must not already exist")
    return candidate


def run_demo(output_dir: Path) -> tuple[Path, Path, Path]:
    """Drive the public CLI through a reference run, aggregate, and scorecard."""

    run_exit = cli.main(
        [
            "run",
            "--episode",
            EPISODE_ID,
            "--model",
            "reference-scripted",
            "--results-root",
            str(output_dir),
            "--run-id",
            RUN_ID,
            "--json",
        ]
    )
    if run_exit != 0:
        raise DemoError("reference episode command failed")

    scorecard = output_dir / "reference-scorecard.html"
    report_exit = cli.main(
        ["report", str(output_dir), "--output", str(scorecard), "--json"]
    )
    if report_exit != 0:
        raise DemoError("reference scorecard command failed")

    run_directory = output_dir / "runs" / EPISODE_ID / "reference-scripted" / RUN_ID
    try:
        artifact, evaluation = load_run_artifact(run_directory)
    except ResultArtifactError as error:
        raise DemoError("demo run artifact could not be verified") from error
    if artifact.model != REFERENCE_LABEL or not evaluation.all_pass:
        raise DemoError("demo did not produce the expected scripted reference result")
    if evaluation.critical_failures:
        raise DemoError("demo reference has critical failures")

    aggregates = tuple(sorted((output_dir / "aggregates").rglob("*.json")))
    if len(aggregates) != 1 or not scorecard.is_file():
        raise DemoError("demo artifacts are incomplete")
    return run_directory, aggregates[0], scorecard


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demo and print stable, user-facing artifact locations."""

    arguments = _parser().parse_args(argv)
    try:
        output_dir = _output_directory(arguments.output_dir)
        run_directory, aggregate, scorecard = run_demo(output_dir)
        _, evaluation = load_run_artifact(run_directory)
    except (DemoError, OSError, ValueError) as error:
        print(f"mirror-firm demo: {error}")
        return 2

    print("Mirror Firm offline demonstration")
    print(f"Run kind: {REFERENCE_LABEL}")
    print(
        "Result: "
        f"overall={evaluation.overall:.3f}; all_pass={evaluation.all_pass}; "
        f"critical_failures={len(evaluation.critical_failures)}"
    )
    print(f"Artifacts: {output_dir}")
    print(f"Run: {run_directory}")
    print(f"Aggregate JSON: {aggregate}")
    print(f"Offline HTML scorecard: {scorecard}")
    return 0


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
