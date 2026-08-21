# Adding a fictional jurisdiction

A jurisdiction pack supplies terminology, chart-of-accounts templates, tax treatment,
business calendars, holidays, and local date/currency rendering. It does not add a
second engine, an alternative tool contract, or real accounting data. The stable
37-tool surface and the Pydantic schemas remain jurisdiction-neutral.

1. Add a fictional pack under `jurisdictions/` with explicit tax codes, rates, and
   rounding cases. Use deterministic `Decimal` values and UTC-aware calendar behaviour.
2. Add fictional worlds that declare the pack, client/engagement ownership, authored
   documents, opening balances, events, and traps. Do not reuse real firm or client
   material.
3. Add episodes, answer keys, and evaluator-only scripted references. A reference may
   know the intended path, but every ID it uses must first be available to a model
   through initial context or an allowed typed tool result.
4. Add derivation, calendar/DST, ownership, isolation, provenance, critical-failure,
   reference-run, and double-compile determinism tests.

Run `mirror-firm validate --world <world-id>` before submitting the pack. It checks
structural ownership, accounting invariants, event templates, answer-key/reference
gates, and deterministic compilation. New packs must preserve the global schemas,
action log, logical-state digest, and deterministic evaluation contract.

See [world authoring](authoring-worlds.md), [evaluation](evaluation.md), and
[contributor guidance](../CONTRIBUTING.md).
