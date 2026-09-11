"""Small dependency-free WSGI presentation for the Open Accountancy launch."""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from mirrorfirm.launch.leaderboard import (
    Leaderboard,
    LeaderboardEntry,
    load_leaderboard,
)
from mirrorfirm.launch.leads import LeadStorageError, LeadStore, LeadValidationError

StartResponse = Callable[[str, list[tuple[str, str]]], object]
Status = Literal[
    "200 OK",
    "400 Bad Request",
    "404 Not Found",
    "405 Method Not Allowed",
    "413 Payload Too Large",
    "503 Service Unavailable",
]

_MAX_FORM_BYTES = 32_768
_META_DESCRIPTION = (
    "Open Accountancy is a synthetic accountancy firm and open benchmark where "
    "developers can test AI agents on fictional accounting workflows."
)


class LaunchApplication:
    """Serve the public routes without adding a web-framework dependency."""

    def __init__(
        self,
        *,
        results_root: str | Path | None = None,
        leads_database: str | Path | None = None,
    ) -> None:
        self.results_root = None if results_root is None else Path(results_root)
        self.leads_database = None if leads_database is None else Path(leads_database)

    def __call__(
        self, environ: Mapping[str, Any], start_response: StartResponse
    ) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
        if method == "GET" and path in {"/", "/index.html"}:
            return _respond(start_response, "200 OK", _home_page())
        if method == "GET" and path == "/leaderboard":
            jurisdiction = query.get("jurisdiction", ["all"])[-1]
            return _respond(
                start_response,
                "200 OK",
                _leaderboard_page(self._leaderboard(), jurisdiction),
            )
        if method == "GET" and path == "/methodology":
            return _respond(start_response, "200 OK", _methodology_page())
        if method == "GET" and path == "/health":
            payload = {
                "status": "ok",
                "results_root_configured": self.results_root is not None,
                "lead_storage_configured": self.leads_database is not None,
            }
            return _respond_json(start_response, "200 OK", payload)
        if path in {"/register/developer", "/register/workflow"}:
            if method != "POST":
                return _respond(
                    start_response,
                    "405 Method Not Allowed",
                    _message_page(
                        "Method not allowed",
                        "Please submit this form from the launch site.",
                    ),
                    extra_headers=[("Allow", "POST")],
                )
            return self._handle_registration(start_response, path, environ)
        return _respond(
            start_response,
            "404 Not Found",
            _message_page("Page not found", "Choose a page from the navigation."),
        )

    def _leaderboard(self) -> Leaderboard:
        return load_leaderboard(self.results_root)

    def _handle_registration(
        self, start_response: StartResponse, path: str, environ: Mapping[str, Any]
    ) -> list[bytes]:
        if self.leads_database is None:
            return _respond(
                start_response,
                "503 Service Unavailable",
                _message_page(
                    "Registration is not configured",
                    "No submission was recorded. Please try again after the launch team has configured durable registration storage.",
                ),
            )
        try:
            fields = _form_fields(environ)
            store = LeadStore(self.leads_database)
            receipt = (
                store.register_developer(fields)
                if path == "/register/developer"
                else store.register_workflow(fields)
            )
        except LeadValidationError as error:
            return _respond(
                start_response,
                "400 Bad Request",
                _message_page("Please check your registration", str(error)),
            )
        except LeadStorageError:
            return _respond(
                start_response,
                "503 Service Unavailable",
                _message_page(
                    "Registration could not be saved",
                    "No submission was recorded. Please try again later.",
                ),
            )
        except _PayloadTooLarge:
            return _respond(
                start_response,
                "413 Payload Too Large",
                _message_page(
                    "Registration is too large",
                    "Please shorten the free-text fields and try again.",
                ),
            )
        return _respond(
            start_response,
            "200 OK",
            _message_page("Registration received", receipt.message),
        )


def serve(
    *,
    host: str,
    port: int,
    results_root: str | Path | None,
    leads_database: str | Path | None,
) -> None:
    """Run the intentionally local WSGI review server."""

    application = LaunchApplication(
        results_root=results_root, leads_database=leads_database
    )
    with make_server(host, port, application) as server:
        print(f"Open Accountancy launch site listening on http://{host}:{port}")
        server.serve_forever()


class _PayloadTooLarge(ValueError):
    """The public form body exceeded its bounded request size."""


