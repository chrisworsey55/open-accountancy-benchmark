# Architecture and data flow

Mirror Firm is a local, deterministic stateful evaluation environment. Authored fixture
files are the source of truth; the runtime never mutates them.

```text
fictional world fixtures + jurisdiction pack
                │
                ▼
       deterministic compiler ──► compiled SQLite world + logical digest
                │                              │
                ▼                              ▼
episode manifest + allowed tools      fresh run copy and initial StateSnapshot
                │                              │
                └────────► harness / local MCP tool engine
                                               │
                                               ▼
                                Actions, events, final StateSnapshot
                                               │
                                               ▼
                         deterministic graders + qualitative judge boundary
                                               │
                                               ▼
                           E.17 EvaluationResult, verified JSON, offline HTML
```

## World state

Worlds are authored in repository-level YAML, CSV, and fictional document files. The
compiler validates schema, references, ownership, accounting invariants, traps, event
templates, and double-compilation logical digest equality before a world is usable.
The resulting SQLite database is copied for every run. A `StateSnapshot` captures the
initial and final database state, and its logical digest is canonical rather than SQLite
byte-level identity.

Every client-owned operational record is resolved through the active client and
engagement. This prevents same-client cross-engagement access as well as cross-client
access. The world clock advances only through approved tool durations or `advance_time`;
events fire deterministically when crossed.

## Tool and harness boundary

The registry owns the stable typed tool definitions. The local MCP server renders those
same definitions, so a model sees only the episode's allowed subset. The harness starts
from a fresh compiled copy, sends provider messages and MCP calls through the adapter,
records an append-only Action for every call, and requires explicit `finish_episode`.

An episode cannot become terminal merely because a completion call was attempted. The
terminal Action, final snapshot, state digest, and execution identity are bound together;
collision or write failure leaves the engine resumable and logs a failed Action instead.

## Evaluation and persisted results

Evaluation first applies critical-failure checks, then deterministic graders, then an
optional single or dual qualitative judge. The stable E.17 `EvaluationResult` remains
the per-run score. WP-13 stores it with a separate verified provenance envelope instead
of changing the stable schema. Aggregates and reports consume verified artifacts rather
than rerunning episodes.

Output directories use no-follow descriptors, containment checks, identity checkpoints,
and atomic no-overwrite publication. The security design detects common path replacement
and substitution races but is not a signature scheme against replacement of every local
trust anchor.

See [tool/MCP contracts](tool-mcp.md), [evaluation](evaluation.md), and
[SECURITY.md](../SECURITY.md).
