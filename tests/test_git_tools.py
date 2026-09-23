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


def test_git_push_and_pull_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=work, check=True)
    ws = Workspace(work)
    (work / "a.txt").write_text("v1")
    git_commit(ws, "init")

    from silkcode.tools.git import git_pull, git_push
    out = git_push(ws)
    assert out.startswith("Pushed main to origin.")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    assert (clone / "a.txt").read_text() == "v1"

    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=clone, check=True)
    (clone / "a.txt").write_text("v2")
    subprocess.run(["git", "commit", "-aqm", "update"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)

    out = git_pull(ws)
    assert "a.txt" in out or "Fast-forward" in out or "up to date" not in out
    assert (work / "a.txt").read_text() == "v2"


def test_push_if_needed(tmp_path, monkeypatch):
    from silkcode.tools.git import push_if_needed
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))

    # not a git repo -> nothing to do
    plain = tmp_path / "plain"
    plain.mkdir()
    assert push_if_needed(Workspace(plain)) is None

    # repo with no remote -> nothing to do
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=lonely, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=lonely, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=lonely, check=True)
    (lonely / "a.txt").write_text("x")
    git_commit(Workspace(lonely), "init")
    assert push_if_needed(Workspace(lonely)) is None

    # repo with a remote and unpushed commits -> pushes
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=lonely, check=True)
    out = push_if_needed(Workspace(lonely))
    assert out is not None and out.startswith("Pushed main")

    # up to date -> nothing to do
    assert push_if_needed(Workspace(lonely)) is None

    # new commit -> pushes again
    (lonely / "b.txt").write_text("y")
    git_commit(Workspace(lonely), "more")
    assert push_if_needed(Workspace(lonely)).startswith("Pushed main")


def test_git_error_is_single_line(tmp_path):
    out = git_diff(Workspace(tmp_path))  # not a git repository
    assert out.startswith("git error")
    assert "\n" not in out


def test_a_plain_folder_is_told_so_in_words(tmp_path):
    """Opening a directory that is not a repository is a normal thing to do,
    and the diff panel is where a new user meets the answer. It used to read
    `git error (exit 128): fatal: not a git repository (or any of the parent
    directories): .git`."""
    from silkcode.tools.git import git_diff, git_log, git_status
    from silkcode.workspace import Workspace

    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    (plain / "notes.txt").write_text("hello\n")
    ws = Workspace(str(plain))

    for report in (git_diff(ws), git_status(ws), git_log(ws)):
        assert "not a git repository" in report
        assert "git init" in report, f"no way forward offered: {report}"
        assert "exit 12" not in report, f"still quoting git's exit code: {report}"
        assert "fatal:" not in report and "warning:" not in report


def test_a_real_repository_is_unaffected(repo):
    """`git diff` and `git status` disagree about how to say it — exit 129 with
    'warning: Not a git repository' versus exit 128 with 'fatal: not a git
    repository' — so the check is case-insensitive, and must not catch
    anything else."""
    from silkcode.tools.git import git_status
    assert "not a git repository" not in git_status(repo)