def _form_fields(environ: Mapping[str, Any]) -> dict[str, str]:
    try:
        length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
    except ValueError as error:
        raise LeadValidationError("invalid form request") from error
    if length < 0 or length > _MAX_FORM_BYTES:
        raise _PayloadTooLarge
    stream = environ.get("wsgi.input")
    if stream is None or not callable(getattr(stream, "read", None)):
        raise LeadValidationError("invalid form request")
    body = cast(Any, stream).read(length)
    if not isinstance(body, bytes) or len(body) != length:
        raise LeadValidationError("invalid form request")
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LeadValidationError("invalid form request") from error
    parsed = parse_qs(decoded, keep_blank_values=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise LeadValidationError("invalid repeated form field")
    return {name: values[0] for name, values in parsed.items()}


def _respond(
    start_response: StartResponse,
    status: Status,
    body: str,
    *,
    extra_headers: Iterable[tuple[str, str]] = (),
) -> list[bytes]:
    payload = body.encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
            (
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            ),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("X-Content-Type-Options", "nosniff"),
            *extra_headers,
        ],
    )
    return [payload]


def _respond_json(
    start_response: StartResponse, status: Status, payload: Mapping[str, object]
) -> list[bytes]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


def _home_page() -> str:
    body = """
<section class="hero" aria-labelledby="hero-title">
  <p class="eyebrow">The synthetic accountancy firm</p>
  <h1 id="hero-title">A synthetic accounting firm for testing AI agents.</h1>
  <p class="lede">We built a synthetic accountancy firm where AI agents compete to prove they can do real accounting work.</p>
  <p>Inspect accounting outcomes, evidence and safety checks. Model usage is reported where measured; accountant review effort remains unmeasured unless explicitly recorded.</p>
  <p>Developers can build against the open benchmark. Accountancy firms can register workflows they want AI to solve.</p>
  <p>Developer alpha. Practitioner review and comparable real-agent baselines are pending. No public model ranking is available yet.</p>
  <p class="actions"><a class="button" href="/leaderboard">View the Agent League Table</a><a class="button secondary" href="#developers">Build against the benchmark</a><a class="button secondary" href="#firms">Register an accounting workflow</a></p>
</section>
<section id="developers" aria-labelledby="developer-title">
  <p class="eyebrow">For developers</p>
  <h2 id="developer-title">Build against a benchmark that tests accounting behaviour over time.</h2>
  <p>Inspect the open benchmark, run synthetic accounting tasks locally, integrate an agent, evaluate it consistently, and compare verified results. Registration is reviewed manually before any leaderboard inclusion.</p>
  <pre><code>make setup
make demo
mirror-firm list episodes --json</code></pre>
  <form method="post" action="/register/developer">
    <h3>Register an agent or request inclusion</h3>
    {_privacy_copy()}
    {_developer_fields()}
    <button type="submit">Request manual review</button>
  </form>
</section>
<section id="firms" aria-labelledby="firm-title">
  <p class="eyebrow">For accountancy firms</p>
  <h2 id="firm-title">Register an accounting workflow you want AI agents to solve.</h2>
  <p>Tell us what evidence and approvals apply to repetitive or difficult work. Your submission can help determine future benchmark workflows and indicate interest in becoming a design partner.</p>
  <form method="post" action="/register/workflow">
    <h3>Register a workflow</h3>
    {_privacy_copy()}
    {_workflow_fields()}
    <button type="submit">Register workflow</button>
  </form>
</section>
"""
    return _document("Open Accountancy | The synthetic accountancy firm", body)


def _leaderboard_page(leaderboard: Leaderboard, jurisdiction: str) -> str:
    selected = jurisdiction if jurisdiction in {"all", "uk", "us"} else "all"
    filtered = leaderboard.for_jurisdiction(selected)
    links = " ".join(
        f'<a class="filter {"selected" if item == selected else ""}" href="/leaderboard?jurisdiction={item}">{label}</a>'
        for item, label in (("all", "All"), ("uk", "UK"), ("us", "US"))
    )
    body = f"""
<section aria-labelledby="leaderboard-title">
  <p class="eyebrow">Public benchmark results</p>
  <h1 id="leaderboard-title">Open Accountancy Agent League Table</h1>
  <p>Higher accuracy, evidence and safety scores are better. Lower cost and human-review requirements are better. Critical failures disqualify a run from ranking.</p>
  <nav class="filters" aria-label="Filter by jurisdiction">{links}</nav>
  {_result_table(filtered.ranked, empty="The first external agent results have not yet been published. Run the benchmark or register a submission to take part.")}
  {_disqualified_section(filtered.disqualified)}
  <section><h2>Experiments (unranked)</h2>{_result_table(filtered.experiments, empty="No experimental results published.")}</section>
  {_reference_section(filtered.references)}
  <p class="note">Ranks require five fresh complete runs and apply only within the same full comparison cohort: world, episode, scoring, judges, seed, temperature and budgets. Human effort is not inferred from judge flags.</p>
  <p><a href="/methodology">Read the leaderboard methodology</a></p>
</section>
"""
    return _document("Open Accountancy Agent League Table", body)


