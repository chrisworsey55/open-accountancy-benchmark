# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Google Generative AI API adapter."""

import json

from google import genai
from google.genai import types

from mirrorfirm.harness.adapters.base import ModelAdapter, ModelResponse, ToolCall

THINKING_LEVEL_MAP = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}


class GoogleAdapter(ModelAdapter):
    """Adapter for Google Gemini models."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 65536,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens
        self.client = genai.Client()
        self._chat = None
        self._system_instruction = None
        self._tools = None

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        if self._chat is None:
            self._tools = self._translate_tools(tools)
            for message in messages:
                if message["role"] == "system":
                    self._system_instruction = message["content"]

            config = types.GenerateContentConfig(
                temperature=self.temperature,
                max_output_tokens=self.max_tokens,
                tools=self._tools,
                system_instruction=self._system_instruction,
                tool_config=types.ToolConfig(
                    include_server_side_tool_invocations=True,
                ),
            )
            if self.reasoning_effort in THINKING_LEVEL_MAP:
                thinking_dict = {
                    "thinking_level": THINKING_LEVEL_MAP[self.reasoning_effort],
                    "include_thoughts": True,
                }
                config._raw_data = getattr(config, "_raw_data", {})
                if isinstance(config._raw_data, dict):
                    config._raw_data["thinking_config"] = thinking_dict
                else:
                    try:
                        config.thinking_config = types.ThinkingConfig(
                            thinking_level=THINKING_LEVEL_MAP[self.reasoning_effort],
                            include_thoughts=True,
                        )
                    except Exception:
                        pass

            self._chat = self.client.chats.create(model=self.model, config=config)
            user_message = next(
                (
                    message.get("content", "")
                    for message in messages
                    if message["role"] == "user"
                ),
                "Begin.",
            )
            response = self._chat.send_message(user_message or "Begin.")
        else:
            last_message = messages[-1]
            if last_message.get("role") == "user" and "parts" in last_message:
                parts = []
                for part_dict in last_message["parts"]:
                    if "function_response" in part_dict:
                        function_response = part_dict["function_response"]
                        parts.append(
                            types.Part.from_function_response(
                                name=function_response["name"],
                                response=function_response["response"],
                            )
                        )
                    elif "text" in part_dict:
                        parts.append(types.Part.from_text(text=part_dict["text"]))
                response = self._chat.send_message(parts)
            else:
                response = self._chat.send_message(
                    last_message.get("content", "Continue.")
                )

        tool_calls = []
        text_parts = []
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    function_call = part.function_call
                    tool_calls.append(
                        ToolCall(
                            id=function_call.name,
                            name=function_call.name,
                            arguments=json.dumps(dict(function_call.args))
                            if function_call.args
                            else "{}",
                        )
                    )
                elif part.text and not getattr(part, "thought", False):
                    text_parts.append(part.text)

        message = {"role": "model", "parts": []}
        for tool_call in tool_calls:
            message["parts"].append(
                {
                    "function_call": {
                        "name": tool_call.name,
                        "args": json.loads(tool_call.arguments),
                    }
                }
            )
        if text_parts:
            message["parts"].append({"text": "\n".join(text_parts)})

        usage = response.usage_metadata if response.usage_metadata else None
        return ModelResponse(
            message=message,
            tool_calls=tool_calls,
            text="\n".join(text_parts),
            input_tokens=usage.prompt_token_count if usage else 0,
            output_tokens=usage.candidates_token_count if usage else 0,
        )

    @staticmethod
    def make_tool_result_messages(results: list[tuple[str, str]]) -> list[dict]:
        return [
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": tool_call_id,
                            "response": {"result": result},
                        }
                    }
                    for tool_call_id, result in results
                ],
            }
        ]

    @staticmethod
    def make_system_message(content: str) -> dict:
        return {"role": "system", "content": content}

    @staticmethod
    def make_user_message(content: str) -> dict:
        return {"role": "user", "parts": [{"text": content}]}

    @staticmethod
    def _translate_tools(tools: list[dict]) -> list:
        declarations = [
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters=tool["parameters"],
            )
            for tool in tools
        ]
        return [types.Tool(function_declarations=declarations)]
