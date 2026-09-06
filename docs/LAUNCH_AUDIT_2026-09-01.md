# Franklin & McGrath launch audit — 1 September 2026

## Repository snapshot

- **Repository root:** the local workspace root (the exact absolute path is reported to the operator, not committed into the public release candidate).
- **Initial branch / commit:** `main` at `9e70660d54d2ba30324c67326c164fd271003e63` (`release: add public documentation and deterministic demo`)
- **Remotes:** none configured.
- **Initial working tree:** clean; no modified or untracked files before this audit began.
- **Recent delivery history:** WP-10 UK references (`9caa444`), WP-11 US workflows (`3085cdb`), WP-12 APEX (`488a53f`), WP-13 reporting/CLI (`3cea4b2`), WP-14 documentation/demo (`9e70660`).
- **Package:** `mirror-firm` 0.1.0, Python 3.12+, uv-managed; package directory `mirrorfirm`; console command `mirror-firm` remains a documented legacy developer command.
- **Entry points:** `mirrorfirm.cli:main`; `scripts/run_demo.py`; `scripts/check_docs.py`; the optional local launch app is `mirror-firm launch serve`.
- **Architecture:** Python local-first benchmark. YAML/CSV/fictional documents compile deterministically to SQLite; the harness uses local MCP tools; evaluation writes verified JSON and offline HTML. The launch layer is an optional dependency-free WSGI surface over verified aggregate artifacts.
- **Storage:** benchmark-world SQLite snapshots and local result artifacts; optional separate SQLite lead store configured by `FRANKLIN_MCGATH_LEADS_DB`.
- **Deployment:** no host, domain, deployment configuration, analytics, error-reporting provider, form provider, or remote database is configured.
- **Tests:** final `make test` collected 562 items: 555 passed and 7 skipped. Focused launch tests cover public routes, forms, result extraction, ties, filters, critical failures, malformed files, and scripted-reference treatment.
- **Benchmark scope verified from repository:** two UK/US jurisdiction packs, seven native episodes, a 37-tool registry, deterministic graders, CF-1 through CF-10, result artifacts, and reference trajectories.

## Audit matrix