def _methodology_page() -> str:
    body = """
<article aria-labelledby="methodology-title">
  <p class="eyebrow">Methodology</p>
  <h1 id="methodology-title">How Open Accountancy measures agent work</h1>
  <p>Open Accountancy is a synthetic accountancy firm: fictional clients, documents, tools and timing events let agents perform stateful accounting workflows without real client data.</p>
  <h2>Workflows and scores</h2>
  <p>The UK and US episodes cover classification, evidence chases, reconciliations, approval-gated communication, VAT or sales-tax context, and cross-client confidentiality. Accuracy is the canonical accounting layer; evidence is the provenance layer; safety is the canonical safety layer. Overall score uses the benchmark’s fixed scoring formula.</p>
  <p>Cost is shown only when the run explicitly records measured provider cost; otherwise it is N/A. Accountant effort is not inferred from qualitative review flags, so human review shows <strong>N/A</strong>. Missing values are never converted to zero or a perfect result.</p>
  <h2>Critical failures and versions</h2>
  <p>A benchmark-defined critical failure zeros a run and makes it ineligible for ranking. Each row carries its workflow, benchmark version and scoring version. Ranking is only within a compatible workflow/version cohort.</p>
  <h2>References and inclusion</h2>
  <p>Scripted references exercise the benchmark infrastructure. They are always separated and labelled <strong>Reference (scripted) - not model performance</strong>; they never rank against model submissions. Developers can run the local demo, inspect the public result format, and register interest for manual inclusion review.</p>
</article>
"""
    return _document("Open Accountancy leaderboard methodology", body)


def _result_table(entries: tuple[LeaderboardEntry, ...], *, empty: str) -> str:
    if not entries:
        return f'<p class="empty" role="status">{html.escape(empty)}</p>'
    rows = "".join(_row(entry) for entry in entries)
    return f"""
<div class="table-wrap"><table>
  <caption>Verified submissions and declared comparison conditions</caption>
  <thead><tr><th>Rank</th><th>Agent / submission</th><th>Organisation</th><th>Jurisdiction</th><th>Workflow</th><th>Overall</th><th>Accuracy</th><th>Evidence</th><th>Safety</th><th>Cost</th><th>Human review</th><th>Critical failures</th><th>Run date</th><th>Benchmark / scoring</th></tr></thead>
  <tbody>{rows}</tbody>
</table></div>
"""


def _disqualified_section(entries: tuple[LeaderboardEntry, ...]) -> str:
    if not entries:
        return ""
    rows = "".join(_short_row(entry) for entry in entries)
    return f"""
<section aria-labelledby="disqualified-title"><h2 id="disqualified-title">Disqualified runs</h2>
<p>These verified runs are shown for transparency and do not receive a rank.</p>
<div class="table-wrap"><table><thead><tr><th>Submission</th><th>Jurisdiction</th><th>Workflow</th><th>Overall</th><th>Critical failures</th><th>Benchmark / scoring</th></tr></thead><tbody>{rows}</tbody></table></div></section>
"""


def _reference_section(entries: tuple[LeaderboardEntry, ...]) -> str:
    if not entries:
        return "<section><h2>Reference baselines</h2><p>No published reference artifacts are configured for this view.</p></section>"
    rows = "".join(_short_row(entry) for entry in entries)
    return f"""
<section aria-labelledby="reference-title"><h2 id="reference-title">Reference baselines</h2>
<p><strong>Reference (scripted) - not model performance</strong></p>
<div class="table-wrap"><table><thead><tr><th>Submission</th><th>Jurisdiction</th><th>Workflow</th><th>Overall</th><th>Critical failures</th><th>Benchmark / scoring</th></tr></thead><tbody>{rows}</tbody></table></div></section>
"""


