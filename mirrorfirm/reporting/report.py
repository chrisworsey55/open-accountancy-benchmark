# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-13: retain the generic shell and add deterministic
# stateful scorecards, comparison tables, and offline JSON exports.
"""Generic static HTML report shell derived from Harvey LAB."""

import argparse
import html
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias, TypedDict

from mirrorfirm.reporting.artifacts import (
    AggregateArtifact,
    ComparisonIdentity,
    ExternalApexArtifact,
    ResultArtifactError,
    SecureExecutionDirectory,
    write_json_atomic,
    write_json_atomic_to_directory,
    write_text_atomic,
    write_text_atomic_to_directory,
)

# The retained WP-01 report consumes legacy evaluator JSON rather than a Franklin & McGrath
# domain model. Keep that uncontrolled file boundary explicit and local.
ScorePayload: TypeAlias = dict[str, Any]


class _ComparisonUsage(TypedDict):
    """Machine-readable mean resource use for one native model row."""

    cost_usd: float
    tokens_in: float
    tokens_out: float
    latency_s: float
    steps: float
    world_days_elapsed: float


class _ComparisonSummary(TypedDict):
    """Typed comparison row kept separate from the stable E.17 schema."""

    model: str
    mean_overall: float
    all_pass_rate: float
    critical_failure_rate: float
    critical_failure_ids: list[str]
    reliability: dict[str, float | int]
    layers: dict[str, float]
    usage: _ComparisonUsage


