# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: unsafe shell/file tools were removed and the executor
# is represented by a local protocol until WP-07 introduces the tool registry and WP-08
# adds MCP routing, clock/event injection, budgets, and explicit finish_episode handling.
"""Minimal provider-agnostic agent loop derived from Harvey LAB."""

import json
import time
from pathlib import Path
from typing import Protocol, TextIO

from mirrorfirm.harness.adapters.base import ModelAdapter, ModelResponse


class ToolExecutor(Protocol):
    """Minimal executor contract for the future typed-world tool layer."""

    def execute(self, name: str, arguments: str) -> str:
        """Execute one tool call and return a serialized result."""

    def get_metrics(self) -> dict[str, object]:
        """Return executor metrics for reporting."""


def run_agent(
    adapter: ModelAdapter,
    system_prompt: str,
    user_prompt: str,
    tool_executor: ToolExecutor,
    tools: list[dict],
    max_turns: int = 200,
    transcript_path: str | None = None,
) -> dict[str, object]:
    """Run the generic adapter loop until the model stops or reaches ``max_turns``.

    WP-01 intentionally leaves world lifecycle semantics to WP-08. In particular, this
    scaffold does not expose a shell, a filesystem, or a default tool set.
    """

    messages = [
        adapter.make_system_message(system_prompt),
        adapter.make_user_message(user_prompt),
    ]
    total_input_tokens = 0
    total_output_tokens = 0
    turn_count = 0
    start_time = time.time()
    last_response: ModelResponse | None = None
    context_overflow = False

    transcript_file: TextIO | None = None
    if transcript_path:
        path = Path(transcript_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        transcript_file = path.open("w", encoding="utf-8")

    try:
        for turn in range(max_turns):
            turn_count = turn + 1
            try:
                response = adapter.chat(messages, tools)
            except Exception as error:
                error_message = str(error)
                if (
                    "prompt is too long" in error_message
                    or "context_length_exceeded" in error_message
                ):
                    context_overflow = True
                    break
                raise

            last_response = response
            messages.append(response.message)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            if transcript_file:
                _log_turn(transcript_file, turn_count, response)

            if not response.tool_calls:
                break

            tool_results: list[tuple[str, str]] = []
            for tool_call in response.tool_calls:
                result = tool_executor.execute(tool_call.name, tool_call.arguments)
                if transcript_file:
                    _log_tool(
                        transcript_file,
                        turn_count,
                        tool_call.name,
                        tool_call.arguments,
                        result,
                    )
                tool_results.append((tool_call.id, result))
            messages.extend(adapter.make_tool_result_messages(tool_results))
    finally:
        if transcript_file:
            transcript_file.close()

    return {
        "messages": messages,
        "turn_count": turn_count,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "wall_clock_seconds": round(time.time() - start_time, 2),
        "finished_cleanly": (
            not context_overflow
            and last_response is not None
            and not last_response.tool_calls
        ),
        "context_overflow": context_overflow,
        "tool_metrics": tool_executor.get_metrics(),
        "finish_summary": None,
    }


def _log_turn(file: TextIO, turn: int, response: ModelResponse) -> None:
    """Append a model turn to a transcript JSONL file."""

    file.write(
        json.dumps(
            {
                "turn": turn,
                "role": "assistant",
                "text": response.text[:500] if response.text else None,
                "tool_calls": [
                    {"name": tool_call.name, "arguments": tool_call.arguments}
                    for tool_call in response.tool_calls
                ]
                or None,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        )
        + "\n"
    )
    file.flush()


def _log_tool(
    file: TextIO,
    turn: int,
    name: str,
    arguments: str,
    result: str,
) -> None:
    """Append a tool result to a transcript JSONL file."""

    file.write(
        json.dumps(
            {
                "turn": turn,
                "role": "tool",
                "tool_name": name,
                "arguments": arguments,
                "result_preview": result[:1000],
            }
        )
        + "\n"
    )
    file.flush()
