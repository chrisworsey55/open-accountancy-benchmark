# Developer alpha release status — 6 September 2026

The release candidate exposes fictional workflows, a scripted demo, source and contribution
rules. It does not claim a ranked model result, practitioner endorsement, or savings.
The dated 1 September documents are historical planning records, not launch approval.

## Evidence gates

- Scoring revision: `wp09-v2`. Both worlds: `0.1.1`.
- Corrected statement/cash-book sources and posted ledger fixtures are versioned with
  their expectations. All seven controls must pass before the candidate is packaged.
- Regression gates cover wrong amounts, unrelated evidence, signed residuals,
  equivalent line order/wording, MCP lifecycle/scope, cohort separation and unknown effort.
- Source/history scan, clean install, format, lint, type, documentation, test and demo
  checks must be recorded against the exact published source revision.
- Practitioner review: pending; use `PRACTITIONER_REVIEW.md`.
- Real-agent baselines: pending agreed spend cap and practitioner-reviewed scoring freeze.
- Domain activation and browser QA depend on access to the registrar/browser session.

## Real-agent baselines

Use two real provider configurations on the same seven episodes with five fresh runs
each (70 attempts), identical declared judges and budgets. Start with one paired pilot
to estimate API cost. Obtain a total budget before paid calls. Record provider pricing
and actual usage, including judges and failures; do not claim a hard dollar limit from
token limits alone. Stop before a run whose worst-case allocation exceeds the remaining
approved budget. If this cannot be bounded, do not start it.

Publish the full sweep manifest, successful and failed attempts, source revision,
prompts/adapters, model IDs, judge configuration, seeds and limitations. Five runs meet
the baseline policy; they do not establish production reliability. Cost and accountant
effort must be marked unavailable when they are not measured.

The developer alpha can invite contributions while these evidence gates remain pending.
A promoted ranked-results campaign must wait for them. No social post is sent by the
release scripts.