def _normalize_dual_scores(dual: ScorePayload) -> ScorePayload:
    """Flatten a dual-judge aggregate into a single-report shape."""

    per_judge = dual.get("per_judge", {})
    judges = dual.get("judges") or list(per_judge)
    by_judge: dict[str, dict[str, ScorePayload]] = {}
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

    document_coverage: ScorePayload = next(
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
    return write_text_atomic(output_path, report_html, output_root=run_directory)


def _render_criterion(criterion: ScorePayload) -> str:
    verdict = str(criterion.get("verdict", "fail"))
    verdict_class = "pass" if verdict == "pass" else "fail"
    return f"""<details>
<summary><span class="{verdict_class}">{html.escape(verdict.upper())}</span> · {html.escape(str(criterion.get("title", criterion.get("id", ""))))}</summary>
<div class="inner"><div class="reasoning">{html.escape(str(criterion.get("reasoning", "")))}</div></div>
</details>"""


def write_scorecard(
    aggregates: Sequence[AggregateArtifact],
    output: str | Path,
    *,
    secure_directory: SecureExecutionDirectory | None = None,
) -> tuple[Path, Path]:
    """Write deterministic offline HTML and JSON scorecard exports.

    Native model rows, scripted references, and external APEX rows are intentionally
    separated by the caller before rendering.  This function accepts only native and
    reference aggregates because public-reference APEX records are not E.17 results.
    """

    if not aggregates:
        raise ResultArtifactError("at least one aggregate is required for a scorecard")
    output_path = Path(output)
    if output_path.suffix.lower() != ".html":
        raise ValueError("scorecard output must end in .html")
    ordered = tuple(
        sorted(aggregates, key=lambda item: (item.kind, item.episode_id, item.model))
    )
    native = tuple(item for item in ordered if item.kind == "native_model")
    references = tuple(item for item in ordered if item.kind == "scripted_reference")
    payload = {"aggregates": [item.model_dump(mode="json") for item in ordered]}
    json_path = output_path.with_suffix(".json")
    document = _scorecard_html(native, references)
    if secure_directory is not None:
        return _write_report_pair_to_directory(
            secure_directory,
            output_path.name,
            json_path.name,
            payload,
            document,
        )
    write_json_atomic(json_path, payload)
    write_text_atomic(output_path, document)
    return output_path, json_path


def write_comparison_report(
    aggregates: Sequence[AggregateArtifact],
    output: str | Path,
    *,
    secure_directory: SecureExecutionDirectory | None = None,
) -> tuple[Path, Path]:
    """Write a descriptive-only compatible native-model comparison and JSON export."""

    rows = _compatible_native_rows(aggregates)
    output_path = Path(output)
    if output_path.suffix.lower() != ".html":
        raise ValueError("comparison output must end in .html")
    payload = _comparison_payload(rows)
    json_path = output_path.with_suffix(".json")
    if secure_directory is not None:
        return _write_report_pair_to_directory(
            secure_directory,
            output_path.name,
            json_path.name,
            payload,
            _comparison_html(rows),
        )
    write_json_atomic(json_path, payload)
    write_text_atomic(output_path, _comparison_html(rows))
    return output_path, json_path


def write_external_apex_report(
    artifact: ExternalApexArtifact,
    output: str | Path,
    *,
    secure_directory: SecureExecutionDirectory | None = None,
) -> tuple[Path, Path]:
    """Render a public-reference APEX result only in an external report section."""

    output_path = Path(output)
    if output_path.suffix.lower() != ".html":
        raise ValueError("external report output must end in .html")
    payload = artifact.model_dump(mode="json")
    json_path = output_path.with_suffix(".json")
    evaluation = artifact.evaluation
    raw_criteria = evaluation.get("criterion_results")
    criteria = raw_criteria if isinstance(raw_criteria, list) else []
    passed = sum(
        isinstance(item, Mapping) and item.get("passed") is True for item in criteria
    )
    body = (
        "<h1>External APEX-Accounting Result</h1>"
        "<p>This public-reference result is external and not comparable to native "
        "Franklin &amp; McGrath scores. It is excluded from all headline aggregation.</p>"
        "<table><tbody>"
        f"<tr><th>Label</th><td>{_escape(artifact.report_label)}</td></tr>"
        f"<tr><th>Revision</th><td>{_escape(artifact.source_revision)}</td></tr>"
        f"<tr><th>Contamination</th><td>{_escape(artifact.contamination)}</td></tr>"
        f"<tr><th>Task</th><td>{_escape(evaluation.get('task_id', '—'))}</td></tr>"
        f"<tr><th>Criteria</th><td>{passed}/{len(criteria)}</td></tr>"
        "</tbody></table>"
    )
    document = _document("External APEX-Accounting Result", body)
    if secure_directory is not None:
        return _write_report_pair_to_directory(
            secure_directory,
            output_path.name,
            json_path.name,
            payload,
            document,
        )
    write_json_atomic(json_path, payload)
    write_text_atomic(output_path, document)
    return output_path, json_path


def _write_report_pair_to_directory(
    directory: SecureExecutionDirectory,
    html_name: str,
    json_name: str,
    payload: object,
    document: str,
) -> tuple[Path, Path]:
    """Publish a report pair only while the retained output root remains intact."""

    if (
        not html_name
        or not json_name
        or Path(html_name).name != html_name
        or Path(json_name).name != json_name
        or not html_name.endswith(".html")
        or not json_name.endswith(".json")
    ):
        raise ResultArtifactError("report output filename is unsafe")
    try:
        directory.checkpoint()
        json_path = write_json_atomic_to_directory(directory, json_name, payload)
        directory.checkpoint()
        html_path = write_text_atomic_to_directory(directory, html_name, document)
        directory.checkpoint()
        return html_path, json_path
    except BaseException:
        directory.remove_file_if_present(html_name)
        directory.remove_file_if_present(json_name)
        raise


def _scorecard_html(
    native: Sequence[AggregateArtifact], references: Sequence[AggregateArtifact]
) -> str:
    return _document(
        "Franklin & McGrath Scorecard",
        "".join(
            (
                "<h1>Franklin &amp; McGrath Scorecard</h1>"
                + _aggregate_section("Model performance", native)
                + _aggregate_section(
                    "Reference trajectories — not model performance", references
                )
                + "<section><h2>External APEX results</h2><p>External/non-comparable "
                "APEX rows are intentionally excluded from native headline metrics.</p></section>"
            )
        ),
    )


def _comparison_html(rows: Sequence[AggregateArtifact]) -> str:
    summaries = _model_summaries(rows)
    summary_rows = "".join(
        "<tr>"
        f"<td>{_escape(summary['model'])}</td>"
        f"<td>{float(summary['mean_overall']):.3f}</td>"
        f"<td>{float(summary['all_pass_rate']):.3f}</td>"
        f"<td>{float(summary['critical_failure_rate']):.3f}</td>"
        f"<td>{_escape(', '.join(summary['critical_failure_ids']) or '—')}</td>"
        f"<td>{_metric(summary['reliability'], 'pass_at_1')}</td>"
        f"<td>{_metric(summary['reliability'], 'pass_at_5')}</td>"
        f"<td>{_metric(summary['reliability'], 'pass_to_3')}</td>"
        f"<td>{_metric(summary['reliability'], 'pass_to_5')}</td>"
        f"<td>{float(summary['layers']['task_completion']):.3f}</td>"
        f"<td>{float(summary['layers']['accounting']):.3f}</td>"
        f"<td>{float(summary['layers']['state']):.3f}</td>"
        f"<td>{float(summary['layers']['safety']):.3f}</td>"
        f"<td>{float(summary['layers']['provenance']):.3f}</td>"
        f"<td>{float(summary['layers']['communication']):.3f}</td>"
        f"<td>{float(summary['layers']['efficiency']):.3f}</td>"
        f"<td>{float(summary['usage']['cost_usd']):.2f}</td>"
        f"<td>{float(summary['usage']['tokens_in']):.0f}/"
        f"{float(summary['usage']['tokens_out']):.0f}</td>"
        f"<td>{float(summary['usage']['latency_s']):.2f}</td>"
        f"<td>{float(summary['usage']['steps']):.1f}</td>"
        f"<td>{float(summary['usage']['world_days_elapsed']):.2f}</td>"
        "</tr>"
        for summary in summaries
    )
    body = (
        "<h1>Franklin &amp; McGrath Comparison</h1>"
        "<p>Descriptive results only; this report does not imply statistical significance.</p>"
        "<table><thead><tr><th>Model</th><th>Mean overall</th><th>All-pass rate</th>"
        "<th>CF rate</th><th>CF identifiers</th>"
        "<th>Pass@1</th><th>Pass@5</th><th>Pass^3</th><th>Pass^5</th>"
        "<th>Task</th><th>Accounting</th><th>State</th><th>Safety</th>"
        "<th>Provenance</th><th>Communication</th><th>Efficiency</th>"
        "<th>Mean cost</th><th>Input/output tokens</th><th>Latency</th><th>Steps</th>"
        "<th>World-days</th></tr></thead><tbody>"
        + summary_rows
        + "</tbody></table>"
        + _aggregate_section("Episode-by-episode breakdown", rows)
    )
    return _document("Franklin & McGrath Comparison", body)


def _aggregate_section(title: str, rows: Sequence[AggregateArtifact]) -> str:
    if not rows:
        return f"<section><h2>{_escape(title)}</h2><p>None available.</p></section>"
    headings = (
        "Episode",
        "Model",
        "World/version",
        "Runs",
        "Overall",
        "All-pass",
        "CFs",
        "Task",
        "Accounting",
        "State",
        "Safety",
        "Provenance",
        "Communication",
        "Efficiency",
        "Pass@1",
        "Pass@5",
        "Pass^3 (headline)",
        "Pass^5",
        "Cost",
        "Input/output tokens",
        "Latency",
        "Steps",
        "World-days",
        "Configuration",
    )
    table_rows = "".join(_aggregate_row(row) for row in rows)
    details = "".join(_failure_details(row) for row in rows)
    return (
        f"<section><h2>{_escape(title)}</h2><table><thead><tr>"
        + "".join(f"<th>{_escape(heading)}</th>" for heading in headings)
        + "</tr></thead><tbody>"
        + table_rows
        + "</tbody></table>"
        + '<p class="cf-zeroed">CF-zeroed rows have one or more critical '
        "failures and an overall score of 0.</p>" + details + "</section>"
    )


def _aggregate_row(row: AggregateArtifact) -> str:
    reliability = row.reliability
    failures = ", ".join(row.critical_failure_ids) or "—"
    cf_zeroed = any(result.critical_failures for result in row.results)
    row_class = ' class="cf-zeroed"' if cf_zeroed else ""
    values = (
        row.episode_id,
        row.label or row.model,
        f"{row.world_id} / {row.world_version}",
        str(row.run_count),
        f"{float(reliability['mean_overall']):.3f}",
        f"{float(reliability['all_pass_rate']):.3f}",
        failures,
        f"{row.layer_means.task_completion:.3f}",
        f"{row.layer_means.accounting:.3f}",
        f"{row.layer_means.state:.3f}",
        f"{row.layer_means.safety:.3f}",
        f"{row.layer_means.provenance:.3f}",
        f"{row.layer_means.communication:.3f}",
        f"{row.layer_means.efficiency:.3f}",
        _metric(reliability, "pass_at_1"),
        _metric(reliability, "pass_at_5"),
        _metric(reliability, "pass_to_3"),
        _metric(reliability, "pass_to_5"),
        f"{row.usage_means.cost_usd:.2f}",
        f"{row.usage_means.tokens_in:.0f}/{row.usage_means.tokens_out:.0f}",
        f"{row.usage_means.latency_s:.2f}",
        f"{row.usage_means.steps:.1f}",
        f"{row.usage_means.world_days_elapsed:.2f}",
        row.configuration_hash,
    )
    return (
        f"<tr{row_class}>"
        + "".join(f"<td>{_escape(value)}</td>" for value in values)
        + "</tr>"
    )


def _failure_details(row: AggregateArtifact) -> str:
    failed: list[tuple[str, str, str, tuple[str, ...]]] = []
    for result in row.results:
        for criterion in result.criterion_results:
            if not criterion.passed:
                failed.append(
                    (
                        result.run_id,
                        criterion.criterion_id,
                        criterion.detail,
                        tuple(criterion.evidence_refs),
                    )
                )
    if not failed:
        return ""
    items = "".join(
        "<li><strong>"
        + _escape(f"{run_id}: {criterion_id}")
        + "</strong><pre>"
        + _escape(detail)
        + "</pre><span>Evidence: "
        + _escape(", ".join(evidence) or "—")
        + "</span></li>"
        for run_id, criterion_id, detail, evidence in sorted(failed)
    )
    return f"<details><summary>Failed criteria and evidence</summary><ul>{items}</ul></details>"


def _compatible_native_rows(
    aggregates: Sequence[AggregateArtifact],
) -> tuple[AggregateArtifact, ...]:
    if not aggregates:
        raise ResultArtifactError("at least one aggregate is required for comparison")
    rows = tuple(sorted(aggregates, key=lambda item: (item.episode_id, item.model)))
    world_pins: dict[str, tuple[str, str]] = {}
    for row in rows:
        if row.kind != "native_model":
            raise ResultArtifactError(
                "reference and external results cannot enter model-performance comparison"
            )
        if not row.configuration_hash:
            raise ResultArtifactError(
                "comparison input is missing configuration provenance"
            )
        if row.comparison_identity.run_kind != "native_model":
            raise ResultArtifactError(
                "comparison input has non-native execution provenance"
            )
        pin = (row.world_id, row.world_version)
        current = world_pins.setdefault(row.episode_id, pin)
        if current != pin:
            raise ResultArtifactError("comparison mixes world pins for one episode")
    run_ids = [run.run_id for row in rows for run in row.runs]
    artifact_ids = [run.artifact_id for row in rows for run in row.runs]
    if len(set(run_ids)) != len(run_ids) or len(set(artifact_ids)) != len(artifact_ids):
        raise ResultArtifactError(
            "comparison input contains duplicate run or artifact identity"
        )
    identities: dict[str, ComparisonIdentity] = {}
    for row in rows:
        first_identity = identities.setdefault(row.episode_id, row.comparison_identity)
        if row.comparison_identity != first_identity:
            differing = [
                field
                for field in type(first_identity).model_fields
                if getattr(first_identity, field)
                != getattr(row.comparison_identity, field)
            ]
            raise ResultArtifactError(
                "comparison has incompatible material configuration: "
                + ", ".join(sorted(differing))
            )
    return rows


def _comparison_payload(rows: Sequence[AggregateArtifact]) -> dict[str, object]:
    return {
        "kind": "native-comparison",
        "rows": [row.model_dump(mode="json") for row in rows],
        "model_summaries": list(_model_summaries(rows)),
    }


def _model_summaries(
    rows: Sequence[AggregateArtifact],
) -> tuple[_ComparisonSummary, ...]:
    grouped: defaultdict[str, list[AggregateArtifact]] = defaultdict(list)
    for row in rows:
        grouped[row.model].append(row)
    summaries: list[_ComparisonSummary] = []
    for model in sorted(grouped):
        entries = grouped[model]
        results = sorted(
            (result for entry in entries for result in entry.results),
            key=lambda result: (result.episode_id, result.run_id),
        )
        count = len(results)
        layers = {
            field: sum(float(getattr(result.scores, field)) for result in results)
            / count
            for field in (
                "task_completion",
                "accounting",
                "state",
                "safety",
                "provenance",
                "communication",
                "efficiency",
            )
        }
        failures = [result for result in results if result.critical_failures]
        summaries.append(
            _ComparisonSummary(
                model=model,
                mean_overall=sum(result.overall for result in results) / count,
                all_pass_rate=sum(result.all_pass for result in results) / count,
                critical_failure_rate=len(failures) / count,
                critical_failure_ids=sorted(
                    {
                        failure.cf_id
                        for result in results
                        for failure in result.critical_failures
                    }
                ),
                reliability=_suite_reliability(results),
                layers=layers,
                usage=_ComparisonUsage(
                    cost_usd=sum(result.usage.cost_usd for result in results) / count,
                    tokens_in=sum(result.usage.tokens_in for result in results) / count,
                    tokens_out=sum(result.usage.tokens_out for result in results)
                    / count,
                    latency_s=sum(result.usage.latency_s for result in results) / count,
                    steps=sum(result.usage.steps for result in results) / count,
                    world_days_elapsed=sum(
                        result.usage.world_days_elapsed for result in results
                    )
                    / count,
                ),
            )
        )
    return tuple(summaries)


def _suite_reliability(
    results: Sequence[object],
) -> dict[str, float | int]:
    """Apply the stable Pass@k/Pass^k truth table to sorted native result rows."""

    values = [getattr(result, "all_pass") for result in results]
    if not values or not all(isinstance(value, bool) for value in values):
        raise ResultArtifactError("comparison contains invalid evaluation verdicts")
    summary: dict[str, float | int] = {}
    for value in (1, 3, 5):
        if len(values) >= value:
            selected = values[:value]
            summary[f"pass_at_{value}"] = int(any(selected))
            summary[f"pass_to_{value}"] = int(all(selected))
    return summary


def _metric(values: Mapping[str, float | int], key: str) -> str:
    return str(values[key]) if key in values else "unavailable"


def _document(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #17202a; }}
table {{ border-collapse: collapse; width: 100%; font-size: .82rem; }}
th, td {{ border: 1px solid #ccd1d1; padding: .38rem; text-align: left; vertical-align: top; }}
th {{ background: #ebf5fb; position: sticky; top: 0; }}
.cf-zeroed {{ background: #fdecea; }} pre {{ white-space: pre-wrap; }}
details {{ margin-top: 1rem; }} section {{ margin: 2rem 0; overflow-x: auto; }}
</style></head><body>{body}</body></html>"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def main() -> None:
    """Generate a generic report from a run directory."""

    parser = argparse.ArgumentParser(description="Generate an HTML evaluation report")
    parser.add_argument("--run-directory", required=True, type=Path)
    arguments = parser.parse_args()
    print(f"Report written to: {generate_report(arguments.run_directory)}")


if __name__ == "__main__":
    main()