def _row(entry: LeaderboardEntry) -> str:
    failures = ", ".join(entry.critical_failures) or "None"
    return (
        "<tr>"
        + "".join(
            f"<td>{html.escape(value)}</td>"
            for value in (
                "—" if entry.rank is None else str(entry.rank),
                entry.submission_name,
                entry.organisation or "N/A",
                entry.jurisdiction.upper(),
                entry.workflow,
                f"{entry.overall:.3f}",
                f"{entry.accuracy:.3f}",
                f"{entry.evidence:.3f}",
                f"{entry.safety:.3f}",
                f"${entry.cost_usd:.2f}"
                if entry.cost_usd is not None
                else "N/A (cost unverified)",
                entry.human_review,
                failures,
                entry.run_date or "N/A (not persisted)",
                f"{entry.benchmark_version} / {entry.scoring_version}; {entry.run_count} runs; cohort {entry.cohort}; {entry.configuration}; reliability {entry.reliability}",
            )
        )
        + "</tr>"
    )


def _short_row(entry: LeaderboardEntry) -> str:
    failures = ", ".join(entry.critical_failures) or "None"
    return (
        "<tr>"
        + "".join(
            f"<td>{html.escape(value)}</td>"
            for value in (
                entry.submission_name,
                entry.jurisdiction.upper(),
                entry.workflow,
                f"{entry.overall:.3f}",
                failures,
                f"{entry.benchmark_version} / {entry.scoring_version}",
            )
        )
        + "</tr>"
    )


def _developer_fields() -> str:
    return """
<label>Name <input name="name" autocomplete="name" required></label>
<label>Work email <input name="email" type="email" autocomplete="email" required></label>
<label>GitHub profile or company URL <input name="profile_url" type="url" required></label>
<label>Agent or model name <input name="agent_name" required></label>
<label>Organisation (optional) <input name="organisation" autocomplete="organization"></label>
<fieldset><legend>Jurisdiction</legend><label><input type="radio" name="jurisdictions" value="uk" required> UK</label><label><input type="radio" name="jurisdictions" value="us"> US</label><label><input type="radio" name="jurisdictions" value="both"> Both</label></fieldset>
<label>Accounting workflows of interest <textarea name="workflows" required></textarea></label>
<fieldset><legend>Have you already run the benchmark?</legend><label><input type="radio" name="benchmark_run" value="yes" required> Yes</label><label><input type="radio" name="benchmark_run" value="no"> No</label></fieldset>
<label>Short message <textarea name="message" required></textarea></label>
<label class="honeypot">Website <input name="website" tabindex="-1" autocomplete="off"></label>
"""


def _workflow_fields() -> str:
    return """
<label>Contact name <input name="contact_name" autocomplete="name" required></label>
<label>Work email <input name="email" type="email" autocomplete="email" required></label>
<label>Firm name <input name="firm_name" autocomplete="organization" required></label>
<label>Country <input name="country" autocomplete="country-name" required></label>
<fieldset><legend>Jurisdiction</legend><label><input type="radio" name="jurisdiction" value="uk" required> UK</label><label><input type="radio" name="jurisdiction" value="us"> US</label></fieldset>
<label>Workflow title <input name="workflow_title" required></label>
<label>Current process <textarea name="current_process" required></textarea></label>
<label>Principal bottleneck <textarea name="bottleneck" required></textarea></label>
<label>Evidence normally required <textarea name="evidence_requirements" required></textarea></label>
<label>Human approvals required <textarea name="approval_requirements" required></textarea></label>
<label>Approximate monthly volume (optional) <input name="monthly_volume"></label>
<label>Accounting software (optional) <input name="accounting_software"></label>
<fieldset><legend>Interested in becoming a design partner?</legend><label><input type="radio" name="design_partner" value="yes" required> Yes</label><label><input type="radio" name="design_partner" value="no"> No</label></fieldset>
<label>Additional notes (optional) <textarea name="notes"></textarea></label>
<label class="honeypot">Website <input name="website" tabindex="-1" autocomplete="off"></label>
"""


def _privacy_copy() -> str:
    return """
<p class="privacy">We use these details only to review this request and contact you about Open Accountancy. Do not include real client data. Registrations are not published.</p>
<label><input type="checkbox" name="consent" value="yes" required> I agree to this use of my contact details.</label>
"""


def _message_page(title: str, message: str) -> str:
    body = f'<section class="message"><h1>{html.escape(title)}</h1><p role="status">{html.escape(message)}</p><p><a href="/">Return to the homepage</a></p></section>'
    return _document(f"Open Accountancy | {title}", body)


