# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Anthropic Messages API adapter."""

import json
from typing import Any, cast

import anthropic

from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)

ADAPTIVE_MODELS = (
    "claude-fable-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
)

NO_TEMPERATURE_MODELS = (
    "claude-fable-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-7",
    "claude-sonnet-5",
)


class AnthropicAdapter(ModelAdapter):
    """Adapter for Anthropic Claude models."""

    MAX_OUTPUT = {
        "claude-fable-5": 128000,
        "claude-opus-4-8": 128000,
        "claude-opus-4-7": 128000,
        "claude-opus-4-6": 128000,
        "claude-sonnet-5": 128000,
        "claude-sonnet-4-6": 64000,
        "claude-haiku-4-5": 64000,
    }

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(model, temperature, reasoning_effort)
        if max_tokens is None:
            max_tokens = next(
                (
                    value
                    for name, value in self.MAX_OUTPUT.items()
                    if model.startswith(name)
                ),
                16384,
            )
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic()
        self._system_prompt: str | None = None

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        api_messages = []
        for message in messages:
            if message["role"] == "system":
                self._system_prompt = message["content"]
            else:
                api_messages.append(message)

        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._system_prompt or "",
            "messages": api_messages,
            "tools": [self._translate_tool(tool) for tool in tools],
        }
        if not self.model.startswith(NO_TEMPERATURE_MODELS):
            kwargs["temperature"] = self.temperature
        if self.reasoning_effort and self.model.startswith(ADAPTIVE_MODELS):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["extra_body"] = {"output_config": {"effort": self.reasoning_effort}}
            if "temperature" in kwargs:
                kwargs["temperature"] = 1

        # The SDK's generated stream overload cannot express this provider-normalized
        # request; keep the cast at the vendor call boundary only.
        with cast(Any, self.client.messages).stream(**kwargs) as stream:
            response = stream.get_final_message()

        tool_calls = []
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=json.dumps(block.input),
                    )
                )
            elif block.type == "text":
                text_parts.append(block.text)

        return ModelResponse(
            message={
                "role": "assistant",
                "content": [self._block_to_dict(block) for block in response.content],
            },
            tool_calls=tool_calls,
            text="\n".join(text_parts),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": result,
                    }
                    for tool_call_id, result in results
                ],
            }
        ]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}

    @staticmethod
    def _translate_tool(tool: ProviderPayload) -> ProviderPayload:
        return {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["parameters"],
        }

    @staticmethod
    def _block_to_dict(block: object) -> ProviderPayload:
        # Anthropic block variants are SDK-owned and intentionally narrowed here.
        provider_block = cast(Any, block)
        if provider_block.type == "text":
            return {"type": "text", "text": provider_block.text}
        if provider_block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": provider_block.id,
                "name": provider_block.name,
                "input": provider_block.input,
            }
        if provider_block.type == "thinking":
            result: ProviderPayload = {
                "type": "thinking",
                "thinking": provider_block.thinking,
            }
            if hasattr(provider_block, "signature") and provider_block.signature:
                result["signature"] = provider_block.signature
            return result
        if hasattr(provider_block, "model_dump"):
            return cast(ProviderPayload, provider_block.model_dump())
        return {"type": provider_block.type}
