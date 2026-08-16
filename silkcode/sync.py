"""Tell the truth about a branch that moved while you were working on it.

A session holds a workspace for a long time. Meanwhile someone pushes to the
branch, a pull request gets merged, or the base branch advances. Nothing in
the workspace notices, and the agent keeps committing on top of a picture
that is no longer accurate.

`git status` alone is not enough here, because it answers from the last
fetch and it cannot distinguish two situations that look identical in its
output:

  * the branch has commits the base does not have, and they are real work
  * the branch has commits the base does not have, and they are the *same*
    work, already merged under a different sha by a squash merge

In the second case git says "ahead 8, behind 3" and invites you to reconcile
something that is not actually missing. Merging then produces a confusing
duplicate; the right move is to restart the branch from the base. Telling
those apart is the point of this module: the commits differ, the trees do
not, so compare trees.

Nothing here discards work. The survey is read-only, and every action that
changes the working tree refuses when there is anything uncommitted or
anything unmerged to lose, and says what it would take instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .workspace import Workspace

DEFAULT_REMOTE = "origin"


@dataclass
class Survey:
    """What is true about this branch right now, after a fetch."""

    branch: str = ""
    remote: str = DEFAULT_REMOTE
    upstream: str | None = None          # e.g. "origin/feature"
    base: str | None = None              # e.g. "origin/main"
    ahead: int = 0                       # local commits the upstream lacks
    behind: int = 0                      # upstream commits we lack
    # Commits the *base* lacks — for a feature branch this is the work in
    # flight, and it is a different number from `ahead`, which only compares
    # against this branch's own upstream.
    ahead_of_base: int = 0
    dirty: list[str] = field(default_factory=list)
    # The branch's commits are absent from the base, but its *content* is
    # already there: the signature of a squash merge.
    squash_merged: bool = False
    detached: bool = False
    no_remote: bool = False
    error: str | None = None

    @property
    def diverged(self) -> bool:
        return bool(self.ahead and self.behind)

    @property
    def in_sync(self) -> bool:
        return not self.ahead and not self.behind and not self.squash_merged

    def recommendation(self) -> tuple[str, str]:
        """(action, why) — the action `resync` would take, and the reason."""
        if self.error:
            return "none", self.error
        if self.no_remote:
            return "none", "no remote is configured, so there is nothing to sync with"
        if self.detached:
            return "none", "HEAD is detached; check out a branch first"
        if self.squash_merged:
            return "restart", (
                f"this branch's work is already in {self.base} under a different "
                "commit (a squash merge), so its commits are history rather than "
                "pending work — restart it from the base instead of merging")
        if self.behind and not self.ahead:
            return "fast-forward", (
                f"{self.upstream} moved {self.behind} commit(s) ahead and nothing "
                "local is unpushed")
        if self.diverged:
            return "merge", (
                f"both moved: {self.ahead} local commit(s) and {self.behind} "
                f"on {self.upstream}")
        if self.ahead:
            return "push", f"{self.ahead} local commit(s) are not on {self.upstream} yet"
        return "none", "already up to date"

    def summary(self) -> str:
        if self.error:
            return f"sync: {self.error}"
        if self.no_remote:
            return "sync: no remote configured"
        where = self.upstream or "(no upstream)"
        bits = [f"branch {self.branch}", f"tracking {where}"]
        if self.ahead:
            bits.append(f"{self.ahead} ahead")
        if self.behind:
            bits.append(f"{self.behind} behind")
        if self.ahead_of_base and self.base:
            bits.append(f"{self.ahead_of_base} ahead of {self.base}")
        if self.squash_merged:
            bits.append(f"already merged into {self.base}")
        if self.dirty:
            bits.append(f"{len(self.dirty)} uncommitted file(s)")
        if self.in_sync and not self.dirty:
            bits.append("up to date")
        action, why = self.recommendation()
        return " · ".join(bits) + (f"\nsuggested: {action} — {why}" if action != "none"
                                   else f"\n{why}")


def _run(ws: Workspace, *args: str, timeout: int = 60) -> str:
    from .tools.git import _git
    return _git(ws, *args, timeout=timeout)


def _ok(out: str) -> bool:
    return not out.startswith("git error")


def _count(ws: Workspace, spec: str) -> int:
    out = _run(ws, "rev-list", "--count", spec)
    return int(out.strip()) if _ok(out) and out.strip().isdigit() else 0


def survey(ws: Workspace, remote: str = DEFAULT_REMOTE,
           base: str | None = None, fetch: bool = True) -> Survey:
    """Read-only: fetch, then describe where this branch stands.

    Changes nothing in the working tree. `base` defaults to the remote's
    default branch, which is what a feature branch is measured against.
    """
    from .github import git_credential_env

    result = Survey(remote=remote)
    if not _ok(_run(ws, "rev-parse", "--git-dir")):
        result.error = "not a git repository"
        return result

    branch = _run(ws, "branch", "--show-current").strip()
    if not branch:
        result.detached = True
        result.error = "HEAD is detached"
        return result
    result.branch = branch

    remotes = _run(ws, "remote")
    if not _ok(remotes) or remote not in remotes.split():
        result.no_remote = True
        return result

    if fetch:
        # --prune so a branch deleted after its merge stops looking present
        out = _fetch(ws, remote, git_credential_env())
        if out and not _ok(out):
            result.error = out
            return result

    upstream = _run(ws, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    result.upstream = upstream if _ok(upstream) and upstream else None

    result.base = base or _default_base(ws, remote)

    if result.upstream:
        result.ahead = _count(ws, f"{result.upstream}..HEAD")
        result.behind = _count(ws, f"HEAD..{result.upstream}")

    status = _run(ws, "status", "--porcelain")
    if _ok(status):
        result.dirty = [line for line in status.splitlines() if line.strip()]

    if result.base and _ok(_run(ws, "rev-parse", "--verify", "--quiet", result.base)):
        result.ahead_of_base = _count(ws, f"{result.base}..HEAD")
    result.squash_merged = _is_squash_merged(ws, result.base)
    return result


def _fetch(ws: Workspace, remote: str, env: dict | None) -> str:
    from .tools.git import _git
    return _git(ws, "fetch", "--prune", remote, env=env, timeout=120)


def _default_base(ws: Workspace, remote: str) -> str | None:
    """The remote's default branch, e.g. origin/main."""
    head = _run(ws, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD").strip()
    if _ok(head) and head:
        return head.replace("refs/remotes/", "", 1)
    for candidate in (f"{remote}/main", f"{remote}/master"):
        if _ok(_run(ws, "rev-parse", "--verify", "--quiet", candidate)):
            return candidate
    return None


def _is_squash_merged(ws: Workspace, base: str | None) -> bool:
    """True when this branch has commits the base lacks, yet the two trees
    are identical — the work landed as a squash and these commits are now
    history, not pending work."""
    if not base or not _ok(_run(ws, "rev-parse", "--verify", "--quiet", base)):
        return False
    if _count(ws, f"{base}..HEAD") == 0:
        return False  # nothing of ours is missing from the base
    # `git diff --quiet` exits 1 when there are differences; _git turns a
    # non-zero exit into "git error: ...", so an empty result means identical.
    return _ok(_run(ws, "diff", "--quiet", base, "HEAD"))


def resync(ws: Workspace, remote: str = DEFAULT_REMOTE, base: str | None = None,
           allow_restart: bool = False) -> str:
    """Bring the branch up to date, or explain why it cannot be done safely.

    Never discards anything: uncommitted changes stop it, and restarting a
    squash-merged branch (which moves the branch pointer) needs
    allow_restart, because the caller should have seen the survey first.
    """
    state = survey(ws, remote=remote, base=base)
    if state.error:
        return f"cannot sync: {state.error}"
    if state.no_remote:
        return "cannot sync: no remote configured"

    action, why = state.recommendation()

    if state.dirty and action in ("fast-forward", "merge", "restart"):
        return (f"{len(state.dirty)} uncommitted file(s) would be affected; "
                "commit or stash them first, then sync again.\n"
                + state.summary())

    if action == "none":
        return state.summary()
    if action == "push":
        return (f"nothing to pull — {why}.\n"
                "Push when you are ready; sync does not push for you.")

    if action == "restart":
        if not allow_restart:
            return ("this branch is already merged into "
                    f"{state.base} (squash), so its commits are history.\n"
                    "Re-run with restart enabled to move it onto the base; "
                    "nothing would be lost, but the branch pointer moves.\n"
                    + state.summary())
        out = _run(ws, "checkout", "-B", state.branch, state.base or "")
        if not _ok(out):
            return f"restart failed: {out}"
        return (f"restarted {state.branch} from {state.base}; its work was already "
                "there under the squashed commit.")

    if action == "fast-forward":
        out = _run(ws, "merge", "--ff-only", state.upstream or "@{u}", timeout=120)
        if not _ok(out):
            return f"fast-forward failed: {out}"
        return f"fast-forwarded {state.branch} to {state.upstream} ({state.behind} commit(s))."

    # diverged
    out = _run(ws, "merge", "--no-edit", state.upstream or "@{u}", timeout=120)
    if not _ok(out):
        return ("merge stopped with conflicts; resolve them and commit, "
                f"then sync again.\n{out}")
    return f"merged {state.upstream} into {state.branch} ({state.behind} commit(s))."


def git_sync(ws: Workspace, apply: bool = False, restart: bool = False) -> str:
    """Agent tool: report where the branch stands, and optionally reconcile it."""
    if apply:
        return resync(ws, allow_restart=restart)
    return survey(ws).summary()
