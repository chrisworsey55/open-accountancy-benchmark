# Development and deterministic builds

## Setup and checks

```sh
make setup
uv lock --check
make docs
make fmt
make lint
make type
uv run mypy --config-file=/dev/null --strict mirrorfirm
make test
```

`make fmt` is check-only; use `uv run ruff format .` intentionally when formatting a
change. The CI workflow runs lock verification, check-only formatting, lint, project and
config-free strict typing, documentation checks, and tests.

## Determinism

World compilation canonicalises logical state and compares independent builds. Reference
tests run each authored UK/US trajectory twice from fresh copies and require matching
Actions and final digest. Result artifacts use canonical JSON, sorted rows, public
configuration hashes, and verified initial/final snapshot bindings. Measured wall-clock
latency is intentionally retained as usage evidence, so otherwise identical live or
scripted executions need not have byte-identical scorecards.

The APEX importer uses a user-pinned Hugging Face revision and stores source/runtime/task
hashes. Its optional real-source test requires a local `MIRRORFIRM_APEX_REAL_SOURCE`
path; it is intentionally skipped when that download is unavailable.

## Build and installation

```sh
uv build
```

The wheel includes the authored `worlds/`, `episodes/`, `jurisdictions/`, and generated
`schemas/` resources required by the installed CLI. Verify a built distribution in a
fresh temporary environment before release. Build outputs under `dist/` are ignored and
must not be committed.

`make release-check` is a source-only checklist for docs, ignored generated outputs,
developer paths, credential canaries, and required wheel resource configuration. It does
not replace the dynamic tests or prove host-level security.
