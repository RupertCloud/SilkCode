"""Read-only Git tools for V0.1 (SRS section 32; write operations are V0.2)."""

from __future__ import annotations

import os
import subprocess

from ..workspace import Workspace


def _git(ws: Workspace, *args: str, env: dict | None = None, timeout: int = 60) -> str:
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
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


def git_commit(ws: Workspace, message: str, add_all: bool = True) -> str:
    """Stage and commit changes (SRS section 32; V0.2)."""
    if not message.strip():
        return "git error: commit message must not be empty"
    if add_all:
        staged = _git(ws, "add", "-A")
        if staged.startswith("git error"):
            return staged
    out = _git(ws, "commit", "-m", message)
    if out.startswith("git error"):
        return out
    head = _git(ws, "log", "-1", "--oneline")
    return f"Committed: {head.strip() or out.strip()}"


def git_log(ws: Workspace, limit: int = 10) -> str:
    limit = min(max(int(limit), 1), 100)
    out = _git(ws, "log", f"-{limit}", "--oneline", "--decorate")
    return out.strip() or "(no commits)"


def _current_branch(ws: Workspace) -> str:
    return _git(ws, "branch", "--show-current").strip()


def git_push(ws: Workspace, remote: str = "origin", branch: str | None = None,
             set_upstream: bool = True) -> str:
    from ..github import git_credential_env
    branch = branch or _current_branch(ws)
    if not branch or branch.startswith("git error"):
        return "git error: cannot determine the current branch; pass 'branch' explicitly"
    args = ["push"] + (["-u"] if set_upstream else []) + [remote, branch]
    out = _git(ws, *args, env=git_credential_env(), timeout=120)
    if out.startswith("git error"):
        return out
    return f"Pushed {branch} to {remote}." + (f"\n{out.strip()}" if out.strip() else "")


def push_if_needed(ws: Workspace) -> str | None:
    """Push the current branch if it has commits the remote doesn't.
    Returns a status message, or None when there is nothing to push
    (no repo, no commits, no remote, or already up to date)."""
    if _git(ws, "rev-parse", "--verify", "HEAD").startswith("git error"):
        return None
    remotes = _git(ws, "remote")
    if remotes.startswith("git error") or not remotes.strip():
        return None
    ahead = _git(ws, "rev-list", "--count", "@{u}..HEAD")
    if not ahead.startswith("git error") and ahead.strip() == "0":
        return None  # upstream exists and nothing is ahead
    return git_push(ws)


def git_pull(ws: Workspace, remote: str = "origin", branch: str | None = None) -> str:
    from ..github import git_credential_env
    args = ["pull", remote] + ([branch] if branch else [])
    out = _git(ws, *args, env=git_credential_env(), timeout=120)
    return out.strip() or "(up to date)"
