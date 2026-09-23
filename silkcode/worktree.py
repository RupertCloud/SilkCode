"""An isolated worktree for a session that should not touch your checkout.

`silkcode --isolated` forks the repository at HEAD into a throwaway git
worktree on its own `silk/<stamp>` branch, and the session runs there: the
agent can edit, branch and commit freely while your working tree - including
its uncommitted changes, which the fork deliberately does not see - stays
exactly as you left it. The design is nac's sandbox worktree, minus the
container: the isolation here is about *your checkout*, not about the host.

What happens to the worktree at the end of the session depends on what the
session did, and errs toward keeping work:

- commits on the branch: kept, and the path and branch are named, so the
  work is one `git merge silk/<stamp>` away
- uncommitted changes: kept - removing a dirty worktree destroys work
- clean and unchanged: removed, branch and all; an unused fork is litter

This is a refusal-first feature: outside a git repository, or in one with
no commits yet, `--isolated` explains itself and stops rather than quietly
running unisolated - a person who asked for isolation must never get a
silent live mount instead.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .workspace import ToolError

BRANCH_PREFIX = "silk/"


@dataclass
class IsolatedWorktree:
    root: Path          # the worktree the session runs in
    branch: str         # silk/<stamp>
    repo_root: Path     # the live checkout it was forked from
    base: str           # the commit it was forked at


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=60)


def create(path: str | Path) -> IsolatedWorktree:
    """Fork the repository containing `path` at HEAD into a new worktree."""
    from .config import config_dir

    top = _git(path, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ToolError(
            "--isolated needs a git repository: the isolation is a git "
            f"worktree forked from HEAD, and {path} is not inside one.")
    repo_root = Path(top.stdout.strip())

    head = _git(repo_root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise ToolError(
            "--isolated needs at least one commit to fork from; this "
            "repository has none yet. Make an initial commit first.")
    base = head.stdout.strip()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch = f"{BRANCH_PREFIX}{stamp}"
    target = config_dir() / "worktrees" / f"{repo_root.name}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)

    added = _git(repo_root, "worktree", "add", "-b", branch, str(target), "HEAD")
    if added.returncode != 0:
        detail = (added.stderr or added.stdout).strip().splitlines()
        raise ToolError("could not create the isolated worktree: "
                        + (detail[0] if detail else "git worktree add failed"))
    return IsolatedWorktree(root=target, branch=branch, repo_root=repo_root, base=base)


def cleanup(wt: IsolatedWorktree) -> str:
    """Keep the worktree if it holds work; remove it if it is litter."""
    dirty = _git(wt.root, "status", "--porcelain")
    has_changes = dirty.returncode != 0 or bool(dirty.stdout.strip())

    ahead = _git(wt.root, "rev-list", "--count", f"{wt.base}..HEAD")
    commits = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0

    if commits or has_changes:
        what = []
        if commits:
            what.append(f"{commits} commit{'s' if commits != 1 else ''} on {wt.branch}")
        if has_changes:
            what.append("uncommitted changes")
        return (f"isolated worktree kept ({' and '.join(what)}): {wt.root}\n"
                f"  merge with:  git merge {wt.branch}\n"
                f"  discard with:  git worktree remove --force {wt.root} "
                f"&& git branch -D {wt.branch}")

    removed = _git(wt.repo_root, "worktree", "remove", str(wt.root))
    if removed.returncode != 0:
        # never force: whatever git is protecting is worth more than tidiness
        return f"isolated worktree left in place (git: {removed.stderr.strip()})"
    _git(wt.repo_root, "branch", "-D", wt.branch)
    return "isolated worktree removed (no commits, no changes)"
