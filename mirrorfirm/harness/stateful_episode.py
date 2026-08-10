"""MCP-routed execution controls for one bounded, stateful episode."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from mirrorfirm.harness.adapters.base import ProviderPayload
from mirrorfirm.tools import MCPToolServer, WorldToolEngine

_BUDGET_ERROR_CODE: Final = "EPISODE_BUDGET_EXCEEDED"


class StatefulEpisodeAdapter:
    """Expose an episode-approved subset of the stable 37-tool MCP server.

    The adapter is deliberately a harness boundary: it neither changes the registry nor
    calls the world engine directly.  Every permitted request is routed through the
    existing MCP adapter, while episode-specific tool, step, and world-time budgets are
    enforced before the request reaches the world.
    """

    def __init__(
        self,
        engine: WorldToolEngine,
        *,
        allowed_tools: set[str] | frozenset[str],
        max_steps: int,
        max_world_days: int,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("episode max_steps must be positive")
        if max_world_days <= 0:
            raise ValueError("episode max_world_days must be positive")

        self.engine = engine
        self._server = MCPToolServer(engine)
        self._registered_tool_names = frozenset(
            definition.name for definition in engine.registry.definitions()
        )
        self._allowed_tools = frozenset(allowed_tools)
        unknown_tools = self._allowed_tools - self._registered_tool_names
        if unknown_tools:
            raise ValueError(
                f"episode declares unknown tools: {', '.join(sorted(unknown_tools))}"
            )
        self._max_steps = max_steps
        self._world_started_at = engine.now
        self._world_deadline = self._world_started_at + timedelta(days=max_world_days)
        self._step_count = 0
        self._tool_errors = 0
        self._step_budget_exhausted = False
        self._world_time_budget_exhausted = False
        self._finish_summary: dict[str, object] | None = None

    @property
    def registered_tool_names(self) -> frozenset[str]:
        """Return the immutable stable registry names behind this episode."""

        return self._registered_tool_names

    @property
    def provider_tools(self) -> list[ProviderPayload]:
        """Translate approved MCP input schemas to the provider adapter shape."""

        listed = self._server.list_tools().get("tools")
        if not isinstance(listed, list):
            raise RuntimeError("MCP tools/list response is malformed")
        translated: list[ProviderPayload] = []
        for tool in listed:
            if not isinstance(tool, dict):
                raise RuntimeError("MCP tools/list contains a malformed tool")
            name = tool.get("name")
            description = tool.get("description")
            input_schema = tool.get("inputSchema")
            if not isinstance(name, str) or name not in self._allowed_tools:
                continue
            if not isinstance(description, str) or not isinstance(input_schema, dict):
                raise RuntimeError("MCP tool schema is malformed")
            translated.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": cast(dict[str, object], input_schema),
                }
            )
        return translated

    @property
    def is_finished(self) -> bool:
        """Whether the underlying engine committed a successful finish Action."""

        return self.engine.finished

    @property
    def budget_exhausted(self) -> bool:
        """Whether execution hit an episode step or world-time limit."""

        return self._step_budget_exhausted or self._world_time_budget_exhausted

    @property
    def finish_summary(self) -> dict[str, object] | None:
        """Return the validated final tool output once ``finish_episode`` succeeds."""

        return self._finish_summary

    def execute(self, name: str, arguments: str) -> str:
        """Route one model tool request through MCP and return serialized MCP output."""

        if self.budget_exhausted:
            return self._error_result(
                _BUDGET_ERROR_CODE, "episode budget has already been exhausted"
            )
        if self._step_count >= self._max_steps:
            self._step_budget_exhausted = True
            return self._error_result(
                _BUDGET_ERROR_CODE, "episode step budget has been exhausted"
            )
        if name not in self._allowed_tools:
            self._tool_errors += 1
            return self._error_result(
                "PERMISSION_DENIED", f"{name!r} is not allowed in this episode"
            )
        try:
            parsed_arguments: object = json.loads(arguments)
        except json.JSONDecodeError:
            self._tool_errors += 1
            return self._error_result(
                "VALIDATION_ERROR", "tool arguments must be a JSON object"
            )
        if not isinstance(parsed_arguments, dict):
            self._tool_errors += 1
            return self._error_result(
                "VALIDATION_ERROR", "tool arguments must be a JSON object"
            )
        typed_arguments = cast(dict[str, object], parsed_arguments)
        if self._would_exceed_world_time(name, typed_arguments):
            self._world_time_budget_exhausted = True
            return self._error_result(
                _BUDGET_ERROR_CODE, "episode world-time budget would be exceeded"
            )

        self._step_count += 1
        response = self._server.call_tool(name, typed_arguments)
        if response.get("isError") is True:
            self._tool_errors += 1
        elif name == "finish_episode":
            structured = response.get("structuredContent")
            if isinstance(structured, dict):
                self._finish_summary = cast(dict[str, object], structured)
        return json.dumps(response, ensure_ascii=False, sort_keys=True)

    def get_metrics(self) -> dict[str, object]:
        """Return bounded harness metrics alongside the existing world Action log."""

        elapsed = self.engine.now - self._world_started_at
        return {
            "tool_calls": self._step_count,
            "tool_errors": self._tool_errors,
            "step_budget_exhausted": self._step_budget_exhausted,
            "world_time_budget_exhausted": self._world_time_budget_exhausted,
            "world_days_elapsed": elapsed.total_seconds() / 86_400,
        }

    def _would_exceed_world_time(self, name: str, arguments: dict[str, object]) -> bool:
        definition = self.engine.registry.get(name)
        if definition is None:
            return False
        requested = self.engine.now + timedelta(minutes=definition.duration_minutes)
        if name == "advance_time":
            requested = self._requested_advance_time(arguments) or requested
        return requested > self._world_deadline

    def _requested_advance_time(self, arguments: dict[str, object]) -> datetime | None:
        minutes = arguments.get("minutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            return self.engine.now + timedelta(minutes=minutes)
        until = arguments.get("until")
        if not isinstance(until, str):
            return None
        try:
            parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _error_result(code: str, message: str) -> str:
        return json.dumps(
            {
                "content": [{"type": "text", "text": message}],
                "structuredContent": {
                    "ok": False,
                    "result": None,
                    "error": {"code": code, "message": message, "details": {}},
                    "action_id": None,
                },
                "isError": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
