"""Read-only Git tools for V0.1 (SRS section 32; write operations are V0.2)."""

from __future__ import annotations

import subprocess

from ..workspace import Workspace


def _git(ws: Workspace, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"git error: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        first_line = detail.splitlines()[0] if detail else "unknown error"
        return f"git error (exit {proc.returncode}): {first_line}"
    return proc.stdout


def git_status(ws: Workspace) -> str:
    out = _git(ws, "status", "--short", "--branch")
    return out.strip() or "(clean working tree)"


def git_diff(ws: Workspace, staged: bool = False) -> str:
    args = ["diff", "--staged"] if staged else ["diff"]
    out = _git(ws, *args)
    if out.startswith("git error"):
        return out
    return out.strip() or "(no changes)"


def git_log(ws: Workspace, limit: int = 10) -> str:
    limit = min(max(int(limit), 1), 100)
    out = _git(ws, "log", f"-{limit}", "--oneline", "--decorate")
    return out.strip() or "(no commits)"
