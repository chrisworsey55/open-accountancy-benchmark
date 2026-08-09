# Mirror Firm agent instructions

Read `SPEC.md` completely before making changes. Treat it as the normative product and
architecture specification.

Work on only the work packet explicitly assigned in the current request. Do not begin a
later packet, redesign stable interfaces, or add adjacent product scope. If the current
packet conflicts with the specification or depends on an unavailable earlier packet,
stop and report the conflict.

Preserve unrelated existing work and inspect the working tree before editing. Do not
delete, overwrite, stage, commit, push, publish, deploy, or open a pull request unless
the user explicitly authorises that action in the current session.

Use only fictional scenario data. Never add real or confidential client, person, firm,
credential, financial, or production data. Public repository names and required
third-party attribution are permitted.

Retain all upstream licences, source notices, commit pins, and per-file attribution
required by `SPEC.md`. Take no code from Mercor Archipelago or Thrive's private systems.

Implement tests with every feature. Before finishing, run the formatting, linting, type
checking, and test commands available for the current packet. Report the exact commands
and outcomes. Never claim a check passed if it was not run.

Prefer the smallest implementation that satisfies the assigned packet. Do not invent
credentials, fabricate verification, bypass permission failures, or make destructive
changes to overcome a blocker.
