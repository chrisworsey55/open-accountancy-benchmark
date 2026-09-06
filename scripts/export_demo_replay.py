"""Create a public, fictional reference replay from a verified persisted run."""

from __future__ import annotations

import argparse
from pathlib import Path

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.reporting.artifacts import (
    REFERENCE_DISPLAY_LABEL,
    load_run_artifact,
    write_json_atomic,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, score = load_run_artifact(args.run)
    if (
        run.run_kind != "scripted_reference"
        or run.status != "complete"
        or not score.all_pass
    ):
        raise ValueError("a replay requires a passing, explicitly scripted reference")
    directory = args.run.parent if args.run.is_file() else args.run
    with SQLiteWorldView.open(directory / run.final_snapshot.relative_path) as final:
        steps = [
            {
                "id": action.id,
                "tool": action.tool,
                "inputs": action.input_payload,
                "changes": [
                    mutation.model_dump(mode="json") for mutation in action.mutations
                ],
            }
            for action in final.actions()
        ]
    payload = {
        "label": REFERENCE_DISPLAY_LABEL,
        "episode_id": run.episode_id,
        "world_version": run.world_version,
        "scoring_version": run.configuration.scoring_version,
        "final_state_digest": run.final_snapshot.state_digest,
        "overall": score.overall,
        "critical_failures": len(score.critical_failures),
        "criteria": [
            criterion.model_dump(mode="json") for criterion in score.criterion_results
        ],
        "steps": steps,
    }
    write_json_atomic(args.output, payload)


if __name__ == "__main__":
    main()
