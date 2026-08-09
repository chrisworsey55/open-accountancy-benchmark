# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""OpenAI Responses API adapter."""

from typing import Any, cast

import openai

from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)


class OpenAIAdapter(ModelAdapter):
    """Adapter for OpenAI models using the Responses API."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 128000,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens
        self.client = openai.OpenAI()
        self._context: list[ProviderPayload] = []
        self._system_instructions: str | None = None

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        if not self._context:
            for message in messages:
                if message["role"] == "system":
                    self._system_instructions = message["content"]
                elif message["role"] == "user":
                    self._context.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": message["content"],
                        }
                    )

        kwargs = {
            "model": self.model,
            "instructions": self._system_instructions or "",
            "input": self._context,
            "tools": [self._translate_tool(tool) for tool in tools],
            "max_output_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {
                "effort": self.reasoning_effort,
                "summary": "auto",
            }
        else:
            kwargs["temperature"] = self.temperature

        # The Responses SDK overload cannot describe the normalized provider payload.
        response = cast(Any, self.client.responses).create(**kwargs)
        tool_calls = []
        text_parts = []
        output_items = []
        for item in response.output:
            output_items.append(item)
            if item.type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=item.call_id,
                        name=item.name,
                        arguments=item.arguments,
                    )
                )
            elif item.type == "message":
                for content in item.content:
                    if hasattr(content, "text"):
                        text_parts.append(content.text)

        self._context.extend(output_items)
        return ModelResponse(
            message={
                "role": "assistant",
                "output": [self._item_to_dict(item) for item in output_items],
            },
            tool_calls=tool_calls,
            text="\n".join(text_parts),
            input_tokens=response.usage.input_tokens if response.usage else 0,
            output_tokens=response.usage.output_tokens if response.usage else 0,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        items = []
        for tool_call_id, result in results:
            item = {
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": result,
            }
            self._context.append(item)
            items.append(item)
        return items

    def make_system_message(self, content: str) -> ProviderPayload:
        self._system_instructions = content
        return {"role": "system", "content": content}

    @staticmethod
    def make_user_message(content: str) -> ProviderPayload:
        return {"role": "user", "content": content}

    @staticmethod
    def _translate_tool(tool: ProviderPayload) -> ProviderPayload:
        return {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        }

    @staticmethod
    def _item_to_dict(item: object) -> ProviderPayload:
        # Response output variants are SDK-owned; contain their untyped edge here.
        provider_item = cast(Any, item)
        if provider_item.type == "function_call":
            return {
                "type": "function_call",
                "call_id": provider_item.call_id,
                "name": provider_item.name,
                "arguments": provider_item.arguments,
            }
        if provider_item.type == "message":
            return {
                "type": "message",
                "role": getattr(provider_item, "role", "assistant"),
                "content": [
                    {"type": "text", "text": content.text}
                    for content in provider_item.content
                    if hasattr(content, "text")
                ],
            }
        if hasattr(provider_item, "model_dump"):
            return cast(ProviderPayload, provider_item.model_dump())
        return {"type": provider_item.type}
