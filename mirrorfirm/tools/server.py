"""A dependency-free stdio MCP adapter for the WP-07 tool registry."""

from __future__ import annotations

import json
import sys
from typing import TextIO

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
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result.result, ensure_ascii=False, sort_keys=True
                        ),
                    }
                ],
                "structuredContent": result.model_dump(mode="json"),
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

        for line in input_stream:
            if not line.strip():
                continue
            request = json.loads(line)
            request_id = request.get("id")
            try:
                method = request["method"]
                params = request.get("params", {})
                if method == "tools/list":
                    response = self.list_tools()
                elif method == "tools/call":
                    response = self.call_tool(
                        params["name"], params.get("arguments", {})
                    )
                else:
                    raise ValueError(f"unsupported MCP method {method!r}")
                payload = {"jsonrpc": "2.0", "id": request_id, "result": response}
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": str(error)},
                }
            output_stream.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            output_stream.flush()
