"""A dependency-free stdio MCP adapter for the WP-07 tool registry."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Protocol, TextIO, cast

from .engine import WorldToolEngine


class MCPToolServer:
    """Expose one scoped ``WorldToolEngine`` through the MCP tools methods."""

    def __init__(self, engine: WorldToolEngine) -> None:
        self.engine = engine

    def list_tools(self) -> dict[str, object]:
        """Return registry-derived MCP tool schemas."""

        return {"tools": self.engine.registry.mcp_tools()}

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Call one tool and shape its typed result for MCP clients."""

        result = self.engine.call(name, arguments)
        if result.ok:
            definition = self.engine.registry.get(name)
            if definition is None or result.result is None:
                raise ValueError("successful tool call has no registered typed output")
            output = definition.output_model.model_validate(result.result).model_dump(
                mode="json", exclude_none=True
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(output, ensure_ascii=False, sort_keys=True),
                    }
                ],
                "structuredContent": output,
                "isError": False,
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": result.error.message if result.error else "tool failed",
                }
            ],
            "structuredContent": result.model_dump(mode="json"),
            "isError": True,
        }

    def serve_stdio(
        self, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout
    ) -> None:
        """Serve newline-delimited JSON-RPC MCP requests over standard input/output."""

        MCPStdioSession(self).serve(input_stream, output_stream)


class ToolEndpoint(Protocol):
    def list_tools(self) -> dict[str, object]: ...
    def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]: ...


class MCPStdioSession:
    """Serial MCP 2025-06-18 lifecycle over bounded newline-delimited JSON-RPC."""

    MAX_LINE = 1_048_576

    def __init__(self, endpoint: ToolEndpoint, *, instructions: str = "") -> None:
        self.endpoint = endpoint
        self.instructions = instructions
        self.initialized = False
        self.ready = False

    def handle(self, raw: str) -> dict[str, object] | None:
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            return self._error(None, -32700, "Parse error")
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
        ):
            return self._error(None, -32600, "Invalid Request")
        identifier = request.get("id")
        if "id" in request and (
            not isinstance(identifier, (str, int)) or isinstance(identifier, bool)
        ):
            return self._error(None, -32600, "Invalid request id")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            # Notifications never execute tools and never receive a response.
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(identifier, -32602, "Params must be an object")
        result: dict[str, object]
        if method == "initialize":
            info = params.get("clientInfo")
            if (
                self.initialized
                or not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(info, dict)
                or not isinstance(info.get("name"), str)
                or not isinstance(info.get("version"), str)
            ):
                return self._error(identifier, -32602, "Invalid initialization")
            self.initialized = True
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "franklin-mcgrath", "version": "0.1.1"},
                "instructions": self.instructions,
            }
        elif method == "ping":
            result = {}
        elif not self.ready:
            return self._error(identifier, -32000, "Initialize the session first")
        elif method == "tools/list":
            if params.get("cursor") is not None:
                return self._error(
                    identifier, -32602, "This tool list has no continuation cursor"
                )
            result = self.endpoint.list_tools()
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._error(
                    identifier, -32602, "Expected tool name and argument object"
                )
            result = self.endpoint.call_tool(name, cast(dict[str, object], arguments))
        else:
            return self._error(identifier, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def serve(
        self,
        input_stream: TextIO,
        output_stream: TextIO,
        *,
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        while not stopped():
            line = input_stream.readline(self.MAX_LINE + 1)
            if not line:
                return
            if len(line) > self.MAX_LINE or len(line.encode("utf-8")) > self.MAX_LINE:
                output_stream.write(
                    json.dumps(self._error(None, -32600, "Request exceeds one MiB"))
                    + "\n"
                )
                output_stream.flush()
                return
            if not line.strip():
                continue
            payload = self.handle(line)
            if payload is not None:
                output_stream.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                )
                output_stream.flush()

    @staticmethod
    def _error(identifier: object, code: int, message: str) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": code, "message": message},
        }
