"""Resyncing a branch that moved while a session was working on it.

Everything here is real git: the module exists to interpret git's own
answers, so stubbing git would only test the stub.
"""

from __future__ import annotations

import subprocess

import pytest

from silkcode.sync import resync, survey
from silkcode.workspace import Workspace


def git(*args, cwd=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check, env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
                                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                            "GIT_TERMINAL_PROMPT": "0"})


def configure_identity(path):
    """Set the identity in the repo itself, not just in the environment of
    the commands this file runs. resync() invokes git through the product's
    own helper, which inherits the ambient environment — and a CI runner has
    no global identity, so a merge there fails with "Committer identity
    unknown" while passing on a developer machine."""
    git("config", "user.email", "t@t", cwd=path)
    git("config", "user.name", "t", cwd=path)


def commit(path, name, text="x"):
    (path / name).write_text(text)
    git("add", "-A", cwd=path)
    git("commit", "-qm", f"add {name}", cwd=path)


@pytest.fixture
def clones(tmp_path):
    """A bare origin with main, plus two clones of it — 'ours' (the session's
    workspace) and 'theirs' (whoever else is pushing)."""
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    git("clone", "-q", str(origin), str(seed))
    commit(seed, "README.md", "# demo\n")
    git("push", "-q", "origin", "main", cwd=seed)

    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    git("clone", "-q", str(origin), str(ours))
    git("clone", "-q", str(origin), str(theirs))
    configure_identity(ours)
    configure_identity(theirs)
    # so origin/HEAD resolves and the base can be discovered
    git("remote", "set-head", "origin", "main", cwd=ours)
    return Workspace(ours), theirs, origin


# --------------------------------------------------------------------------
# reading the situation
# --------------------------------------------------------------------------

def test_a_quiet_branch_reports_up_to_date(clones):
    ws, _, _ = clones
    state = survey(ws)
    assert state.in_sync and not state.dirty
    assert state.recommendation()[0] == "none"
    assert "up to date" in state.summary()


def test_a_push_by_someone_else_is_noticed(clones):
    """The case this exists for: the branch moved and nothing local knows."""
    ws, theirs, _ = clones
    commit(theirs, "theirs.txt")
    git("push", "-q", "origin", "main", cwd=theirs)

    state = survey(ws)
    assert state.behind == 1 and state.ahead == 0
    assert state.recommendation()[0] == "fast-forward"


def test_local_work_alone_is_not_something_to_pull(clones):
    ws, _, _ = clones
    commit(ws.root, "mine.txt")
    state = survey(ws)
    assert state.ahead == 1 and state.behind == 0
    action, _ = state.recommendation()
    assert action == "push"


def test_both_moving_is_reported_as_divergence(clones):
    ws, theirs, _ = clones
    commit(ws.root, "mine.txt")
    commit(theirs, "theirs.txt")
    git("push", "-q", "origin", "main", cwd=theirs)

    state = survey(ws)
    assert state.diverged and state.ahead == 1 and state.behind == 1
    assert state.recommendation()[0] == "merge"


def test_uncommitted_changes_are_reported(clones):
    ws, _, _ = clones
    (ws.root / "scratch.txt").write_text("wip\n")
    assert survey(ws).dirty


def test_a_repository_with_no_remote(tmp_path):
    root = tmp_path / "solo"
    root.mkdir()
    git("init", "-q", str(root))
    commit(root, "a.txt")
    state = survey(Workspace(root))
    assert state.no_remote
    assert "no remote" in state.summary()


def test_a_directory_that_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    state = survey(Workspace(plain))
    assert state.error and "not a git repository" in state.error


# --------------------------------------------------------------------------
# the squash-merge case: commits differ, content does not
# --------------------------------------------------------------------------

def _squash_merge_ours_into_main(ws, origin, tmp_path):
    """Land our branch's work on main the way GitHub's squash merge does:
    one new commit holding the same tree, under a different sha."""
    integrator = tmp_path / "integrator"
    git("clone", "-q", str(origin), str(integrator))
    configure_identity(integrator)
    git("fetch", "-q", "origin", "feature", cwd=integrator)
    git("merge", "--squash", "origin/feature", cwd=integrator)
    git("commit", "-qm", "Feature (#1)", cwd=integrator)
    git("push", "-q", "origin", "main", cwd=integrator)


def test_a_squash_merged_branch_is_not_mistaken_for_pending_work(clones, tmp_path):
    """git reports such a branch as ahead — its commits really are absent
    from main — but the work is already there under the squashed commit.
    Merging would be wrong; the branch should restart from the base."""
    ws, _, origin = clones
    git("checkout", "-qb", "feature", cwd=ws.root)
    commit(ws.root, "feature.txt", "work\n")
    git("push", "-q", "-u", "origin", "feature", cwd=ws.root)
    _squash_merge_ours_into_main(ws, origin, tmp_path)

    state = survey(ws, base="origin/main")
    # `ahead` compares against origin/feature, which still holds our commit;
    # the number that matters here is how far ahead of the *base* we are.
    assert state.ahead_of_base >= 1, "our commits are genuinely absent from main"
    assert state.squash_merged, "but the content is already there"
    action, why = state.recommendation()
    assert action == "restart"
    assert "squash" in why


