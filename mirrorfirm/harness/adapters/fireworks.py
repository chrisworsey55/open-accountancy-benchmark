# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Fireworks OpenAI-compatible chat-completions adapter."""

import os
import time
from typing import Any, cast

import openai

from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)

_MAX_RETRIES = 8


class FireworksAdapter(ModelAdapter):
    """Adapter for Fireworks chat-completions models."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 128000,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens
        if not self.model.startswith("accounts/"):
            self.model = f"accounts/fireworks/models/{self.model}"
        self.client = openai.OpenAI(
            api_key=os.environ["FIREWORKS_API_KEY"],
            base_url=os.environ.get(
                "FIREWORKS_API_BASE", "https://api.fireworks.ai/inference/v1"
            ),
        )

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        response = None
        last_error: Exception | None = None
        kwargs: dict[str, object] = {}
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        else:
            kwargs["temperature"] = self.temperature

        for attempt in range(_MAX_RETRIES):
            try:
                # Fireworks accepts OpenAI-shaped payloads beyond the SDK overload.
                response = cast(Any, self.client.chat.completions).create(
                    model=self.model,
                    messages=messages,
                    tools=[self._translate_tool(tool) for tool in tools],
                    max_tokens=self.max_tokens,
                    **kwargs,
                )
                break
            except (
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.InternalServerError,
            ) as error:
                last_error = error
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(min(60, 15 * (attempt + 1)))

        if response is None:
            if last_error is None:
                raise RuntimeError("Fireworks request failed without an exception")
            raise last_error

        message_obj = response.choices[0].message
        tool_calls = [
            ToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=tool_call.function.arguments or "{}",
            )
            for tool_call in message_obj.tool_calls or []
        ]
        usage = response.usage
        return ModelResponse(
            message=cast(ProviderPayload, message_obj.model_dump(exclude_none=True)),
            tool_calls=tool_calls,
            text=message_obj.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    @staticmethod
    def make_tool_result_messages(
        results: list[tuple[str, str]],
    ) -> list[ProviderPayload]:
        return [
            {"role": "tool", "tool_call_id": tool_call_id, "content": result}
            for tool_call_id, result in results
        ]

    @staticmethod
    def make_system_message(content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    @staticmethod
    def make_user_message(content: str) -> ProviderPayload:
        return {"role": "user", "content": content}

    @staticmethod
    def _translate_tool(tool: ProviderPayload) -> ProviderPayload:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
