# Security policy

## Scope and supported versions

Mirror Firm is a local, synthetic evaluation environment. The supported version is the
latest released `0.1.x` version on the default branch; older versions may not receive
security fixes. This policy does not offer an SLA or a guarantee of suitability for
production accounting work.

Do **not** connect Mirror Firm to real client data, real ledgers, payment systems,
filing systems, or professional-services workflows. Do not treat any output as
accounting, tax, legal, or audit advice.

## Reporting a vulnerability

Please report a potential vulnerability privately through the repository host's
maintainer-contact or private security-reporting mechanism, if it is enabled. Include a
minimal reproduction, affected revision, impact, and any prerequisites. Do not include
real client data, credentials, or exploit payloads that would expose third parties.

This repository deliberately does not invent a security email address. If private
reporting is unavailable, open the least-sensitive issue necessary to request a secure
contact channel; avoid publishing a working exploit before maintainers respond.

## Threat model

Trusted components are the reviewed world compiler, world engine, deterministic
graders, fixture files after review, and locally installed dependencies. Untrusted
inputs include model responses, tool-call arguments, documents, imported APEX files,
result/report input artifacts, requested output paths, and live-provider text.

The project is not designed to protect against an attacker who can rewrite every local
trust anchor, the Python interpreter, and the source tree. Its artifact checks provide
internal consistency and resistance to common mutable-path, substitution, malformed
input, and accidental-leak failures—not signatures or remote attestation.

## Implemented controls

### Model and document inputs

- Agents receive typed tool schemas, not a shell or filesystem-write tool. Documents
  are data, never instructions; the harness system prompt and injection tests make that
  boundary explicit.
- Document parsing follows the minimal sandbox pattern. `SANDBOX=podman` is expected in
  CI where a container runtime is available (`--network=none`, dropped capabilities,
  read-only inputs). `SANDBOX=none` is a local-development fallback, not equivalent
  isolation.
- Malformed calls, unknown tools, permission failures, and rejected calls are charged
  against the episode budget and recorded as Actions.

### Accounting and world safety

- Every tool is scoped by client **and engagement**. Cross-scope lookup, mutation,
  approval, event, provenance, and messaging paths reject mismatches. A successful
  foreign-record leak is a CF-5 critical failure.
- Lifecycle transitions are centralised; review and system events use the same legal
  transition rules.
- Posting and approval-gated external messages cannot be called directly by the agent.
  A granted approval causes an explicit system Action, bound to the immutable approved
  draft state where messaging is involved.
- Actions are append-only. Initial/final SQLite snapshots are bound to execution
  provenance, terminal completion, digest, and artifact hashes. Failed terminal
  snapshot writes do not make an episode terminal.

### Filesystem, output, and artifact handling

- World/import paths reject traversal, Windows paths, Unicode separator confusables,
  ambiguous normalisation, duplicate flattening, dangling references, and incompatible
  ownership before compilation or installation.
- Pack manifests, lock files, task artifacts, and runtime assets are read once from
  no-follow regular-file descriptors and verified before use.
- Run roots, snapshot directories, aggregates, and reports retain no-follow directory
  descriptors and ancestor identities. Checkpoints detect rename, unlink, substitution,
  symlink, and TOCTOU races; cleanup occurs only through retained safe descriptors.
- Result artifacts use canonical sanitized JSON and atomic no-overwrite publication.
  Missing, modified, symlinked, duplicated, or inconsistent required artifacts fail
  closed before evaluation, comparison, reporting, or aggregation.

### Secrets, answer keys, and external data

- Provider credentials belong only in environment variables used to construct an
  adapter. Public run configuration is allowlisted; credentials are neither persisted
  nor hashed.
- Structured transcript data, messages, tool inputs/outputs, errors, summaries, and
  report text pass through a central sanitizer. Credential-like keys, bearer values,
  PEM material, and assignment-style secrets are redacted before persistence or hashes.
- UK/US answer keys are evaluator-only and are not injected into model-visible context,
  ordinary MCP results, transcripts, or reports.
- APEX public development packs are explicitly contaminated by public reference answers.
  Gold output stays out of normal agent and judge paths, and `public-reference-answers`
  results are barred from native aggregation and comparison.

## Known limitations

- Local files and output locations remain within the security of the host operating
  system and the current user. The checks above are not a sandbox for arbitrary hostile
  native code.
- `SANDBOX=none` does not isolate document parsing; use the documented container mode
  where appropriate.
- Provider adapters necessarily send selected synthetic prompts and tool context to the
  chosen live provider. Do not supply real or confidential material.
- Qualitative live-model judging can disagree or require review. Deterministic safety
  and accounting controls are intentionally separate, but no software test suite proves
  all possible unsafe accounting behaviours are absent.
- Public APEX gold inspection is intentionally available only by an explicit command;
  its availability means that pack is unsuitable for held-out measurement.

See [SPEC §L](SPEC.md#l-security-and-privacy-model) and [docs/evaluation.md](docs/evaluation.md)
for the normative boundaries and critical-failure definitions.
