"""Silk Code's own directory must not become part of the user's project.

.silkcode/ holds the workspace lock, project memory and project skills. It
lives inside the repository being worked on, which makes it the agent's
problem: `git add -A && git commit` is an ordinary thing for an agent to do,
and without this it would commit a lock file naming a pid — and whatever
memory holds — into the user's repository and push it.
"""

from __future__ import annotations

import subprocess

import pytest

from silkcode.lock import acquire
from silkcode.memory import remember
from silkcode.statedir import ensure_ignored, state_dir
from silkcode.workspace import Workspace


def _repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "initial"], check=True)
    return root


def _status(root):
    return subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                          capture_output=True, text=True).stdout


def test_the_state_directory_ignores_itself(tmp_path):
    directory = state_dir(tmp_path / "project")
    assert (directory / ".gitignore").read_text().strip().endswith("*")


def test_taking_the_lock_does_not_dirty_the_repository(tmp_path):
    root = _repo(tmp_path)
    assert _status(root) == "", "fixture repo should start clean"
    acquire(root, "session-1")
    assert _status(root) == "", f"the lock showed up in git status:\n{_status(root)}"


def test_remembering_does_not_dirty_the_repository(tmp_path):
    root = _repo(tmp_path)
    remember(Workspace(root), "the parser is generated, do not edit it")
    assert _status(root) == "", f"memory showed up in git status:\n{_status(root)}"


def test_an_agent_committing_everything_cannot_commit_our_state(tmp_path):
    """The failure that matters: the lock file and the user's notes ending up
    in their repository, and then pushed."""
    root = _repo(tmp_path)
    acquire(root, "session-1")
    remember(Workspace(root), "an internal note")
    (root / "feature.py").write_text("def f(): pass\n")

    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "add feature"], check=True)

    tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    assert "feature.py" in tracked, "the user's own work must still be committed"
    assert not [f for f in tracked if f.startswith(".silkcode/")], \
        f"Silk Code state was committed to the user's repository: {tracked}"


def test_an_existing_gitignore_is_never_overwritten(tmp_path):
    """Someone who edited it deliberately keeps their version."""
    directory = tmp_path / "project" / ".silkcode"
    directory.mkdir(parents=True)
    (directory / ".gitignore").write_text("# mine\n!keep.txt\n")
    ensure_ignored(directory)
    assert (directory / ".gitignore").read_text() == "# mine\n!keep.txt\n"


def test_a_read_only_workspace_does_not_raise(tmp_path, monkeypatch):
    """Hygiene, not correctness: it must never be the reason something fails."""
    directory = tmp_path / "ro"
    directory.mkdir()

    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", refuse)
    ensure_ignored(directory)  # must not raise
