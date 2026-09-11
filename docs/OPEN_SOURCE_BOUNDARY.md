# Franklin & McGrath open-source boundary

## Public preparation scope

Subject to a final licensing and secret audit, the public repository contains benchmark contracts and schemas, the local runner and CLI, agent integration interfaces, synthetic UK and US sample workflows and evidence, scripted references, public scoring interfaces, non-sensitive graders, result artifacts, leaderboard transformation rules, versioning rules, quick-start documentation, contribution guidance, architecture notes, and the credential-free demo.

The intended public quick start is `make setup` followed by `make demo`; the checked-in documentation should make that useful within approximately ten minutes on a supported machine.

## Private by design

Do not publish held-out episodes, hidden answer keys, anti-gaming cases, private judge prompts, the full production test corpus, lead registrations, unpublished submissions, execution infrastructure, sandbox controls, operational analytics, private admin tools, secrets, credentials, deployment infrastructure, or real client data.

The scoring principles remain public even where specific held-out cases are not.

## Publication audit

Before any repository-visibility change, Chris must run the release gate, inspect all untracked files, verify licence/notice obligations, inspect the configured lead database is outside the repository, and conduct the required human review for accidental real-data inclusion. This task does not publish the repository, package, results, or deployment.

## Launch release disposition — 11 September 2026

This selected source release includes the public development cases and their answer
keys, deterministic references, generic trace/correction schemas and development
judges. These are not hidden evaluation assets.

The existing local WSGI presentation and SQLite lead-storage helper remain public
compatibility code. They contain no deployed database or actual applications. Live
marketing source and lead records stay in the separate private website project.
The local helper is not the production intake service. No real registrations, client
exports, private app source or private evaluation packs belong in a source archive.
