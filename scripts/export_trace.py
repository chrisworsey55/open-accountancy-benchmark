"""Export verified fictional benchmark Actions into the public learning contract."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.learning.schemas import emit_synthetic_trace
from mirrorfirm.reporting.artifacts import load_run_artifact, write_json_atomic
from mirrorfirm.security import sanitize_for_persistence


def portable(value: JsonValue) -> JsonValue:
    """Local snapshot filenames have no place in the public interchange payload."""
    if isinstance(value, dict):
        return {
            key: portable(item)
            for key, item in value.items()
            if key not in {"db_path", "database_path", "transcript_path"}
        }
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, evaluation = load_run_artifact(args.run)
    if not run.episode_id.startswith("epi-"):
        raise ValueError("only a verified synthetic episode can be exported")
    run_directory = args.run.parent if args.run.is_file() else args.run
    with SQLiteWorldView.open(run_directory / run.final_snapshot.relative_path) as view:
        actions = [
            cast(
                dict[str, JsonValue],
                portable(sanitize_for_persistence(action.model_dump(mode="json"))),
            )
            for action in view.actions()
        ]
    trace = emit_synthetic_trace(
        trace_id=run.run_id,
        episode_id=run.episode_id,
        actions=actions,
        outputs=[evaluation.model_dump(mode="json")],
    )
    write_json_atomic(args.output, trace.model_dump(mode="json"))


if __name__ == "__main__":
    main()
