"""Read-only subprocess document parsing for the WP-07 document tools."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .errors import ToolExecutionError

_READ_SCRIPT = (
    "from pathlib import Path; import sys; "
    "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
)


def parse_document_text(path: Path) -> str:
    """Parse one fixture document outside the world-engine process.

    ``SANDBOX=none`` is the portable local mode.  ``SANDBOX=podman`` applies the
    networkless, capability-dropped container command used by CI when Podman and the
    configured parser image are available.  Neither mode exposes a write operation to
    the agent tool surface.
    """

    mode = os.environ.get("SANDBOX", "none")
    if mode == "none":
        command = [sys.executable, "-I", "-c", _READ_SCRIPT, str(path)]
    elif mode == "podman":
        command = [
            "podman",
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--read-only",
            "--volume",
            f"{path.parent}:/input:ro",
            "mirrorfirm-document-parser:0.1",
            "python",
            "-I",
            "-c",
            _READ_SCRIPT,
            f"/input/{path.name}",
        ]
    else:
        raise ToolExecutionError("VALIDATION_ERROR", "SANDBOX must be none or podman")
    try:
        completed = subprocess.run(command, check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ToolExecutionError(
            "VALIDATION_ERROR", "sandboxed document parse failed"
        ) from error
    return completed.stdout.decode("utf-8", errors="replace")
