"""Read-only Git tools for V0.1 (SRS section 32; write operations are V0.2)."""

from __future__ import annotations

import os
import subprocess
import threading

from ..workspace import Workspace

# Attribution context (SRS section 60, provenance): set per turn by the agent
# loop in its worker thread, so commits made by the agent register Silk Code
# as co-author with the model that did the work. Human-initiated commits
# (outside an agent turn) carry no trailers. Thread-local so concurrent
# sessions attribute their own model correctly.
_attribution = threading.local()

CO_AUTHOR = "Co-Authored-By: Silk Code <agent@silkcode.dev>"


def set_attribution(model: str, session: int | None = None) -> None:
    _attribution.info = {"model": model, "session": session}


def clear_attribution() -> None:
    _attribution.info = None


def get_attribution() -> dict | None:
    return getattr(_attribution, "info", None)


def _with_trailers(message: str) -> str:
    info = get_attribution()
    if not info:
        return message
    trailers = [CO_AUTHOR, f"X-Silk-Model: {info['model']}"]
    if info.get("session"):
        trailers.append(f"X-Silk-Session: {info['session']}")
    return message.rstrip() + "\n\n" + "\n".join(trailers)


# Said once, in words, rather than echoing git's exit code at the user. The
# `git error` prefix is load-bearing: callers below test for it to decide
# whether a command succeeded.
NOT_A_REPOSITORY = ("git error: this project is not a git repository — "
                    "run `git init` here to track changes, revert turns, and "
                    "see diffs")


def _git(ws: Workspace, *args: str, env: dict | None = None, timeout: int = 60) -> str:
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        return ws.git(*args, env=env, timeout=timeout)
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
        # Opening a plain folder is a normal thing to do, and "git error (exit
        # 128): fatal: not a git repository (or any of the parent
        # directories): .git" is not the way to say so. It is the first thing
        # a new user sees in the diff panel.
        # `status` says "fatal: not a git repository" and exits 128; `diff`
        # says "warning: Not a git repository" and exits 129. Same situation,
        # and the diff panel is where a user meets it first.
        if "not a git repository" in first_line.lower():
            return NOT_A_REPOSITORY
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
    out = _git(ws, "commit", "-m", _with_trailers(message))
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


def git_add_remote(ws: Workspace, name: str, url: str) -> str:
    """Add a named remote without putting credentials in repository config."""
    if not name.strip() or not url.strip():
        return "git error: remote name and URL are required"
    out = _git(ws, "remote", "add", name, url)
    return out if out.startswith("git error") else f"Added remote {name}."


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
