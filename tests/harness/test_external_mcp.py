"""An external stdio client completes the same reference via the public CLI."""

import json
import select
import subprocess
import sys
from pathlib import Path

from mirrorfirm.episodes.references import ScriptedReferenceAdapter, reference_calls_for

ROOT = Path(__file__).resolve().parents[2]


def test_external_client_initializes_respects_scope_and_completes_reference(tmp_path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mirrorfirm.cli",
            "mcp",
            "serve",
            "--episode",
            "epi-uk-02",
            "--run-id",
            "external-test",
            "--results-root",
            str(tmp_path / "runs"),
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:

        def send(request):
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            if "id" not in request:
                return None
            assert select.select([process.stdout], [], [], 15)[0], (
                "MCP response timed out"
            )
            return json.loads(process.stdout.readline())

        init = send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "external-test", "version": "1"},
                },
            }
        )
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert "Reconcile Kestrel" in init["result"]["instructions"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "create_workpaper" in names and "propose_journal" not in names
        rejected = send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "propose_journal", "arguments": {}},
            }
        )
        assert rejected["result"]["isError"] is True
        assert (
            rejected["result"]["structuredContent"]["error"]["code"]
            == "PERMISSION_DENIED"
        )
        adapter = ScriptedReferenceAdapter(reference_calls_for("epi-uk-02"))
        for _ in range(99):
            turn = adapter.chat([], [])
            if not turn.tool_calls:
                break
            call = turn.tool_calls[0]
            response = send(
                {
                    "jsonrpc": "2.0",
                    "id": call.id,
                    "method": "tools/call",
                    "params": {
                        "name": call.name,
                        "arguments": json.loads(call.arguments),
                    },
                }
            )
            adapter.make_tool_result_messages(
                [(call.id, json.dumps(response["result"]))]
            )
            if call.name == "finish_episode":
                break
        process.stdin.close()
        assert process.wait(timeout=15) == 0
        assert (tmp_path / "runs/external-test/final.db").is_file()
        assert "unranked" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
