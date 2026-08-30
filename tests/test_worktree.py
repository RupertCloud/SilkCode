"""Worktree isolation: the session gets a fork, the checkout stays yours.

`--isolated` forks the repository at HEAD into a throwaway worktree on a
silk/<stamp> branch. The tests care about the promises in both directions:
the session cannot touch the live checkout, and the cleanup cannot destroy
work - commits and uncommitted changes are kept, only litter is removed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from silkcode.worktree import BRANCH_PREFIX, cleanup, create
from silkcode.workspace import ToolError


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    for name, value in (("GIT_AUTHOR_NAME", "T"), ("GIT_AUTHOR_EMAIL", "t@t"),
                        ("GIT_COMMITTER_NAME", "T"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("x = 1\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    return root


# ---- the fork ----------------------------------------------------------------

def test_the_fork_starts_from_head_not_from_the_dirty_checkout(repo):
    (repo / "app.py").write_text("x = 2  # uncommitted\n")
    wt = create(repo)
    assert wt.root.is_dir()
    assert (wt.root / "app.py").read_text() == "x = 1\n", \
        "uncommitted changes leaked into the fork"
    assert wt.branch.startswith(BRANCH_PREFIX)
    assert (repo / "app.py").read_text() == "x = 2  # uncommitted\n"


def test_writes_in_the_worktree_never_touch_the_checkout(repo):
    wt = create(repo)
    (wt.root / "app.py").write_text("x = 99\n")
    (wt.root / "new.py").write_text("fresh\n")
    assert (repo / "app.py").read_text() == "x = 1\n"
    assert not (repo / "new.py").exists()


def test_the_worktree_lives_in_state_not_beside_the_project(repo, tmp_path):
    wt = create(repo)
    assert str(tmp_path / "home") in str(wt.root)
    assert repo not in wt.root.parents


def test_a_subdirectory_forks_the_whole_repository(repo):
    (repo / "src").mkdir()
    _git(repo, "add", "-A")
    wt = create(repo / "src")
    assert (wt.root / "app.py").is_file()


# ---- refusal first -----------------------------------------------------------

def test_outside_a_repository_is_a_refusal_not_a_silent_live_mount(tmp_path,
                                                                   monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ToolError) as caught:
        create(plain)
    assert "not inside one" in str(caught.value)


def test_a_repository_with_no_commits_is_refused_with_the_reason(tmp_path,
                                                                 monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    with pytest.raises(ToolError) as caught:
        create(empty)
    assert "initial commit" in str(caught.value)


# ---- cleanup errs toward keeping work ----------------------------------------

def test_a_clean_unused_fork_is_removed_branch_and_all(repo):
    wt = create(repo)
    message = cleanup(wt)
    assert "removed" in message
    assert not wt.root.exists()
    assert BRANCH_PREFIX not in _git(repo, "branch", "--list", wt.branch).stdout


def test_commits_keep_the_worktree_and_say_how_to_merge(repo):
    wt = create(repo)
    (wt.root / "feature.py").write_text("done\n")
    _git(wt.root, "add", "feature.py")
    _git(wt.root, "commit", "-qm", "add feature")
    message = cleanup(wt)
    assert "kept" in message and "1 commit" in message
    assert f"git merge {wt.branch}" in message
    assert wt.root.exists()
    assert wt.branch in _git(repo, "branch", "--list", wt.branch).stdout


def test_uncommitted_changes_keep_the_worktree(repo):
    """Removing a dirty worktree destroys work; litter is the only thing
    cleanup may delete."""
    wt = create(repo)
    (wt.root / "app.py").write_text("half-finished\n")
    message = cleanup(wt)
    assert "kept" in message and "uncommitted changes" in message
    assert wt.root.exists()


def test_the_checkout_is_untouched_across_the_whole_lifecycle(repo):
    before = (repo / "app.py").read_text()
    wt = create(repo)
    (wt.root / "app.py").write_text("changed in fork\n")
    _git(wt.root, "commit", "-aqm", "fork work")
    cleanup(wt)
    assert (repo / "app.py").read_text() == before
    assert "fork work" not in _git(repo, "log", "--oneline", "-1").stdout


# ---- through the CLI ---------------------------------------------------------

def test_an_isolated_oneshot_run_writes_in_the_fork_not_the_checkout(
        repo, tmp_path, stub_server, monkeypatch):
    from conftest import sse_response
    from silkcode.cli.repl import run_repl

    scripted = [
        sse_response(tool_calls=[("write_file", json.dumps(
            {"path": "generated.py", "content": "made by the agent\n"}))],
            usage={"prompt_tokens": 5, "completion_tokens": 5}),
        sse_response(content="Done.", usage={"prompt_tokens": 5, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        (tmp_path / "home" / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        rc = run_repl(str(repo), None, "agent", prompt="make generated.py",
                      isolated=True)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert rc == 0
    assert not (repo / "generated.py").exists(), "the agent wrote into the checkout"
    worktrees = list((tmp_path / "home" / "worktrees").iterdir())
    assert worktrees, "the dirty worktree should have been kept"
    assert (worktrees[0] / "generated.py").read_text() == "made by the agent\n"


def test_isolated_outside_a_repository_exits_with_the_explanation(
        tmp_path, monkeypatch, capsys):
    from silkcode.cli.repl import run_repl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": "http://127.0.0.1:1",
                                "default_model": "m"}},
    }))
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = run_repl(str(plain), None, "agent", prompt="anything", isolated=True)
    assert rc == 1
    assert "not inside one" in capsys.readouterr().err