| Area | Status | Evidence | Remaining action | Priority | Codex fixed? |
|---|---|---|---|---|---|
| Repository integrity | GREEN | Initial `git status --short` was clean; commit and history recorded above. | Keep launch changes reviewed and uncommitted until approved. | P0 | No |
| Test status | GREEN | Final `make test`: 555 passed, 7 skipped (562 collected). | None. | P0 | No |
| Credential-free demo | GREEN | Final `make demo`: EP-UK-01 scripted reference, overall 1.000, all-pass true, zero CFs. | None. | P0 | No |
| Benchmark runner | GREEN | `mirrorfirm/harness/episode_runner.py`; reference and harness suites pass. | None. | P0 | No |
| UK environment | GREEN | `worlds/uk-wyrley-brook`, four UK episode manifests, reference/world tests. | None. | P0 | No |
| US environment | GREEN | `worlds/us-lakeshore`, three US episode manifests, reference/world tests. | None. | P0 | No |
| Accounting workflows | GREEN | Seven manifests in `episodes/uk` and `episodes/us`; reference suites. | None. | P0 | No |
| Tool execution | GREEN | 37-tool registry and tool-engine tests. | None. | P0 | No |
| Evaluation and grading | GREEN | Deterministic graders, scoring, qualitative boundary, and tests. | None. | P0 | No |
| Critical-failure handling | GREEN | CF-1 through CF-10 forcing tests and zeroing logic. | None. | P0 | No |
| Evidence and provenance | GREEN | Provenance/state graders and integrity remediation tests. | None. | P0 | No |
| Approval and human-review tracking | GREEN | Approval lifecycle, review criteria, and US approval-chain tests. | Human-review amount remains non-canonical for public display. | P1 | No |
| Cost tracking | GREEN | `Usage.cost_usd` in E.17 and aggregate artifacts. | None. | P1 | No |
| Results persistence | GREEN | Verified atomic run/aggregate artifacts and WP-13 tests. | Configure a reviewed artifact root before public display. | P0 | No |
| Public branding | GREEN | README, documentation, reports, demo output, launch routes, and metadata use Franklin & McGrath. | Retained internal names are documented. | P0 | Yes |
| Homepage messaging | GREEN | `mirrorfirm/launch/web.py` and launch-route tests contain the agreed proposition and CTAs. | Deploy the app. | P0 | Yes |
| Public benchmark documentation | GREEN | README plus architecture, evaluation, tooling, running-model, and launch documents. | Publish only after human review. | P0 | Yes |
| Leaderboard data model | GREEN | `mirrorfirm/launch/leaderboard.py` reads verified aggregate artifacts without inventing fields. | None. | P0 | Yes |
| Leaderboard generation | GREEN | Deterministic scanner discovers canonical hashed CLI aggregates and malformed-artifact handling is tested. | Point at reviewed artifacts. | P0 | Yes |
| Leaderboard user interface | GREEN | `/leaderboard` WSGI route, tables, empty state, reference section, and route tests; local server returned 200 against the demo aggregate. | Deploy the app. | P0 | Yes |
| UK and US leaderboard filtering | GREEN | `Leaderboard.for_jurisdiction`, route filter links, and tests. | None. | P0 | Yes |
| Result methodology and transparency | GREEN | `docs/LEADERBOARD_METHODOLOGY.md` and `/methodology`. | None. | P0 | Yes |
| Developer registration | AMBER | Validated `/register/developer` journey with local SQLite persistence and tests. | Configure durable production storage and a deployed WSGI host. | P0 | Yes |
| Accountancy-firm workflow registration | AMBER | Validated `/register/workflow` journey with local SQLite persistence and tests. | Configure durable production storage and a deployed WSGI host. | P0 | Yes |
| Form validation and spam protection | GREEN | Server-side required fields, email/URL checks, bounded body, consent, honeypot, and tests. | Add production rate limiting/WAF at the host. | P1 | Yes |
| Lead persistence | AMBER | `LeadStore` uses separate SQLite tables, duplicates are safe, and failure never confirms success. | Set `FRANKLIN_MCGATH_LEADS_DB` outside repo, restrict access, back up, and verify in production. | P0 | Yes |
| Privacy wording | AMBER | Explicit form wording and no-public-exposure behavior. | Approve privacy notice, retention period, contact channel, and deletion process. | P0 | Yes |
| Mobile experience | GREEN | Responsive CSS and native form controls in launch routes. | Perform production-device check. | P1 | Yes |
| Accessibility | AMBER | Semantic landmarks, labels, native keyboard controls, skip link, focus states, table captions, and tests. | Manual keyboard/contrast check on deployed domain. | P1 | Yes |
| Metadata and social sharing | GREEN | Title, description, Open Graph, and Twitter summary metadata in all launch pages. | Add a production social image if desired. | P1 | Yes |
| Error handling | GREEN | 400, 404, 405, 413, and truthful 503 form states are covered by route tests; local `/health` returned `status: ok`. | Configure host-level error logging. | P1 | Yes |
| Logging and observability | RED | No analytics or error-reporting integration exists. | Choose and configure privacy-preserving host/app error monitoring. | P1 | No |
| Production build | AMBER | Python package, documentation check, and route tests are build-free and local. | Define the production WSGI image/build in the chosen hosting platform. | P0 | No |
| Deployment configuration | RED | No deployment target, domain, HTTPS setup, or host configuration exists. | Chris must choose and configure hosting, DNS, TLS, WSGI process management, and persistent volumes. | P0 | No |
| Open-source release boundary | AMBER | `docs/OPEN_SOURCE_BOUNDARY.md`, existing security/notice controls, and release gate. | Complete human data/licensing audit before changing repository visibility. | P0 | Yes |
| Launch checklist | GREEN | `docs/LAUNCH_CHECKLIST_2026-09-01.md`. | Complete every unchecked manual item. | P0 | Yes |
| Launch communications | GREEN | `docs/LAUNCH_COPY.md` contains the exact agreed four sentences and social copy. | Use only after final verification. | P1 | Yes |

## Current launch blockers

The benchmark itself is verified locally. Public launch remains blocked by external production configuration: a deployed HTTPS WSGI host, durable access-controlled lead storage, a privacy/retention decision, a reviewed results root, and host-level error monitoring. The repository has no remote configured, so neither deployment nor publication is implied by this audit.