def test_a_branch_with_real_unmerged_work_is_not_called_merged(clones, tmp_path):
    """The mirror image, and the one that must never be wrong: work that is
    genuinely not in the base must not be reported as already merged."""
    ws, _, origin = clones
    git("checkout", "-qb", "feature", cwd=ws.root)
    commit(ws.root, "feature.txt", "work\n")
    git("push", "-q", "-u", "origin", "feature", cwd=ws.root)
    _squash_merge_ours_into_main(ws, origin, tmp_path)
    # and then keep working after the merge
    commit(ws.root, "later.txt", "more work\n")

    state = survey(ws, base="origin/main")
    assert not state.squash_merged, "unmerged work was reported as already merged"
    assert state.recommendation()[0] != "restart"


# --------------------------------------------------------------------------
# acting on it
# --------------------------------------------------------------------------

def test_resync_fast_forwards(clones):
    ws, theirs, _ = clones
    commit(theirs, "theirs.txt", "from them\n")
    git("push", "-q", "origin", "main", cwd=theirs)

    message = resync(ws)
    assert "fast-forwarded" in message
    assert (ws.root / "theirs.txt").read_text() == "from them\n"
    assert survey(ws).in_sync


def test_resync_merges_a_divergence(clones):
    ws, theirs, _ = clones
    commit(ws.root, "mine.txt")
    commit(theirs, "theirs.txt")
    git("push", "-q", "origin", "main", cwd=theirs)

    message = resync(ws)
    assert "merged" in message
    assert (ws.root / "mine.txt").exists() and (ws.root / "theirs.txt").exists()


def test_resync_refuses_to_touch_uncommitted_work(clones):
    """The thing it must never do is lose something."""
    ws, theirs, _ = clones
    commit(theirs, "theirs.txt")
    git("push", "-q", "origin", "main", cwd=theirs)
    (ws.root / "wip.txt").write_text("unsaved\n")

    message = resync(ws)
    assert "uncommitted" in message
    assert (ws.root / "wip.txt").read_text() == "unsaved\n"
    assert not (ws.root / "theirs.txt").exists(), "it pulled over uncommitted work"


def test_resync_reports_conflicts_instead_of_guessing(clones):
    ws, theirs, _ = clones
    commit(ws.root, "shared.txt", "our version\n")
    commit(theirs, "shared.txt", "their version\n")
    git("push", "-q", "origin", "main", cwd=theirs)

    message = resync(ws)
    assert "conflict" in message.lower()


def test_restarting_a_squash_merged_branch_needs_saying_so(clones, tmp_path):
    ws, _, origin = clones
    git("checkout", "-qb", "feature", cwd=ws.root)
    commit(ws.root, "feature.txt", "work\n")
    git("push", "-q", "-u", "origin", "feature", cwd=ws.root)
    _squash_merge_ours_into_main(ws, origin, tmp_path)

    held = resync(ws, base="origin/main")
    assert "restart" in held.lower() and "already merged" in held

    done = resync(ws, base="origin/main", allow_restart=True)
    assert "restarted" in done
    state = survey(ws, base="origin/main")
    assert not state.squash_merged and state.ahead == 0


def test_resync_does_not_push_for_you(clones):
    ws, _, _ = clones
    commit(ws.root, "mine.txt")
    message = resync(ws)
    assert "does not push" in message
    assert survey(ws).ahead == 1, "it pushed anyway"


def test_a_merge_that_fails_for_another_reason_is_not_called_a_conflict(clones, monkeypatch):
    """A merge fails for reasons other than conflicts — no configured
    identity, a hook refusing it. Telling someone to resolve conflicts then
    is advice they cannot act on, and it hides the real cause. (CI caught
    this: the runner has no git identity, so every divergent merge reported
    conflicts that did not exist.)"""
    ws, theirs, _ = clones
    commit(ws.root, "mine.txt")
    commit(theirs, "theirs.txt")
    git("push", "-q", "origin", "main", cwd=theirs)
    # remove the identity so the merge commit cannot be created
    git("config", "--unset", "user.email", cwd=ws.root)
    git("config", "--unset", "user.name", cwd=ws.root)

    from silkcode import sync as sync_mod
    real = sync_mod._run

    def no_identity(workspace, *args, **kwargs):
        if args and args[0] == "merge":
            return "git error (exit 128): Committer identity unknown"
        return real(workspace, *args, **kwargs)

    monkeypatch.setattr(sync_mod, "_run", no_identity)
    message = sync_mod.resync(ws)
    assert "conflict" not in message.lower(), message
    assert "Committer identity unknown" in message
    assert "unchanged" in message
