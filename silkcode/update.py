"""Self-update support: update Silk Code without stopping the services.

Two pieces work together so you never have to manually `git pull` and
restart everything:

  * `silkcode update` (or the GUI's Update button / /api/update) pulls the
    latest code from the git remote into the installed checkout.
  * a running GUI daemon watches the checkout's HEAD commit and re-execs
    itself with the same arguments once new code lands (and it is idle), so
    the new code goes live on its own. Sessions are persisted on disk and
    survive the restart.

This works when silkcode is installed from a git checkout (e.g. a cloned
repo or `pip install -e .`). Wheel installs carry no git metadata; those
should be updated with `pip install -U silkcode` instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

import silkcode

ProgressFn = Callable[[str], None]


def package_root() -> Path:
    """Directory containing the installed silkcode package."""
    return Path(silkcode.__file__).resolve().parent.parent


def git_repo_root(start: Path | None = None) -> Path | None:
    """Find the git work-tree root containing `start` (default: the package)."""
    base = (start or package_root()).resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120)


def current_branch(repo: Path) -> str:
    proc = _git(repo, "branch", "--show-current")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    proc = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().removeprefix("origin/")
    return "main"


def working_tree_dirty(repo: Path) -> list[str]:
    proc = _git(repo, "status", "--porcelain")
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def head_commit(repo: Path) -> str | None:
    proc = _git(repo, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def head_changed(repo: Path, baseline: str | None) -> bool:
    current = head_commit(repo)
    return bool(current) and current != baseline


def update_installation(
    repo: Path | None = None,
    branch: str | None = None,
    force: bool = False,
    on_progress: ProgressFn = lambda s: None,
) -> dict:
    """Fast-forward the silkcode checkout to origin/<branch>.

    Only fast-forwards: a dirty tree or local-only commits are reported as
    errors (nothing is lost, nothing is force-reset). Returns
    {"status": "updated"|"up-to-date"|"error", "detail": str,
     "before": str|None, "after": str|None}.
    """
    repo = repo or git_repo_root()
    if repo is None or git_repo_root(start=repo) is None:
        return {"status": "error",
                "detail": "not a git checkout; update with: pip install -U silkcode"}
    branch = branch or current_branch(repo)
    on_progress(f"updating {repo} on branch {branch} ...")
    before = head_commit(repo)
    dirty = working_tree_dirty(repo)
    if dirty and not force:
        return {"status": "error",
                "detail": "working tree is dirty; commit or stash first: "
                          + "; ".join(d[:60] for d in dirty[:5])}
    fetch = _git(repo, "fetch", "origin", branch)
    if fetch.returncode != 0:
        return {"status": "error",
                "detail": f"git fetch failed: {fetch.stderr.strip()[:300]}"}
    merge = _git(repo, "merge", "--ff-only", f"origin/{branch}")
    if merge.returncode != 0:
        first = merge.stderr.strip().splitlines()[0] if merge.stderr.strip() else "merge refused"
        return {"status": "error",
                "detail": f"not a fast-forward (local commits?): {first[:300]}"}
    after = head_commit(repo)
    if after == before:
        return {"status": "up-to-date",
                "detail": f"already up to date at {before[:12] if before else '?'}",
                "before": before, "after": after}
    # sanity: the new code must import before we hot-apply it. It reports its
    # build rather than __version__ — the release number is the same string
    # before and after any update, so printing it proved only that *something*
    # imported, not that the something was the code we just pulled.
    check = subprocess.run(
        [sys.executable, "-c",
         "import silkcode, silkcode.gui.server, silkcode.swarm; "
         "from silkcode.version import commit; print(commit() or '')"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    if check.returncode != 0:
        return {"status": "error",
                "detail": f"new code does not import: {check.stderr.strip()[:300]}",
                "before": before, "after": after}
    detail = f"{before[:12]} -> {after[:12]}"
    imported = check.stdout.strip()
    if imported and after and not after.startswith(imported):
        # The pull landed, but `import silkcode` resolves somewhere else — a
        # site-packages copy shadowing this checkout. Restarting would come
        # back up on the old code while reporting success, so say so plainly.
        detail += (f" — warning: `import silkcode` resolves to {imported}, not "
                   f"{after[:12]}. Another install is shadowing this checkout; "
                   "run `silkcode version` to see which one is in use.")
    on_progress(f"updated {detail}")
    return {"status": "updated", "detail": detail,
            "before": before, "after": after}


def restart_argv(base_args: list[str]) -> list[str]:
    """argv for a clean daemon restart via `python -m silkcode <args>`."""
    return [sys.executable, "-m", "silkcode", *base_args]
