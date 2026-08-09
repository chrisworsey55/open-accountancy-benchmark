# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Mistral chat-completions adapter."""

import os

from mistralai.client import Mistral

from mirrorfirm.harness.adapters.base import ModelAdapter, ModelResponse, ToolCall

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

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
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
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "tools": mistral_tools,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort and self.model in REASONING_MODELS:
            kwargs["reasoning_effort"] = self.reasoning_effort

        response = self.client.chat.complete(**kwargs)
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
        message_dict: dict = {"role": "assistant", "content": content}
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
    def make_tool_result_messages(results: list[tuple[str, str]]) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": tool_call_id, "content": result}
            for tool_call_id, result in results
        ]

    @staticmethod
    def make_system_message(content: str) -> dict:
        return {"role": "system", "content": content}

    @staticmethod
    def make_user_message(content: str) -> dict:
        return {"role": "user", "content": content}

    @staticmethod
    def _serialize_content(content: str | list) -> tuple[str | list[dict], str]:
        if isinstance(content, str):
            return content, content
        if not content:
            return "", ""

        serialized = []
        text_parts = []
        for chunk in content:
            if chunk.type == "thinking":
                thinking = [
                    {"type": "text", "text": text_chunk.text}
                    for text_chunk in chunk.thinking
                    if hasattr(text_chunk, "text")
                ]
                entry: dict = {"type": "thinking", "thinking": thinking}
                if (
                    hasattr(chunk, "signature")
                    and chunk.signature
                    and str(chunk.signature) != "Unset"
                ):
                    entry["signature"] = chunk.signature
                serialized.append(entry)
            elif chunk.type == "text":
                serialized.append({"type": "text", "text": chunk.text})
                text_parts.append(chunk.text)
            else:
                serialized.append({"type": chunk.type})
        return serialized, "\n".join(text_parts)
