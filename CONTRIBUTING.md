# Contributing to Franklin & McGrath

Franklin & McGrath is a public synthetic evaluation environment. Contributions must preserve
the normative architecture in [SPEC.md](SPEC.md), use fictional data only, and include
targeted tests. The project does not accept real client records, credentials, production
traces, private provider prompts, or code from private systems.

## Local setup

```sh
make setup
make docs
make fmt
make lint
make type
make test
```

Use Python 3.12+ through `uv`. Run `uv lock --check` after dependency changes. Generated
results belong under `results/` or a temporary directory and must never be tracked.

## Contribution paths

- **Worlds:** follow [world and episode authoring](docs/authoring-worlds.md). New
  fixture records require explicit client/engagement ownership, document hashes, traps
  where appropriate, deterministic compilation, and validation gates.
- **Episodes:** add a manifest, complete answer key, evaluator-safe reference trajectory,
  evidence-discoverability tests, determinism tests, and critical-failure coverage. Do
  not make answer keys or trajectories model-visible.
- **Tools and schemas:** the registry defines the stable 37-tool contract. Do not rename
  tools or weaken output models, ownership, approval, provenance, or typed error
  boundaries without an approved specification amendment.
- **Graders and reports:** deterministic rules must be generic and fixture-independent.
  APEX/public-reference-answer artifacts cannot enter native aggregates.
- **Documentation:** keep documented commands executable, distinguish scripted references
  from model performance, and retain required Harvey/APEX attribution.

## Review checklist

Before requesting review, run formatting, lint, strict typing, tests, schema freshness,
and the relevant world/reference checks. Add a regression test for every fixed defect.
Avoid unrelated formatting changes. Never commit downloaded APEX content, generated
databases, provider credentials, result artifacts, or hidden answer data.

For security-sensitive reports, follow [SECURITY.md](SECURITY.md) rather than opening a
public issue with sensitive details.
