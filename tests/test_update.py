"""Tests for self-update (silkcode/update.py): git fast-forward pulls and
the daemon restart wiring."""

import subprocess
import sys

from silkcode.update import (
    git_repo_root,
    head_changed,
    restart_argv,
    update_installation,
)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def _commit(cwd, filename="a.txt", content="one\n"):
    (cwd / filename).write_text(content)
    _git(cwd, "add", filename)
    _git(cwd, "commit", "-m", f"commit {filename}")


def _make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _commit(origin, "a.txt", "one\n")
    return origin


def _clone(origin, dest):
    _git(origin.parent, "clone", "-q", "--branch", "main", str(origin), str(dest))
    return dest


def test_git_repo_root_finds_checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "silkcode").mkdir(parents=True)
    (repo / "silkcode" / "__init__.py").write_text("")
    _git(repo, "init", "-b", "main")
    assert git_repo_root(start=repo) == repo.resolve()
    # a path outside any repo yields None
    outside = tmp_path / "norepo"
    outside.mkdir()
    assert git_repo_root(start=outside) is None


def test_update_installation_fast_forward(tmp_path):
    origin = _make_origin(tmp_path)
    install = _clone(origin, tmp_path / "install")
    before = git_repo_root(start=install)
    assert before == install.resolve()

    _commit(origin, "b.txt", "two\n")  # new code upstream

    result = update_installation(repo=install)
    assert result["status"] == "updated"
    assert result["before"] != result["after"]
    assert (install / "b.txt").is_file()  # pull actually landed


def test_update_installation_up_to_date(tmp_path):
    origin = _make_origin(tmp_path)
    install = _clone(origin, tmp_path / "install")
    result = update_installation(repo=install)
    assert result["status"] == "up-to-date"


def test_update_installation_dirty_tree_refuses(tmp_path):
    origin = _make_origin(tmp_path)
    install = _clone(origin, tmp_path / "install")
    (install / "dirty.txt").write_text("local change\n")
    _commit(origin, "b.txt", "two\n")

    result = update_installation(repo=install)
    assert result["status"] == "error"
    assert "dirty" in result["detail"]
    assert not (install / "b.txt").exists()  # nothing was pulled


def test_update_installation_dirty_tree_force(tmp_path):
    origin = _make_origin(tmp_path)
    install = _clone(origin, tmp_path / "install")
    (install / "dirty.txt").write_text("local change\n")
    _commit(origin, "b.txt", "two\n")

    result = update_installation(repo=install, force=True)
    assert result["status"] == "updated"
    assert (install / "b.txt").is_file()


def test_update_installation_diverged_refuses(tmp_path):
    origin = _make_origin(tmp_path)
    install = _clone(origin, tmp_path / "install")
    _commit(install, "local.txt", "local\n")   # local-only commit
    _commit(origin, "b.txt", "two\n")          # upstream commit

    result = update_installation(repo=install)
    assert result["status"] == "error"
    assert "fast-forward" in result["detail"]
    # nothing lost: local commit is still there
    assert (install / "local.txt").is_file()


def test_update_installation_not_a_repo(tmp_path):
    result = update_installation(repo=tmp_path / "missing")
    assert result["status"] == "error"
    assert "pip install" in result["detail"]


def test_head_changed():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _git(repo, "init", "-b", "main")
        _commit(repo, "a.txt", "one\n")
        assert head_changed(repo, None) is True   # HEAD exists now
        head = subprocess.run(["git", "-C", td, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        assert head_changed(repo, head) is False  # same commit


def test_restart_argv():
    argv = restart_argv(["gui", ".", "--port", "9000"])
    assert argv == [sys.executable, "-m", "silkcode", "gui", ".", "--port", "9000"]
