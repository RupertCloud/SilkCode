"""Choosing which project a session points at.

The picker decides which working tree the agent then edits, commits and
pushes. A mistake here does not look like a crash - it looks like the agent
quietly working on the wrong repository - so these tests pin down the
identity of the resolved workspace, not just that something resolved.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from silkcode.project import (
    ProjectChoice,
    available_projects,
    clone_github_repo,
    is_github_spec,
    local_path_for_github,
    parse_github_spec,
    prompt_for_project,
    projects_dir,
    recent_projects,
    record_recent_project,
    resolve_project,
)
from silkcode.workspace import ToolError


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(d))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return d


# --------------------------------------------------------------------------
# spec parsing
# --------------------------------------------------------------------------

def test_recognizes_github_specs():
    for spec in ("github:owner/repo", "github/owner/repo",
                 "github:my-org/my.repo_name"):
        assert is_github_spec(spec), spec
    for spec in ("/local/path", "./rel", "owner/repo", "github:onlyowner", ""):
        assert not is_github_spec(spec), spec


def test_parses_owner_and_repo():
    parsed = parse_github_spec("github:facebook/react")
    assert (parsed.owner, parsed.repo) == ("facebook", "react")


def test_rejects_a_non_github_spec():
    with pytest.raises(ValueError):
        parse_github_spec("/some/path")


# --------------------------------------------------------------------------
# local clone location  (a mix-up here means editing the wrong repository)
# --------------------------------------------------------------------------

def test_different_repositories_never_share_a_directory(home):
    """'-' is legal in both an owner and a repo name, so joining them with
    one is ambiguous: facebook/react-native and facebook-react/native read
    back identically. clone_github_repo reuses any directory that already
    has a .git, so a collision hands back the wrong working tree under the
    right label - and a push lands in the wrong repository."""
    a = local_path_for_github("facebook", "react-native")
    b = local_path_for_github("facebook-react", "native")
    assert a != b


def test_the_same_repository_always_maps_to_one_directory(home):
    assert local_path_for_github("o", "r") == local_path_for_github("o", "r")


def test_the_directory_stays_inside_the_projects_dir(home):
    """Owner and repo come off the wire; neither may walk out of the tree."""
    for owner, repo in ((".", "."), ("..", ".."), ("..", "repo"), ("owner", "..")):
        path = local_path_for_github(owner, repo).resolve()
        assert projects_dir().resolve() in path.parents, (owner, repo)


def test_the_directory_name_is_still_recognizable(home):
    """A digest keeps them apart, but a human should still see which is which."""
    assert local_path_for_github("facebook", "react").name.startswith("facebook-react-")


# --------------------------------------------------------------------------
# cloning
# --------------------------------------------------------------------------

def test_clone_uses_the_configured_github_credentials(home, monkeypatch):
    """The picker lists whatever the token can see, private repos included,
    so a repo it offered has to be one we can actually clone."""
    seen = {}

    monkeypatch.setattr("silkcode.github.git_credential_env",
                        lambda: {"GIT_ASKPASS": "/helper", "SILKCODE_GIT_TOKEN": "tok"})

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs.get("env") or {}
        (kwargs.get("cwd") or None)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("silkcode.project.subprocess.run", fake_run)
    # Workspace() requires the directory to exist
    monkeypatch.setattr("silkcode.project.Workspace", lambda p: p)

    clone_github_repo("acme", "private-thing")
    assert seen["args"][:2] == ["git", "clone"]
    assert seen["env"].get("SILKCODE_GIT_TOKEN") == "tok", \
        "clone ran without the configured credentials"


def test_fetch_of_an_existing_clone_also_gets_credentials(home, monkeypatch):
    """Updating a private repo needs the token just as much as cloning it."""
    dest = local_path_for_github("acme", "private-thing")
    (dest / ".git").mkdir(parents=True)
    seen = {}

    monkeypatch.setattr("silkcode.github.git_credential_env",
                        lambda: {"SILKCODE_GIT_TOKEN": "tok"})

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("silkcode.project.subprocess.run", fake_run)
    monkeypatch.setattr("silkcode.project.Workspace", lambda p: p)

    clone_github_repo("acme", "private-thing")
    assert "fetch" in seen["args"]
    assert seen["env"].get("SILKCODE_GIT_TOKEN") == "tok", \
        "fetch ran without the configured credentials"


def test_a_failed_clone_reports_the_reason(home, monkeypatch):
    monkeypatch.setattr("silkcode.github.git_credential_env", lambda: None)
    monkeypatch.setattr(
        "silkcode.project.subprocess.run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 128, "", "fatal: repository not found\n"))
    with pytest.raises(ToolError, match="repository not found"):
        clone_github_repo("acme", "nope")


# clone_github_repo always targets github.com, so an end-to-end clone would
# need the network. The offline half of that path - that a token reaches git
# without ever entering .git/config - is covered in test_security.py against
# a local origin repository.


# --------------------------------------------------------------------------
# resolving a spec to a workspace
# --------------------------------------------------------------------------

def test_resolve_a_local_directory(home, tmp_path):
    project = tmp_path / "myproject"
    project.mkdir()
    choice = resolve_project(str(project))
    assert choice.kind == "local"
    assert choice.workspace.root == project.resolve()


def test_resolve_rejects_an_empty_spec(home):
    with pytest.raises(ToolError, match="empty"):
        resolve_project("   ")


def test_resolve_reports_a_missing_directory(home, tmp_path):
    with pytest.raises(ToolError):
        resolve_project(str(tmp_path / "does-not-exist"))


def test_resolve_a_github_spec_clones_it(home, monkeypatch, tmp_path):
    target = tmp_path / "cloned"
    target.mkdir()
    monkeypatch.setattr("silkcode.project.clone_github_repo",
                        lambda o, r, token=None: __import__(
                            "silkcode.workspace", fromlist=["Workspace"]).Workspace(str(target)))
    choice = resolve_project("github:acme/widget")
    assert choice.kind == "github"
    assert choice.label == "github/acme/widget"
    assert choice.github.owner == "acme" and choice.github.repo == "widget"


# --------------------------------------------------------------------------
# recent projects
# --------------------------------------------------------------------------

def test_no_recents_on_a_fresh_install(home):
    assert recent_projects() == []


def test_recents_are_most_recent_first(home):
    record_recent_project("local", "/a", "A")
    record_recent_project("local", "/b", "B")
    assert [r["spec"] for r in recent_projects()] == ["/b", "/a"]


def test_reopening_a_project_does_not_duplicate_it(home):
    record_recent_project("local", "/a", "A")
    record_recent_project("local", "/b", "B")
    record_recent_project("local", "/a", "A")
    assert [r["spec"] for r in recent_projects()] == ["/a", "/b"]


def test_recents_are_capped(home):
    for i in range(20):
        record_recent_project("local", f"/p{i}", f"P{i}")
    assert len(recent_projects()) <= 8


def test_a_corrupt_recents_file_is_ignored(home):
    from silkcode.project import recent_projects_path
    recent_projects_path().write_text("{ not json")
    assert recent_projects() == []


def test_malformed_recent_entries_are_dropped(home):
    from silkcode.project import recent_projects_path
    recent_projects_path().write_text(json.dumps(
        [{"kind": "local", "spec": "/ok", "label": "OK"}, {"spec": "/incomplete"}, None]))
    assert [r["spec"] for r in recent_projects()] == ["/ok"]


# --------------------------------------------------------------------------
# the candidate list the picker offers
# --------------------------------------------------------------------------

def test_available_projects_merges_github_and_recents(home, monkeypatch):
    monkeypatch.setattr("silkcode.github.list_github_repos",
                        lambda: [{"full_name": "acme/widget", "description": "d"}])
    record_recent_project("local", "/work/thing", "thing")
    items = available_projects()
    assert {"github:acme/widget", "/work/thing"} == {i["spec"] for i in items}
    assert items[0]["kind"] == "github", "GitHub repos come first"


def test_a_recent_github_repo_is_not_listed_twice(home, monkeypatch):
    monkeypatch.setattr("silkcode.github.list_github_repos",
                        lambda: [{"full_name": "acme/widget", "description": ""}])
    record_recent_project("github", "github:acme/widget", "github/acme/widget")
    specs = [i["spec"] for i in available_projects()]
    assert specs.count("github:acme/widget") == 1


def test_available_projects_without_a_github_token(home, monkeypatch):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])
    record_recent_project("local", "/work/thing", "thing")
    assert [i["spec"] for i in available_projects()] == ["/work/thing"]


# --------------------------------------------------------------------------
# the interactive picker
# --------------------------------------------------------------------------

def test_picker_cancels_on_q(home, monkeypatch, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])
    with pytest.raises(ToolError, match="cancelled"):
        prompt_for_project(asker=lambda p: "q")


def test_picker_cancels_on_end_of_input(home, monkeypatch, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])

    def eof(prompt):
        raise EOFError

    with pytest.raises(ToolError, match="cancelled"):
        prompt_for_project(asker=eof)


def test_picker_opens_a_typed_local_path(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])
    project = tmp_path / "typed"
    project.mkdir()
    choice = prompt_for_project(asker=lambda p: str(project))
    assert choice.kind == "local"
    assert choice.workspace.root == project.resolve()


def test_picker_number_selects_the_listed_repo(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos",
                        lambda: [{"full_name": "acme/first", "description": ""},
                                 {"full_name": "acme/second", "description": ""}])
    cloned = tmp_path / "c"
    cloned.mkdir()
    from silkcode.workspace import Workspace
    seen = {}

    def fake_clone(owner, repo, token=None):
        seen["repo"] = f"{owner}/{repo}"
        return Workspace(str(cloned))

    monkeypatch.setattr("silkcode.project.clone_github_repo", fake_clone)
    choice = prompt_for_project(asker=lambda p: "2")
    assert seen["repo"] == "acme/second", "picked the wrong repository"
    assert choice.label == "github/acme/second"


def test_picker_reprompts_on_a_bad_number(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos",
                        lambda: [{"full_name": "acme/only", "description": ""}])
    answers = iter(["99", "q"])
    with pytest.raises(ToolError, match="cancelled"):
        prompt_for_project(asker=lambda p: next(answers))
    assert "no such number" in capsys.readouterr().out


def test_picker_reprompts_when_a_path_does_not_exist(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])
    answers = iter([str(tmp_path / "nope"), "q"])
    with pytest.raises(ToolError, match="cancelled"):
        prompt_for_project(asker=lambda p: next(answers))


# --------------------------------------------------------------------------
# upgrading from the pre-digest layout
# --------------------------------------------------------------------------

def _make_clone(path, origin_url):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin_url],
                   check=True)
    (path / "work-in-progress.txt").write_text("uncommitted\n")


def test_an_existing_checkout_is_adopted_not_abandoned(home, monkeypatch):
    """Adding the digest moves where clones live. A user mid-change should
    keep their checkout rather than silently get a fresh clone."""
    from silkcode.project import _legacy_path_for_github
    legacy = _legacy_path_for_github("acme", "widget")
    _make_clone(legacy, "https://github.com/acme/widget.git")

    monkeypatch.setattr("silkcode.github.git_credential_env", lambda: None)
    ran = []
    real_run = subprocess.run

    def fake_run(args, **kw):
        # the origin lookup has to be real; only network calls are stubbed
        if "remote" in args:
            return real_run(args, **kw)
        ran.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("silkcode.project.subprocess.run", fake_run)
    monkeypatch.setattr("silkcode.project.Workspace", lambda p: p)

    clone_github_repo("acme", "widget")
    dest = local_path_for_github("acme", "widget")
    assert (dest / "work-in-progress.txt").read_text() == "uncommitted\n"
    assert not legacy.exists()
    assert not any("clone" in a for a in ran), "should not re-clone an adopted checkout"


def test_a_colliding_old_directory_is_not_adopted(home, monkeypatch):
    """The old name was ambiguous, so a directory bearing it may belong to a
    different repository. Adopting it would be the exact mix-up the digest
    prevents."""
    from silkcode.project import _legacy_path_for_github
    # facebook/react-native and facebook-react/native shared this name
    legacy = _legacy_path_for_github("facebook-react", "native")
    _make_clone(legacy, "https://github.com/facebook/react-native.git")

    monkeypatch.setattr("silkcode.github.git_credential_env", lambda: None)
    ran = []
    real_run = subprocess.run

    def fake_run(args, **kw):
        if "remote" in args:
            return real_run(args, **kw)
        if "clone" in args:
            ran.append(args)
            local_path_for_github("facebook-react", "native").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("silkcode.project.subprocess.run", fake_run)
    monkeypatch.setattr("silkcode.project.Workspace", lambda p: p)

    clone_github_repo("facebook-react", "native")
    assert legacy.exists(), "the other repository's checkout must be left alone"
    assert ran, "a fresh clone should have been made instead"
