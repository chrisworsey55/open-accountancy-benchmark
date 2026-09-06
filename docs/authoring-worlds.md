# Authoring fictional worlds and episodes

Only fictional scenario data belongs in Franklin & McGrath. Do not add real people, firms,
addresses, transactions, documents, credentials, production traces, or client material.

## World workflow

1. Copy a small existing world structure or the documented fixture pattern.
2. Write `world.yaml`, practice/client fixtures, records, bank feeds, and fictional
   documents. Every document file must agree with its authored hash record.
3. Give each engagement-scoped record explicit compatible client and engagement
   ownership. Do not rely on nullable ownership when a client has multiple engagements.
4. Add traps for deliberate exceptions, including discoverability and grading refs.
5. Add valid event definitions. Template bindings must resolve existing typed fields,
   record families, scope, and UTC-aware timestamps at compilation time.
6. Run `mirror-firm validate --world <world>` and repair every failed gate.

The compiler requires schema validity, references, accounting invariants, trap coverage,
ownership, deterministic digest equality, and—where an authored episode targets the
world—reference-run and answer-key gates.

## Episode workflow

An episode manifest pins world ID/version, engagement, agent, instruction, allowed
tools, events, budget, deliverables, deterministic criteria, qualitative criteria,
critical failures, expected state, and a scripted reference digest. Add an adjacent
answer key that exactly matches its deterministic and expected-state assertions.

The scripted reference can encode the expected trajectory but must use only the agent's
allowed tools. Every material ID used by a reference must be discoverable from initial
model-visible context or an earlier successful typed MCP output. Answer keys and
reference-only qualitative expectations must not become model-visible.

## Tests expected with authored content

- structural `validate` success and double-compile digest equality;
- reference score 1.0 with zero critical failures;
- two fresh runs with identical Actions and final digest;
- evidence-lineage and required-read-path failure controls;
- positive and negative ownership, lifecycle, approval, provenance, and isolation tests;
- a forcing test for any activated critical-failure or trap boundary.

Read [architecture](architecture.md), [evaluation](evaluation.md), and
[CONTRIBUTING.md](../CONTRIBUTING.md) before contributing.
