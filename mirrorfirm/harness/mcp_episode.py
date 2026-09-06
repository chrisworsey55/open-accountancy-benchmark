"""External MCP session through the same stateful scope and budget controls."""

from __future__ import annotations

import json
import time
from typing import TextIO, cast

from mirrorfirm.harness.stateful_episode import StatefulEpisodeAdapter
from mirrorfirm.security import canonical_sanitized_json
from mirrorfirm.tools.server import MCPStdioSession


def serve_episode(
    executor: StatefulEpisodeAdapter,
    instructions: str,
    streams: tuple[TextIO, TextIO],
    transcript: TextIO,
) -> dict[str, object]:
    """Persist external tool evidence; external model tokens and costs are unknown.

    This developer connection cannot produce a ranked provider baseline. The server
    cannot verify tokens consumed in a client process it does not control.
    """
    started = time.monotonic()

    class Endpoint:
        def list_tools(self) -> dict[str, object]:
            return {
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "inputSchema": tool["parameters"],
                    }
                    for tool in executor.provider_tools
                ]
            }

        def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, object]:
            response = cast(
                dict[str, object],
                json.loads(executor.execute(name, json.dumps(arguments))),
            )
            transcript.write(
                canonical_sanitized_json(
                    {"tool": name, "arguments": arguments, "result": response}
                ).decode()
            )
            transcript.flush()
            return response

    MCPStdioSession(Endpoint(), instructions=instructions).serve(
        *streams, stopped=lambda: executor.is_finished or executor.budget_exhausted
    )
    return {
        "finished_cleanly": executor.is_finished,
        "episode_finished": executor.is_finished,
        "finish_summary": executor.finish_summary,
        "tool_metrics": executor.get_metrics(),
        "wall_clock_seconds": time.monotonic() - started,
        "external_usage_unverified": True,
        "ranking_eligible": False,
    }
