# SPDX-License-Identifier: MIT
# Derived from harveyai/harvey-labs (MIT), commit
# 55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c.
# Adapted for Mirror Firm WP-01: package imports are self-contained.
"""Canonical model-provider adapter interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeAlias

# Provider SDKs expose heterogeneous, vendor-defined JSON wire objects.  This is the
# deliberately narrow third-party boundary; the rest of the harness uses ToolCall.
ProviderPayload: TypeAlias = dict[str, Any]


@dataclass
class ToolCall:
    """A single tool call from the model."""

    id: str
    name: str
    arguments: str


@dataclass
class ModelResponse:
    """Normalized response from any model provider."""

    message: ProviderPayload
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class ModelAdapter(ABC):
    """Abstract interface implemented by each model provider adapter."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort

    @abstractmethod
    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        """Send messages and tool definitions and return a normalized response."""

    @abstractmethod
    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        """Create provider-native message(s) containing tool-call results."""

    @abstractmethod
    def make_system_message(self, content: str) -> ProviderPayload:
        """Create a provider-native system message."""

    @abstractmethod
    def make_user_message(self, content: str) -> ProviderPayload:
        """Create a provider-native user message."""
