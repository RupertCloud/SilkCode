"""Self-update support: update Silk Code without stopping the services.

Two pieces work together so you never have to manually `git pull` and
restart everything:

  * `silkcode update` (or the GUI's Update button / /api/update) pulls the
    latest code from the git remote into the installed checkout.
  * a running GUI daemon watches the checkout's HEAD commit and re-execs
    itself with the same arguments once new code lands (and it is idle), so
    the new code goes live on its own. Sessions are persisted on disk and
    survive the restart.

A git checkout (a clone, or `pip install -e .`) is fast-forwarded in place.
An install that carries no git metadata - `pip install
git+https://github.com/RupertCloud/SilkCode`, which is what the README leads
with - is reinstalled from wherever pip got it, recovered from the PEP 610
record pip wrote at install time. There is no PyPI package to fall back on,
so guessing `pip install -U silkcode` would only send people to a 404.
"""

from __future__ import annotations

import json
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


def install_origin() -> dict | None:
    """Where pip got this copy of Silk Code, from pip's own record.

    pip writes a PEP 610 `direct_url.json` beside the installed metadata for
    anything installed from a URL or a path - which is exactly how it
    remembers that `pip install git+https://github.com/...` came from git, and
    which revision was asked for. Returns {"kind", "spec"} where `spec` is
    something `pip install` accepts, or None when there is no such record
    (installed from an index, or a pip too old to write one).
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        raw = distribution("silkcode").read_text("direct_url.json")
    except (PackageNotFoundError, OSError, ImportError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    url = data.get("url")
    if not url:
        return None
    vcs = data.get("vcs_info")
    if vcs:
        spec = f"{vcs.get('vcs', 'git')}+{url}"
        # The branch/tag originally asked for, so `silkcode update` tracks the
        # same thing the install did rather than silently jumping to default.
        revision = vcs.get("requested_revision")
        if revision:
            spec += f"@{revision}"
        return {"kind": "vcs", "spec": spec, "url": url,
                "commit_id": vcs.get("commit_id")}
    if (data.get("dir_info") or {}).get("editable"):
        return {"kind": "editable", "spec": url, "url": url}
    return {"kind": "archive", "spec": url, "url": url}


def update_pip_install(spec: str, on_progress: ProgressFn = lambda s: None) -> dict:
    """Reinstall from the same place pip originally got it.

    `--force-reinstall` is not optional here: the version string is static
    between releases, so a plain `--upgrade` against a git URL decides the
    requirement is already satisfied and does nothing at all.
    """
    on_progress(f"reinstalling from {spec} ...")
    before = _installed_commit()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", spec],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return {"status": "error",
                "detail": "pip install failed: " + (tail[-1][:300] if tail else "unknown error"),
                "before": before, "after": before}
    after = _installed_commit()
    if before and after and before == after:
        return {"status": "up-to-date",
                "detail": f"Already up to date: still at {before[:12]}, "
                          "which is what the source has.",
                "before": before, "after": after}
    if not (before and after):
        # pip succeeded, but nothing on disk can name the commit, so there is
        # no honest way to say whether the code moved. Report the reinstall,
        # not an improvement we cannot see.
        return {"status": "updated",
                "detail": f"reinstalled from {spec} (this install records no "
                          "commit, so there is nothing to compare)",
                "before": before, "after": after}
    return {"status": "updated",
            "detail": f"Updated: {before[:12]} -> {after[:12]}",
            "before": before, "after": after}


# Asked in a fresh interpreter, because after pip has replaced the package this
# process is still holding the old one. A checkout can answer from git; a
# `pip install git+...` has no git metadata anywhere, and its commit lives only
# in the PEP 610 record - which is precisely the thing a reinstall rewrites when
# it picks up new upstream code, so it is what makes "did anything change?"
# answerable at all for that install.
_COMMIT_PROBE = """
import json
from importlib.metadata import distribution
from silkcode.version import commit
found = None
try:
    raw = distribution("silkcode").read_text("direct_url.json")
    if raw:
        found = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
except Exception:
    pass
print(found or commit() or "")
"""


def _installed_commit() -> str | None:
    """The commit the installed package is actually on, or None if nothing
    on disk can say."""
    try:
        proc = subprocess.run([sys.executable, "-c", _COMMIT_PROBE],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


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
        origin = install_origin()
        if origin and origin["kind"] == "vcs":
            return update_pip_install(origin["spec"], on_progress=on_progress)
        return {"status": "error",
                "detail": "not a git checkout, and pip recorded no source to "
                          "reinstall from. Reinstall with: pip install --upgrade "
                          "--force-reinstall git+https://github.com/RupertCloud/SilkCode"}
    branch = branch or current_branch(repo)
    # "checking", not "updating": at this point we do not yet know whether
    # there is anything to pull, and leading with "updating" makes a run that
    # changed nothing read as though it did.
    on_progress(f"checking {repo} on branch {branch} ...")
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
                "detail": f"Already up to date: {branch} is at "
                          f"{before[:12] if before else '?'}, same as origin.",
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
    detail = f"Updated {branch}: {before[:12]} -> {after[:12]}"
    imported = check.stdout.strip()
    if imported and after and not after.startswith(imported):
        # The pull landed, but `import silkcode` resolves somewhere else — a
        # site-packages copy shadowing this checkout. Restarting would come
        # back up on the old code while reporting success, so say so plainly.
        detail += (f" — warning: `import silkcode` resolves to {imported}, not "
                   f"{after[:12]}. Another install is shadowing this checkout; "
                   "run `silkcode version` to see which one is in use.")
    return {"status": "updated", "detail": detail,
            "before": before, "after": after}


def restart_argv(base_args: list[str]) -> list[str]:
    """argv for a clean daemon restart via `python -m silkcode <args>`."""
    return [sys.executable, "-m", "silkcode", *base_args]
