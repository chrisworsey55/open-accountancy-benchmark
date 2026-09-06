"""MCP lifecycle, protocol errors and notification non-execution."""

import io
import json

from mirrorfirm.tools.server import MCPStdioSession


class Endpoint:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return {"tools": []}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


def test_lifecycle_and_protocol_errors():
    endpoint = Endpoint()
    session = MCPStdioSession(endpoint)

    def request(method, params=None, identifier=1):
        return session.handle(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": method,
                    "params": params or {},
                }
            )
        )

    assert session.handle("{")["error"]["code"] == -32700
    assert session.handle("null")["error"]["code"] == -32600
    assert request("tools/list")["error"]["code"] == -32000
    result = request(
        "initialize",
        {
            "protocolVersion": "unsupported",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert result["result"]["protocolVersion"] == "2025-06-18"
    assert (
        session.handle('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    )
    assert request("ping")["result"] == {}
    assert request("tools/list")["result"] == {"tools": []}
    assert request("missing")["error"]["code"] == -32601
    assert (
        request("tools/call", {"name": "test", "arguments": []})["error"]["code"]
        == -32602
    )
    assert (
        session.handle(
            '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"test"}}'
        )
        is None
    )
    assert endpoint.calls == []
    assert request("tools/call", {"name": "test"})["result"]["isError"] is False
    assert endpoint.calls == [("test", {})]


def test_stdio_recovers_from_parse_error_and_never_answers_notifications():
    output = io.StringIO()
    MCPStdioSession(Endpoint()).serve(
        io.StringIO(
            '{\n{"jsonrpc":"2.0","method":"notifications/cancelled"}\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
        ),
        output,
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert responses[0]["error"]["code"] == -32700
    assert responses[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
