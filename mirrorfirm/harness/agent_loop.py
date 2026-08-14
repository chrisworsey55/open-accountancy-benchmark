# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-08: unsafe shell/file tools remain removed; the stateful
# executor routes only declared tools through local MCP and makes finish explicit.
"""Provider-agnostic bounded loop for a stateful Mirror Firm episode."""

import json
import time
from pathlib import Path
from typing import Protocol, TextIO

from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
)


class ToolExecutor(Protocol):
    """Stateful MCP executor contract used by the provider-agnostic loop."""

    def execute(self, name: str, arguments: str) -> str:
        """Execute one tool call and return a serialized result."""

    def reserve_tool_calls(self, count: int) -> bool:
        """Atomically charge an entire provider response before executing any call."""

    def get_metrics(self) -> dict[str, object]:
        """Return executor metrics for reporting."""

    @property
    def is_finished(self) -> bool:
        """Whether a successful ``finish_episode`` call made the world terminal."""

    @property
    def budget_exhausted(self) -> bool:
        """Whether a tool-level episode budget has been exhausted."""

    @property
    def finish_summary(self) -> dict[str, object] | None:
        """Return the successful terminal tool output, if one exists."""


def run_agent(
    adapter: ModelAdapter,
    system_prompt: str,
    user_prompt: str,
    tool_executor: ToolExecutor,
    tools: list[ProviderPayload],
    max_turns: int = 200,
    transcript_path: str | None = None,
    max_tokens: int | None = None,
    require_finish_episode: bool = False,
) -> dict[str, object]:
    """Run a bounded stateful episode until it stops, finishes, or exhausts budget.

    The loop accepts only provider-normalized tool calls.  The executor performs the
    actual MCP call and world-time enforcement; token accounting is checked before
    any response's tool calls are permitted to mutate the world.
    """

    if max_turns <= 0:
        raise ValueError("max_turns must be positive")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive when specified")

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
    token_budget_exhausted = False
    batch_budget_exhausted = False

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

            if (
                max_tokens is not None
                and total_input_tokens + total_output_tokens > max_tokens
            ):
                token_budget_exhausted = True
                break

            if not response.tool_calls:
                break

            if not tool_executor.reserve_tool_calls(len(response.tool_calls)):
                batch_budget_exhausted = True
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
                if tool_executor.budget_exhausted or tool_executor.is_finished:
                    break
            messages.extend(adapter.make_tool_result_messages(tool_results))
            if tool_executor.budget_exhausted or tool_executor.is_finished:
                break
    finally:
        if transcript_file:
            transcript_file.close()

    metrics = tool_executor.get_metrics()
    episode_finished = tool_executor.is_finished
    step_budget_exhausted = metrics.get("step_budget_exhausted") is True
    world_time_budget_exhausted = metrics.get("world_time_budget_exhausted") is True
    finished_cleanly = (
        not context_overflow
        and not token_budget_exhausted
        and not tool_executor.budget_exhausted
        and last_response is not None
        and (
            episode_finished if require_finish_episode else not last_response.tool_calls
        )
    )
    return {
        "messages": messages,
        "turn_count": turn_count,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "wall_clock_seconds": round(time.time() - start_time, 2),
        "finished_cleanly": finished_cleanly,
        "context_overflow": context_overflow,
        "token_budget_exhausted": token_budget_exhausted,
        "batch_budget_exhausted": batch_budget_exhausted,
        "step_budget_exhausted": step_budget_exhausted,
        "world_time_budget_exhausted": world_time_budget_exhausted,
        "episode_finished": episode_finished,
        "tool_metrics": metrics,
        "finish_summary": tool_executor.finish_summary,
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
