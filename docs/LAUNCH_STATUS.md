# Developer alpha release status — 11 September 2026

Open Accountancy (formerly Franklin & McGrath), built by General Intelligence
Holdings, is preparing `v0.1.2-alpha.1`. The release candidate exposes fictional workflows, a scripted demo, source and contribution
rules. It does not claim a ranked model result, practitioner endorsement, or savings.
The dated 1 September documents are historical planning records, not launch approval.

## Evidence gates

- Scoring revision: `wp09-v3`. Both worlds: `0.1.1`.
- Corrected statement/cash-book sources and posted ledger fixtures are versioned with
  their expectations. All seven controls must pass before the candidate is packaged.
- Regression gates cover wrong amounts, unrelated evidence, signed residuals,
  equivalent line order/wording, MCP lifecycle/scope, cohort separation and unknown effort.
- Source/history scan, clean install, format, lint, type, documentation, test and demo
  checks must be recorded against the exact published source revision.
- Practitioner review: pending; use `PRACTITIONER_REVIEW.md`.
- Real-agent experiments: use the existing ChatGPT subscription through Codex CLI; no paid API calls are authorised for this release.
- Ranked real-agent baselines: pending practitioner-reviewed scoring freeze and verified comparable execution conditions.
- Browser QA: developer links, mobile navigation/replay, and fictional contact submission verified on the live website.
- Domain activation and partner intake: check the live website at launch time;
  the source release does not establish website or customer-onboarding readiness.

## Real-agent baselines

For this release, use ChatGPT-authenticated Codex for real-agent experiments. Force
ChatGPT authentication and exclude API credentials from the runner environment. Keep
user plugins, external connectors, shell access and reference files out of the evaluated
agent's tools; expose only the scoped fictional MCP server. Record the Codex version,
model, reasoning effort, prompts, outputs, failures and CLI-reported usage.

Five fresh attempts per configuration and episode are the target for repeatability.
MCP experiments remain unranked because the external client's token budget is not
controlled by the benchmark. Do not retrofit them into API-provider baseline cohorts.
Observe subscription limits; do not buy API credits or redeem usage resets implicitly.

Publish the full sweep manifest, successful and failed attempts, source revision,
prompts/adapters, model IDs, judge configuration, seeds and limitations. Five runs meet
the baseline policy; they do not establish production reliability. Cost and accountant
effort must be marked unavailable when they are not measured.

The developer alpha can invite contributions while these evidence gates remain pending.
A promoted ranked-results campaign must wait for them. No social post is sent by the
release scripts.

## Scoring v3

The first subscription pilot exposed missing workflow evidence in the summary judge.
The judge now receives actual task and review-note state, and communication targets
exclude unchanged historical output and other engagements. Binary prompts request the
verdict field that the parser requires. Earlier scores retain their original version.
