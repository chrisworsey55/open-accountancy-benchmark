# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: the report shell is package-local and has no upstream
# utility dependency. Mirror Firm score extensions are deferred to WP-13.
"""Generic static HTML report shell derived from Harvey LAB."""

import argparse
import html
import json
from pathlib import Path


def _normalize_dual_scores(dual: dict) -> dict:
    """Flatten a dual-judge aggregate into a single-report shape."""

    per_judge = dual.get("per_judge", {})
    judges = dual.get("judges") or list(per_judge)
    by_judge: dict[str, dict[str, dict]] = {}
    for judge_model, scores in per_judge.items():
        by_judge[judge_model] = {
            criterion["id"]: criterion
            for criterion in scores.get("criteria_results", [])
        }

    first = per_judge.get(judges[0], {}) if judges else {}
    merged_criteria = []
    for criterion in first.get("criteria_results", []):
        criterion_id = criterion["id"]
        verdicts = []
        reasonings = []
        for judge_model in judges:
            judge_criterion = by_judge.get(judge_model, {}).get(criterion_id)
            if judge_criterion is not None:
                verdicts.append(judge_criterion.get("verdict") == "pass")
                reasonings.append(
                    f"[{judge_model}] {judge_criterion.get('reasoning', '')}"
                )
        merged_criteria.append(
            {
                "id": criterion_id,
                "title": criterion.get("title", criterion_id),
                "verdict": "pass" if verdicts and all(verdicts) else "fail",
                "reasoning": "\n\n".join(reasonings),
            }
        )

    document_coverage = next(
        (
            scores["doc_coverage"]
            for scores in per_judge.values()
            if scores.get("doc_coverage")
        ),
        {},
    )
    return {
        "run_id": dual.get("run_id", ""),
        "task": dual.get("task", ""),
        "judge_model": " + ".join(judges),
        "scored_at": dual.get("scored_at", ""),
        "score": dual.get("dual_criterion_pass", 0.0),
        "criteria_results": merged_criteria,
        "doc_coverage": document_coverage,
    }


def generate_report(run_directory: Path) -> Path:
    """Render ``scores.json`` or ``scores_dual.json`` to ``report.html``.

    The retained shell accepts Harvey's generic score shape. WP-13 will add the Mirror
    Firm EvaluationResult columns and comparison views defined in `SPEC.md`.
    """

    scores_path = run_directory / "scores.json"
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
    else:
        dual_path = run_directory / "scores_dual.json"
        if not dual_path.exists():
            raise FileNotFoundError(
                f"No scores.json or scores_dual.json in {run_directory}"
            )
        scores = _normalize_dual_scores(
            json.loads(dual_path.read_text(encoding="utf-8"))
        )

    criteria = scores.get("criteria_results", [])
    passed = sum(criterion.get("verdict") == "pass" for criterion in criteria)
    total = len(criteria)
    criteria_html = "".join(_render_criterion(criterion) for criterion in criteria)
    metadata = {
        "run_id": html.escape(str(scores.get("run_id", ""))),
        "task": html.escape(str(scores.get("task", ""))),
        "judge": html.escape(str(scores.get("judge_model", ""))),
        "scored_at": html.escape(str(scores.get("scored_at", ""))[:10]),
        "score": float(scores.get("score", 0.0)),
    }
    coverage = scores.get("doc_coverage", {})
    document_coverage = (
        f"{coverage.get('documents_read', '—')}/{coverage.get('total_documents', '—')}"
    )
    status = "ALL PASS" if total > 0 and passed == total else f"MISSED {total - passed}"

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Agent Evaluation — {metadata["run_id"]}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 960px; margin: 40px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.5; }}
h1 {{ font-size: 1.4rem; }} .meta {{ color: #666; font-size: .85rem; margin-bottom: 32px; }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 40px; }}
.stat {{ background: #f5f5f5; border-radius: 8px; padding: 16px; text-align: center; }}
.value {{ font-size: 2rem; font-weight: 700; }} .label {{ font-size: .75rem; color: #666; text-transform: uppercase; }}
details {{ border: 1px solid #e0e0e0; border-radius: 6px; margin-bottom: 8px; }} summary {{ padding: 12px 16px; cursor: pointer; }} .inner {{ padding: 16px; }}
.pass {{ color: #155724; }} .fail {{ color: #721c24; }} .reasoning {{ white-space: pre-wrap; background: #f8f9fa; padding: 10px 12px; }}
</style>
</head>
<body>
<h1>Agent Evaluation Report</h1>
<div class="meta">Run: <strong>{metadata["run_id"]}</strong> · Task: {metadata["task"]} · Judge: {metadata["judge"]} · Scored: {metadata["scored_at"]}</div>
<div class="stats">
  <div class="stat"><div class="value">{metadata["score"]:.2f}</div><div class="label">Score</div></div>
  <div class="stat"><div class="value">{passed}/{total}</div><div class="label">Criteria Passed</div></div>
  <div class="stat"><div class="value">{html.escape(document_coverage)}</div><div class="label">Document Coverage</div></div>
  <div class="stat"><div class="value">{status}</div><div class="label">All-pass</div></div>
</div>
<h2>Criteria ({passed} passed, {total - passed} failed)</h2>
{criteria_html}
</body>
</html>"""
    output_path = run_directory / "report.html"
    output_path.write_text(report_html, encoding="utf-8")
    return output_path


def _render_criterion(criterion: dict) -> str:
    verdict = str(criterion.get("verdict", "fail"))
    verdict_class = "pass" if verdict == "pass" else "fail"
    return f"""<details>
<summary><span class="{verdict_class}">{html.escape(verdict.upper())}</span> · {html.escape(str(criterion.get("title", criterion.get("id", ""))))}</summary>
<div class="inner"><div class="reasoning">{html.escape(str(criterion.get("reasoning", "")))}</div></div>
</details>"""


def main() -> None:
    """Generate a generic report from a run directory."""

    parser = argparse.ArgumentParser(description="Generate an HTML evaluation report")
    parser.add_argument("--run-directory", required=True, type=Path)
    arguments = parser.parse_args()
    print(f"Report written to: {generate_report(arguments.run_directory)}")


if __name__ == "__main__":
    main()
