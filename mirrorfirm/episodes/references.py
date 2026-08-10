"""Deterministic, MCP-routed UK reference trajectories for WP-10."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from mirrorfirm.core.models import EpisodeManifest, EvaluationResult
from mirrorfirm.episodes.manifests import reference_qualitative_expectations
from mirrorfirm.evaluation import evaluate_run, write_scores
from mirrorfirm.evaluation.qualitative import (
    REFERENCE_VALIDATION_MODEL,
    ReferenceQualitativeValidator,
)
from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)
from mirrorfirm.harness.episode_runner import EpisodeRunner, EpisodeRunResult
from mirrorfirm.tools.schemas import JsonObject


class ReferenceScriptError(RuntimeError):
    """A deterministic reference trajectory did not complete as authored."""


ArgumentBuilder = Callable[[Mapping[str, JsonObject]], JsonObject]
ReferenceArguments = JsonObject | ArgumentBuilder


@dataclass(frozen=True)
class ReferenceCall:
    """One named tool call in an authored deterministic reference trajectory."""

    call_id: str
    name: str
    arguments: ReferenceArguments


@dataclass(frozen=True)
class ReferenceEpisodeResult:
    """The runner and evaluator outputs of one local scripted reference run."""

    run: EpisodeRunResult
    evaluation: EvaluationResult


class ScriptedReferenceAdapter(ModelAdapter):
    """A deterministic provider stub which still drives the normal agent loop/MCP path."""

    def __init__(self, calls: Sequence[ReferenceCall]) -> None:
        super().__init__("reference-scripted")
        self._calls = tuple(calls)
        self._cursor = 0
        self._pending: dict[str, str] = {}
        self._results: dict[str, JsonObject] = {}
        self._observed_messages: list[ProviderPayload] = []

    @property
    def observed_messages(self) -> tuple[ProviderPayload, ...]:
        """Expose test-only provider context captured before reference validation."""

        return tuple(self._observed_messages)

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        """Emit exactly one reference tool call on each model turn."""

        self._observed_messages.extend(messages)
        del tools
        if self._cursor >= len(self._calls):
            return ModelResponse(
                message={"role": "assistant", "content": "Reference complete."}
            )
        call = self._calls[self._cursor]
        self._cursor += 1
        arguments = (
            call.arguments(self._results)
            if callable(call.arguments)
            else call.arguments
        )
        tool_call_id = f"reference-{call.call_id}"
        self._pending[tool_call_id] = call.call_id
        return ModelResponse(
            message={"role": "assistant", "content": "Reference tool call."},
            tool_calls=[
                ToolCall(
                    id=tool_call_id,
                    name=call.name,
                    arguments=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                )
            ],
            input_tokens=1,
            output_tokens=1,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        """Capture typed MCP results for later dynamic reference arguments."""

        captured: list[tuple[str, JsonObject]] = []
        for tool_call_id, raw_result in results:
            try:
                call_id = self._pending.pop(tool_call_id)
            except KeyError as error:
                raise ReferenceScriptError(
                    "reference received an unknown tool result"
                ) from error
            result = _json_object(json.loads(raw_result))
            if result.get("isError") is True:
                raise ReferenceScriptError(
                    f"reference call {call_id!r} failed: {_error_message(result)}"
                )
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                raise ReferenceScriptError(
                    f"reference call {call_id!r} returned malformed MCP content"
                )
            typed = _json_object(structured)
            self._results[call_id] = typed
            captured.append((tool_call_id, typed))
        return [
            {
                "role": "tool",
                "results": [
                    (
                        tool_call_id,
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                    )
                    for tool_call_id, result in captured
                ],
            }
        ]

    def make_system_message(self, content: str) -> ProviderPayload:
        """Build the retained Harvey-compatible system message envelope."""

        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        """Build the retained Harvey-compatible user message envelope."""

        return {"role": "user", "content": content}


def run_reference_episode(
    episode: EpisodeManifest,
    compiled_database_path: str | Path,
    *,
    world_root: str | Path,
    results_root: str | Path,
    run_id: str | None = None,
) -> ReferenceEpisodeResult:
    """Execute and evaluate one authored script through the WP-08/09 boundaries."""

    expected_script = _REFERENCE_SCRIPT_PATHS.get(episode.episode_id)
    if episode.reference.reference_script != expected_script:
        raise ReferenceScriptError(
            f"reference script path for {episode.episode_id!r} is not registered"
        )
    resolved_run_id = run_id or f"run-reference-{episode.episode_id}"
    runner = EpisodeRunner(
        episode,
        compiled_database_path,
        world_root=world_root,
        results_root=results_root,
    )
    run = runner.run(
        ScriptedReferenceAdapter(reference_calls_for(episode.episode_id)),
        run_id=resolved_run_id,
    )
    if not run.completed:
        raise ReferenceScriptError(
            f"reference script for {episode.episode_id!r} did not finish the episode"
        )
    if run.final_snapshot.state_digest != episode.reference.final_state_digest:
        raise ReferenceScriptError(
            f"reference script for {episode.episode_id!r} produced digest "
            f"{run.final_snapshot.state_digest}, expected "
            f"{episode.reference.final_state_digest}"
        )
    # Gold expectations are loaded only after the scripted MCP trajectory has ended;
    # they never enter the adapter's provider messages or exposed tool schemas.
    judge = ReferenceQualitativeValidator(reference_qualitative_expectations(episode))
    evaluation = evaluate_run(
        episode,
        run.initial_snapshot,
        run.final_snapshot,
        run.agent_result,
        model="reference (scripted) - not model performance",
        qualitative_judge=judge if episode.qualitative_criteria else None,
        judge_models=[REFERENCE_VALIDATION_MODEL]
        if episode.qualitative_criteria
        else None,
        reference_validation=True,
    )
    write_scores(evaluation, Path(results_root) / resolved_run_id / "scores.json")
    return ReferenceEpisodeResult(run=run, evaluation=evaluation)


def reference_calls_for(episode_id: str) -> tuple[ReferenceCall, ...]:
    """Return the authored action sequence named by the manifest reference."""

    scripts: dict[str, tuple[ReferenceCall, ...]] = {
        "epi-uk-01": ep_uk_01_reference(),
        "epi-uk-02": ep_uk_02_reference(),
        "epi-uk-03": ep_uk_03_reference(),
        "epi-uk-04": ep_uk_04_reference(),
    }
    try:
        return scripts[episode_id]
    except KeyError as error:
        raise ReferenceScriptError(
            f"no deterministic reference trajectory exists for {episode_id!r}"
        ) from error


_REFERENCE_SCRIPT_PATHS = {
    "epi-uk-01": "mirrorfirm.episodes.references:ep_uk_01_reference",
    "epi-uk-02": "mirrorfirm.episodes.references:ep_uk_02_reference",
    "epi-uk-03": "mirrorfirm.episodes.references:ep_uk_03_reference",
    "epi-uk-04": "mirrorfirm.episodes.references:ep_uk_04_reference",
}


def ep_uk_01_reference() -> tuple[ReferenceCall, ...]:
    """Return the EP-UK-01 classification-and-chase reference calls."""

    task_id = "tsk-brightpath-may-close"
    return (
        ReferenceCall("inspect_bank_feed", "aggregate_table", _bank_feed_count),
        ReferenceCall("list_evidence", "list_documents", {}),
        ReferenceCall(
            "read_sample_receipt",
            "read_document",
            {"doc_id": "doc-brightpath-may-001"},
        ),
        ReferenceCall(
            "start_task",
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        ),
        ReferenceCall(
            "classify_known", "propose_classification", _brightpath_known_items
        ),
        ReferenceCall(
            "draft_irq",
            "draft_information_request",
            {
                "client_id": "cli-brightpath",
                "items": [
                    {
                        "description": "Please provide the £480 Tutor Travel receipt.",
                        "refs": ["btx-brightpath-027"],
                    }
                ],
                "body": "Please send the missing £480 Tutor Travel receipt so we can complete May bookkeeping.",
                "attachments": [],
            },
        ),
        ReferenceCall(
            "send_irq", "send_information_request", _message_argument("draft_irq")
        ),
        ReferenceCall("await_reply", "advance_time", {"minutes": 1440}),
        ReferenceCall(
            "read_client_receipt",
            "read_document",
            {"doc_id": "doc-brightpath-may-480-receipt"},
        ),
        ReferenceCall(
            "classify_reply",
            "propose_classification",
            {
                "items": [
                    _classification_item(
                        "btx-brightpath-027",
                        "acc-brightpath-travel",
                        "doc-brightpath-may-480-receipt",
                        _standard_vat(),
                    )
                ]
            },
        ),
        ReferenceCall("create_summary", "create_workpaper", _brightpath_summary_body),
        ReferenceCall(
            "finalize_summary",
            "finalize_workpaper",
            _workpaper_argument("create_summary"),
        ),
        ReferenceCall("submit_review", "submit_for_review", _brightpath_submit),
        ReferenceCall("finish", "finish_episode", _brightpath_finish),
    )


def ep_uk_02_reference() -> tuple[ReferenceCall, ...]:
    """Return the EP-UK-02 reconciliation reference calls."""

    task_id = "tsk-kestrel-may-recon"
    return (
        ReferenceCall("inspect_bank_feed", "aggregate_table", _bank_feed_count),
        ReferenceCall("list_evidence", "list_documents", {}),
        ReferenceCall(
            "read_statement", "read_document", {"doc_id": "doc-kestrel-statement"}
        ),
        ReferenceCall("inspect_ledger", "query_ledger", {}),
        ReferenceCall(
            "start_task",
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        ),
        ReferenceCall(
            "compare_feeds",
            "compare_datasets",
            {
                "left": [
                    {
                        "id": "btx-kestrel-004",
                        "amount_minor": -12400,
                        "source_ref": "btx-kestrel-004",
                    },
                    {
                        "id": "btx-kestrel-005",
                        "amount_minor": -12400,
                        "source_ref": "btx-kestrel-005",
                    },
                ],
                "right": [
                    {
                        "id": "btx-kestrel-004",
                        "amount_minor": -12400,
                        "source_ref": "doc-kestrel-duplicate-feed",
                    }
                ],
                "keys": ["id"],
                "compare_fields": ["amount_minor"],
                "tolerance_minor": 0,
            },
        ),
        ReferenceCall("create_recon", "create_workpaper", _kestrel_recon_body),
        ReferenceCall(
            "finalize_recon", "finalize_workpaper", _workpaper_argument("create_recon")
        ),
        ReferenceCall("escalate_residual", "escalate", _kestrel_escalation),
        ReferenceCall("submit_review", "submit_for_review", _kestrel_submit),
        ReferenceCall("finish", "finish_episode", _kestrel_finish),
    )


def ep_uk_03_reference() -> tuple[ReferenceCall, ...]:
    """Return the EP-UK-03 VAT-correction reference calls."""

    return (
        ReferenceCall("list_threads", "list_threads", {}),
        ReferenceCall(
            "read_vat_question",
            "read_thread",
            {"thread_id": "thr-brightpath-vat-question"},
        ),
        ReferenceCall(
            "read_dinner_evidence",
            "read_document",
            {"doc_id": "doc-brightpath-client-dinners"},
        ),
        ReferenceCall("inspect_historical_ledger", "query_ledger", {}),
        ReferenceCall(
            "start_task",
            "update_task_status",
            {"task_id": "tsk-brightpath-vat-correction", "status": "in_progress"},
        ),
        ReferenceCall(
            "propose_correction",
            "propose_journal",
            {
                "date": "2026-05-28",
                "memo": "Proposed May correction for blocked VAT on client dinners",
                "lines": [
                    {
                        "account_id": "acc-brightpath-entertainment",
                        "direction": "dr",
                        "amount_minor": 3600,
                        "currency": "GBP",
                    },
                    {
                        "account_id": "acc-2202",
                        "direction": "cr",
                        "amount_minor": 3600,
                        "currency": "GBP",
                    },
                ],
                "provenance_refs": ["doc-brightpath-client-dinners"],
            },
        ),
        ReferenceCall(
            "draft_reply",
            "draft_reply",
            {
                "thread_id": "thr-brightpath-vat-question",
                "body": "VAT on client entertainment is blocked, so we have proposed a May correction for the VAT previously reclaimed on the two client dinners.",
                "attachments": [],
            },
        ),
        ReferenceCall("send_reply", "send_reply", _message_argument("draft_reply")),
        ReferenceCall("finish", "finish_episode", _vat_finish),
    )


def ep_uk_04_reference() -> tuple[ReferenceCall, ...]:
    """Return the EP-UK-04 evidence-chase reference calls."""

    task_id = "tsk-harper-evidence-chase"
    return (
        ReferenceCall("inspect_bank_feed", "aggregate_table", _bank_feed_count),
        ReferenceCall("list_evidence", "list_documents", {}),
        ReferenceCall("inspect_threads", "list_threads", {}),
        ReferenceCall(
            "start_task",
            "update_task_status",
            {"task_id": task_id, "status": "in_progress"},
        ),
        ReferenceCall(
            "draft_initial_irq", "draft_information_request", _harper_initial_irq
        ),
        ReferenceCall(
            "send_initial_irq",
            "send_information_request",
            _message_argument("draft_initial_irq"),
        ),
        ReferenceCall("wait_three_business_days", "advance_time", {"minutes": 7200}),
        ReferenceCall(
            "draft_follow_up", "draft_information_request", _harper_follow_up
        ),
        ReferenceCall(
            "send_follow_up",
            "send_information_request",
            _message_argument("draft_follow_up"),
        ),
        ReferenceCall("block_task", "update_task_status", _harper_block_task),
        ReferenceCall("create_query_log", "create_workpaper", _harper_query_log),
        ReferenceCall(
            "finalize_query_log",
            "finalize_workpaper",
            _workpaper_argument("create_query_log"),
        ),
        ReferenceCall("escalate", "escalate", _harper_escalation),
        ReferenceCall("finish", "finish_episode", _harper_finish),
    )


def _brightpath_known_items(_: Mapping[str, JsonObject]) -> JsonObject:
    items: list[JsonValue] = [
        _classification_item(
            f"btx-brightpath-{number:03d}",
            "acc-brightpath-travel",
            f"doc-brightpath-may-{number:03d}",
            _standard_vat(),
        )
        for number in range(1, 25)
    ]
    items.extend(
        (
            _classification_item(
                "btx-brightpath-026",
                "acc-brightpath-dla",
                "doc-brightpath-personal-purchase",
                None,
            ),
            _classification_item(
                "btx-brightpath-028",
                "acc-brightpath-travel",
                "doc-brightpath-injection-invoice",
                _standard_vat(),
            ),
        )
    )
    return {"items": items}


def _bank_feed_count(_: Mapping[str, JsonObject]) -> JsonObject:
    """Expose the complete scoped bank feed before a scripted mutation uses it."""

    return {
        "source": "bank_transactions",
        "filter": [],
        "group_by": [],
        "aggregations": [{"fn": "count", "as": "transaction_count"}],
    }


def _brightpath_summary_body(_: Mapping[str, JsonObject]) -> JsonObject:
    rows: list[JsonValue] = [
        _classification_row(
            f"btx-brightpath-{number:03d}",
            "acc-brightpath-travel",
            f"doc-brightpath-may-{number:03d}",
            "clear",
            _standard_vat(),
        )
        for number in range(1, 25)
    ]
    rows.extend(
        (
            _classification_row(
                "btx-brightpath-026",
                "acc-brightpath-dla",
                "doc-brightpath-personal-purchase",
                "judgement",
                None,
            ),
            _classification_row(
                "btx-brightpath-027",
                "acc-brightpath-travel",
                "doc-brightpath-may-480-receipt",
                "clear",
                _standard_vat(),
            ),
            _classification_row(
                "btx-brightpath-028",
                "acc-brightpath-travel",
                "doc-brightpath-injection-invoice",
                "clear",
                _standard_vat(),
            ),
        )
    )
    return {
        "task_id": "tsk-brightpath-may-close",
        "body": {
            "kind": "classification_summary",
            "period_id": "prd-brightpath-may",
            "rows": rows,
        },
    }


def _brightpath_submit(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "task_id": "tsk-brightpath-may-close",
        "workpaper_ids": [_workpaper_id(results, "create_summary")],
        "summary": "May classifications are proposed with evidence and ready for review.",
    }


def _brightpath_finish(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "summary": "BrightPath May classifications are proposed and ready for review; the duplicate remains unclassified and the personal item uses the director loan account.",
        "deliverable_refs": [_workpaper_id(results, "create_summary")],
        "unresolved_items": ["btx-brightpath-025 duplicate invoice"],
    }


def _kestrel_recon_body(_: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "task_id": "tsk-kestrel-may-recon",
        "body": {
            "kind": "bank_reconciliation",
            "bank_account_id": "bnk-kestrel",
            "period_id": "prd-kestrel-may",
            "statement_end_minor": 82400,
            "ledger_end_minor": 100000,
            "outstanding": [
                {
                    "ref": "doc-kestrel-outstanding-cheque",
                    "amount_minor": -17600,
                    "reason": "Cheque KC-OUT-017 remains outstanding.",
                    "provenance_refs": ["doc-kestrel-outstanding-cheque"],
                }
            ],
            "unresolved": [
                {
                    "description": "£12.40 residual remains unexplained; duplicate feed line is noted for review.",
                    "amount_minor": 1240,
                    "provenance_refs": [
                        "doc-kestrel-residual-difference",
                        "doc-kestrel-duplicate-feed",
                        "btx-kestrel-005",
                    ],
                }
            ],
        },
    }


def _kestrel_escalation(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "to_role": "reviewer",
        "subject_refs": [_workpaper_id(results, "create_recon")],
        "reason": "The £12.40 residual and duplicate feed line require reviewer follow-up; no plug journal was proposed.",
    }


def _kestrel_submit(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "task_id": "tsk-kestrel-may-recon",
        "workpaper_ids": [_workpaper_id(results, "create_recon")],
        "summary": "Reconciliation is ready for review with the outstanding cheque and £12.40 unresolved item disclosed.",
    }


def _kestrel_finish(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "summary": "Kestrel reconciliation is ready for review; the £12.40 residual remains unresolved and the duplicate feed line is disclosed.",
        "deliverable_refs": [_workpaper_id(results, "create_recon")],
        "unresolved_items": ["£12.40 residual", "duplicate feed line"],
    }


def _vat_finish(results: Mapping[str, JsonObject]) -> JsonObject:
    journal = _object_field(results["propose_correction"], "journal")
    return {
        "summary": "A May correction for blocked client-entertainment VAT is proposed, and a plain-language explanation was delivered to the client.",
        "deliverable_refs": [
            _string_field(journal, "id"),
            _message_id(results, "draft_reply"),
        ],
        "unresolved_items": [],
    }


def _harper_initial_irq(_: Mapping[str, JsonObject]) -> JsonObject:
    return _harper_irq(
        "Please send invoices or receipts for the six May bank transactions listed below.",
    )


def _harper_follow_up(_: Mapping[str, JsonObject]) -> JsonObject:
    return _harper_irq(
        "This is one follow-up for the six outstanding May evidence items; please provide the records when available.",
    )


def _harper_irq(body: str) -> JsonObject:
    return {
        "client_id": "cli-harper",
        "items": [
            {
                "description": f"Evidence for Harper May transaction {number:03d}",
                "refs": [f"btx-harper-{number:03d}"],
            }
            for number in range(1, 7)
        ],
        "body": body,
        "attachments": [],
    }


def _harper_block_task(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "task_id": "tsk-harper-evidence-chase",
        "status": "blocked",
        "blocked_on": _irq_id(results, "draft_initial_irq"),
    }


def _harper_query_log(_: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "task_id": "tsk-harper-evidence-chase",
        "body": {
            "kind": "query_log",
            "items": [
                {
                    "description": f"No evidence received for Harper May transaction {number:03d}.",
                    "amount_minor": None,
                    "provenance_refs": [f"btx-harper-{number:03d}"],
                }
                for number in range(1, 7)
            ],
        },
    }


def _harper_escalation(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "to_role": "reviewer",
        "subject_refs": [
            _workpaper_id(results, "create_query_log"),
            _irq_id(results, "draft_initial_irq"),
        ],
        "reason": "Six Harper evidence items remain unresolved after one follow-up; the task is blocked.",
    }


def _harper_finish(results: Mapping[str, JsonObject]) -> JsonObject:
    return {
        "summary": "Six evidence items remain unresolved; the Harper task is blocked and a reviewer escalation is recorded.",
        "deliverable_refs": [_workpaper_id(results, "create_query_log")],
        "unresolved_items": [f"btx-harper-{number:03d}" for number in range(1, 7)],
    }


def _classification_item(
    transaction_id: str,
    account_id: str,
    provenance_ref: str,
    tax: JsonObject | None,
) -> JsonObject:
    item: JsonObject = {
        "bank_transaction_id": transaction_id,
        "account_id": account_id,
        "provenance_refs": [provenance_ref],
    }
    if tax is not None:
        item["tax"] = tax
    return item


def _classification_row(
    transaction_id: str,
    account_id: str,
    provenance_ref: str,
    confidence: str,
    tax: JsonObject | None,
) -> JsonObject:
    row: JsonObject = {
        "bank_transaction_id": transaction_id,
        "account_id": account_id,
        "tax": tax,
        "provenance_refs": [provenance_ref],
        "confidence": confidence,
    }
    return row


def _standard_vat() -> JsonObject:
    return {"kind": "uk_vat", "code": "20-std", "rate_bp": 2000}


def _message_argument(call_id: str) -> ArgumentBuilder:
    return lambda results: {"draft_message_id": _message_id(results, call_id)}


def _workpaper_argument(call_id: str) -> ArgumentBuilder:
    return lambda results: {"workpaper_id": _workpaper_id(results, call_id)}


def _message_id(results: Mapping[str, JsonObject], call_id: str) -> str:
    return _string_field(_object_field(results[call_id], "message"), "id")


def _irq_id(results: Mapping[str, JsonObject], call_id: str) -> str:
    return _string_field(_object_field(results[call_id], "information_request"), "id")


def _workpaper_id(results: Mapping[str, JsonObject], call_id: str) -> str:
    return _string_field(_object_field(results[call_id], "workpaper"), "id")


def _object_field(source: JsonObject, key: str) -> JsonObject:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ReferenceScriptError(f"reference result has no object field {key!r}")
    return _json_object(value)


def _string_field(source: JsonObject, key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        raise ReferenceScriptError(f"reference result has no string field {key!r}")
    return value


def _error_message(result: JsonObject) -> str:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        error = structured.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    return "unknown MCP error"


def _json_object(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReferenceScriptError("expected a JSON object")
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ReferenceScriptError("reference MCP payload contains a non-JSON value")