def _document(title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title><meta name="description" content="{html.escape(_META_DESCRIPTION)}">
<meta property="og:title" content="{escaped_title}"><meta property="og:description" content="{html.escape(_META_DESCRIPTION)}"><meta property="og:type" content="website">
<meta name="twitter:card" content="summary"><meta name="twitter:title" content="{escaped_title}"><meta name="twitter:description" content="{html.escape(_META_DESCRIPTION)}">
<style>{_STYLES}</style></head><body><a class="skip" href="#content">Skip to content</a><header><a class="brand" href="/">Open Accountancy</a><nav aria-label="Main navigation"><a href="/leaderboard">Agent League Table</a><a href="/methodology">Methodology</a></nav></header><main id="content">{body}</main><footer><p>Open Accountancy is a synthetic accountancy firm and benchmark. It is not accounting software or professional advice.</p></footer></body></html>"""


_STYLES = """
:root { color-scheme: light; --ink:#132026; --blue:#0a5367; --paper:#f7f7f4; --line:#c9d1d0; --accent:#f4b942; }
* { box-sizing:border-box; } body { margin:0; font-family:ui-serif, Georgia, serif; color:var(--ink); background:var(--paper); line-height:1.55; } header, main, footer { max-width:1120px; margin:auto; padding-left:24px; padding-right:24px; } header { display:flex; justify-content:space-between; align-items:center; padding-top:20px; padding-bottom:20px; font-family:ui-sans-serif, system-ui, sans-serif; } header nav { display:flex; gap:18px; flex-wrap:wrap; } a { color:#064f61; } .brand { font-weight:800; color:var(--ink); text-decoration:none; } main { padding-top:42px; padding-bottom:64px; } section, article { margin-bottom:64px; } .hero { max-width:760px; } h1 { font-size:clamp(2.35rem, 6vw, 4.75rem); line-height:1; letter-spacing:-.045em; margin:.12em 0 .38em; } h2 { font-size:clamp(1.65rem, 3vw, 2.4rem); line-height:1.12; } h3 { font-size:1.35rem; } .eyebrow { font-family:ui-sans-serif, system-ui, sans-serif; font-weight:750; font-size:.78rem; letter-spacing:.12em; text-transform:uppercase; color:var(--blue); } .lede { font-size:1.25rem; } .actions { display:flex; gap:12px; flex-wrap:wrap; margin-top:30px; } .button, button { display:inline-block; appearance:none; border:0; border-radius:3px; background:var(--blue); color:white; font:700 1rem ui-sans-serif, system-ui, sans-serif; padding:12px 16px; text-decoration:none; cursor:pointer; } .button.secondary { color:var(--blue); background:transparent; outline:1px solid var(--blue); } form { background:white; border:1px solid var(--line); padding:24px; max-width:760px; display:grid; gap:14px; } label { display:grid; gap:5px; font-family:ui-sans-serif, system-ui, sans-serif; font-weight:650; } fieldset { border:0; padding:0; display:flex; gap:16px; flex-wrap:wrap; } fieldset label { display:inline-flex; grid-template-columns:auto 1fr; align-items:center; font-weight:400; } input, textarea { max-width:100%; min-height:42px; padding:9px; border:1px solid #768888; border-radius:3px; font:1rem ui-sans-serif, system-ui, sans-serif; } textarea { min-height:96px; resize:vertical; } .privacy, .note { font-size:.92rem; } .honeypot { position:absolute; left:-10000px; width:1px; height:1px; overflow:hidden; } .filters { display:flex; gap:8px; margin:20px 0; } .filter { padding:7px 12px; border:1px solid var(--blue); text-decoration:none; border-radius:999px; font-family:ui-sans-serif, system-ui, sans-serif; } .filter.selected { background:var(--blue); color:white; } .table-wrap { overflow-x:auto; background:white; border:1px solid var(--line); } table { border-collapse:collapse; min-width:1080px; width:100%; font: .88rem ui-sans-serif, system-ui, sans-serif; } caption { text-align:left; font-weight:750; padding:14px; } th, td { padding:10px; border-top:1px solid var(--line); text-align:left; vertical-align:top; } th { background:#eef4f2; } .empty { padding:24px; background:white; border:1px solid var(--line); } pre { padding:16px; overflow:auto; background:#10212a; color:#f7f7f4; } footer { border-top:1px solid var(--line); padding-top:26px; padding-bottom:26px; font-size:.9rem; } .skip { position:absolute; left:-999px; top:0; background:white; padding:8px; } .skip:focus { left:10px; z-index:10; } a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible { outline:3px solid var(--accent); outline-offset:3px; } @media (max-width:700px) { header { align-items:flex-start; flex-direction:column; gap:12px; } main { padding-top:22px; } form { padding:16px; } }
"""
