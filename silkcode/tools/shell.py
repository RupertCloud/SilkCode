"""Terminal execution tool (SRS section 29)."""

from __future__ import annotations

import subprocess

from ..workspace import Workspace

MAX_OUTPUT_CHARS = 20_000


def run_command(ws: Workspace, command: str, timeout: int = 120) -> str:
    timeout = min(max(int(timeout), 1), 600)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds: {command}"
    out = proc.stdout or ""
    if proc.stderr:
        out = out + ("\n" if out else "") + proc.stderr
    out = out.strip() or "(no output)"
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
    return f"exit code: {proc.returncode}\n{out}"
