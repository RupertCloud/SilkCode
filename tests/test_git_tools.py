import subprocess

import pytest

from silkcode.tools.git import git_commit, git_diff, git_log, git_status
from silkcode.workspace import Workspace


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return Workspace(tmp_path)


def test_git_commit_stages_and_commits(repo):
    (repo.root / "a.py").write_text("x = 1\n")
    out = git_commit(repo, "Add a.py")
    assert out.startswith("Committed:")
    assert "Add a.py" in out
    assert "Add a.py" in git_log(repo)
    assert git_status(repo).startswith("## ") or "clean" in git_status(repo)


def test_git_commit_empty_message(repo):
    assert "must not be empty" in git_commit(repo, "   ")


def test_git_commit_nothing_to_commit(repo):
    out = git_commit(repo, "empty")
    assert out.startswith("git error")


def test_git_diff_in_repo(repo):
    (repo.root / "a.py").write_text("x = 1\n")
    git_commit(repo, "init")
    (repo.root / "a.py").write_text("x = 2\n")
    diff = git_diff(repo)
    assert "-x = 1" in diff and "+x = 2" in diff


def test_git_error_is_single_line(tmp_path):
    out = git_diff(Workspace(tmp_path))  # not a git repository
    assert out.startswith("git error")
    assert "\n" not in out
