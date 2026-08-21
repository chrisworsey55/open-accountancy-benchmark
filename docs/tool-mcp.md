# Tool and MCP contract

Mirror Firm exposes exactly **37 stable tools**. A single registry defines each tool's
name, Pydantic input/output models, role requirements, duration, mutation metadata,
approval gate, and error handling. The local MCP server derives its tool list and JSON
schemas from that registry; every stable output has its own typed model.

The agent has no bash, arbitrary file write/delete, direct ledger-write, payment,
filing, or cross-client parameter tool. Read/compute tools are engagement-scoped;
mutations are permissioned and logged as Actions.

## Tool families

- Context, time, clients, tasks, documents, threads, approvals, review notes, bank,
  ledger, trial balance, and reconciliation read tools.
- Pure `aggregate_table`, `calculate`, and `compare_datasets` computation tools.
- Classification, proposed-journal, draft/approval, workpaper/review, task, escalation,
  and time-advance workflow tools.
- Explicit terminal `finish_episode`.

The normative field-by-field contracts are [SPEC §F](../SPEC.md#f-tool-contracts). Use
the generated JSON schemas in `schemas/` for non-Python consumers. Do not infer a tool
input from a fixture ID: agents must obtain records from context or earlier typed output.

## Key safety semantics

- No direct agent posting or agent auto-send after approval; a granted approval creates
  an explicit, audited system Action.
- Approved draft messages are bound to full send-relevant content. Any mutation causes
  expiry or prevents auto-execution.
- Every requested tool call consumes step budget, including forbidden or malformed calls.
- Results are client-and-engagement scoped. A denied attempt logs a typed error; a
  successful foreign-data leak is CF-5.
- `finish_episode` captures a final snapshot only after safe terminal persistence.

For example, inspect provider-visible schemas through any standard MCP client or the
local registry; do not add a second APEX-specific interpretation of these contracts.
