"""Project selection for new sessions: shared resolver + recent-projects memory."""

import pytest

from silkcode.project import (
    resolve_project,
    record_recent_project,
    recent_projects,
    parse_github_spec,
    is_github_spec,
)

# ---- shared resolver --------------------------------------------------------


def test_resolve_project_local_path(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("# local\n")
    choice = resolve_project(str(proj))
    assert choice.kind == "local"
    assert choice.workspace.root == proj.resolve()
    assert (choice.workspace.root / "README.md").read_text() == "# local\n"


def test_resolve_project_local_missing(tmp_path):
    with pytest.raises(Exception):
        resolve_project(str(tmp_path / "does-not-exist"))


def test_resolve_project_github_spec_shapes():
    assert parse_github_spec("github:owner/repo").repo == "repo"
    assert parse_github_spec("github:owner/repo").owner == "owner"
    assert is_github_spec("github:acme/widget")
    assert is_github_spec("github:acme/widget") and not is_github_spec("/plain/path")


def test_recent_projects_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    record_recent_project("local", "/tmp/a", "/tmp/a")
    record_recent_project("github", "github:o/r", "github/o/r")
    recents = recent_projects()
    assert recents[0]["spec"] == "github:o/r"  # most recent first
    assert any(r["spec"] == "/tmp/a" for r in recents)


def test_cli_project_command_switches_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.agent import Agent
    from silkcode.cli.repl import _project_command
    from silkcode.permissions import PermissionManager
    from silkcode.workspace import Workspace
    from conftest import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "N.md").write_text("new\n")

    old_root = tmp_path / "old"
    old_root.mkdir()
    ws = Workspace(str(old_root))
    agent = Agent(FakeProvider([]), "fake-model", ws, PermissionManager(mode="edit"))
    session = {"cwd": str(old_root)}

    _project_command(agent, None, session, str(proj))
    assert agent.workspace.root == proj.resolve()
    assert session["cwd"].endswith("proj")
    # system prompt regenerated for the new root
    assert str(proj.resolve()) in agent.messages[0]["content"]

    recent = recent_projects()
    assert recent and recent[0]["kind"] == "local" and recent[0]["spec"].endswith("proj")

