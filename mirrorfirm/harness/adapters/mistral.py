# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Mistral chat-completions adapter."""

import os
from typing import Any, cast

from mistralai.client import Mistral

from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)

REASONING_MODELS = {"mistral-medium-3.5", "mistral-small-2603"}


class MistralAdapter(ModelAdapter):
    """Adapter for Mistral models."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens
        self.client = Mistral(
            api_key=os.environ["MISTRAL_API_KEY"],
            timeout_ms=600_000,
        )

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        mistral_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "tools": mistral_tools,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort and self.model in REASONING_MODELS:
            kwargs["reasoning_effort"] = self.reasoning_effort

        # Mistral's generated union types vary by SDK version; isolate the raw wire
        # contract at the provider invocation rather than weakening package typing.
        response = cast(Any, self.client.chat).complete(**kwargs)
        message = response.choices[0].message
        tool_calls = [
            ToolCall(
                id=tool_call.id,
                name=tool_call.function.name,
                arguments=tool_call.function.arguments,
            )
            for tool_call in message.tool_calls or []
        ]
        content, text = self._serialize_content(message.content)
        message_dict: ProviderPayload = {"role": "assistant", "content": content}
        if message.tool_calls:
            message_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in message.tool_calls
            ]

        return ModelResponse(
            message=message_dict,
            tool_calls=tool_calls,
            text=text,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
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
    def _serialize_content(
        content: str | list[object] | None,
    ) -> tuple[str | list[ProviderPayload], str]:
        if isinstance(content, str):
            return content, content
        if not content:
            return "", ""

        serialized: list[ProviderPayload] = []
        text_parts: list[str] = []
        for chunk in content:
            # Content chunks are provider-defined polymorphic SDK objects.
            provider_chunk = cast(Any, chunk)
            if provider_chunk.type == "thinking":
                thinking = [
                    {"type": "text", "text": text_chunk.text}
                    for text_chunk in provider_chunk.thinking
                    if hasattr(text_chunk, "text")
                ]
                entry: ProviderPayload = {"type": "thinking", "thinking": thinking}
                if (
                    hasattr(provider_chunk, "signature")
                    and provider_chunk.signature
                    and str(provider_chunk.signature) != "Unset"
                ):
                    entry["signature"] = provider_chunk.signature
                serialized.append(entry)
            elif provider_chunk.type == "text":
                serialized.append({"type": "text", "text": provider_chunk.text})
                text_parts.append(provider_chunk.text)
            else:
                serialized.append({"type": provider_chunk.type})
        return serialized, "\n".join(text_parts)
