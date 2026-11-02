"""HIGH-2: the automatic context loaders must not follow symlinks out of the
workspace.

`SILKCODE.md`, `.silkcode/memory.md`, project skills and the repo-map walk are
read without a tool call, so a repository-supplied symlink that points at
`~/.ssh/id_rsa` would put that file straight into the system prompt: no
permission check, no provenance record, no redaction. `Workspace.resolve`
already refuses such paths for every tool; these loaders used plain
`Path.read_text` and walked around it.

A refused path is treated as absent - there is nothing to load and nothing to
warn the user about, because the file was never part of the repository.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from silkcode.context import INSTRUCTIONS_FILE, assemble, project_sources
from silkcode.memory import load_memory, remember
from silkcode.repomap import repo_map
from silkcode.skills import load_skills, use_skill
from silkcode.workspace import ToolError, Workspace

SECRET = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
    "OUTSIDE-WORKSPACE-SECRET\n"
)
SECRET_MARKER = "OUTSIDE-WORKSPACE-SECRET"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A repository, plus a file outside it that the repository can link to."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "id_rsa"
    secret.write_text(SECRET)
    repo = tmp_path / "repo"
    (repo / ".silkcode" / "skills").mkdir(parents=True)
    (repo / "app.py").write_text("x = 1\n")
    return SimpleNamespace(repo=repo, outside=outside, secret=secret, home=home)


# ---- instructions ----------------------------------------------------------

def test_symlinked_instructions_are_treated_as_absent(layout):
    (layout.repo / INSTRUCTIONS_FILE).symlink_to(layout.secret)
    ws = Workspace(layout.repo)

    with pytest.raises(ToolError):
        ws.resolve(INSTRUCTIONS_FILE)  # the resolver refuses what the loader must not read

    result = assemble(ws)
    assert SECRET_MARKER not in result.text
    assert INSTRUCTIONS_FILE not in [label for label, _ in project_sources(ws)]
    # Absent, not "withheld": a file that cannot be loaded has nothing to scan.
    assert result.withheld == [] and result.warnings == []


# ---- memory ----------------------------------------------------------------

def test_symlinked_memory_mirror_is_treated_as_absent(layout):
    (layout.repo / ".silkcode" / "memory.md").symlink_to(layout.secret)
    ws = Workspace(layout.repo)

    assert load_memory(ws) == ""
    text = assemble(ws).text
    assert SECRET_MARKER not in text
    assert ".silkcode/memory.md" not in text


def test_symlinked_memory_store_is_not_opened(layout):
    """The SQLite store is a file too, and a link out of the repository must
    not be opened - let alone written - through."""
    store = layout.outside / "memory.db"
    conn = sqlite3.connect(store)
    conn.execute(
        "CREATE TABLE memory (id INTEGER PRIMARY KEY, kind TEXT, text TEXT,"
        " created TEXT, updated TEXT, status TEXT, superseded_by INTEGER)")
    conn.execute(
        "INSERT INTO memory (kind, text, created, updated, status)"
        " VALUES ('fact', 'STORE-SECRET', '2026-01-01', '2026-01-01', 'active')")
    conn.commit()
    conn.close()
    (layout.repo / ".silkcode" / "memory.db").symlink_to(store)

    assert load_memory(Workspace(layout.repo)) == ""


def test_remember_does_not_write_through_a_symlinked_mirror(layout):
    (layout.repo / ".silkcode" / "memory.md").symlink_to(layout.secret)
    ws = Workspace(layout.repo)

    remember(ws, "uses PostgreSQL")

    assert layout.secret.read_text() == SECRET, "the mirror was written outside the workspace"
    assert "uses PostgreSQL" in load_memory(ws)  # the store itself still works


# ---- skills ----------------------------------------------------------------

def test_symlinked_project_skill_is_treated_as_absent(layout):
    (layout.repo / ".silkcode" / "skills" / "leak.md").symlink_to(layout.secret)
    ws = Workspace(layout.repo)

    assert "leak" not in load_skills(ws)
    assert SECRET_MARKER not in assemble(ws).text
    with pytest.raises(ToolError, match="Unknown skill"):
        use_skill(ws, "leak")


# ---- the ordinary cases still work ----------------------------------------

def test_ordinary_project_files_still_load(layout):
    (layout.repo / INSTRUCTIONS_FILE).write_text("# Conventions\n\nUse four spaces.\n")
    (layout.repo / ".silkcode" / "memory.md").write_text("- [2026-01-05] arch: monolith\n")
    (layout.repo / ".silkcode" / "skills" / "sec.md").write_text(
        "---\nname: security-reviewer\ndescription: Security review checklist\n---\n"
        "Check it.\n")
    home_skill = layout.home / "skills"
    home_skill.mkdir()
    (home_skill / "tester.md").write_text("- always add regression tests\n")

    ws = Workspace(layout.repo)
    result = assemble(ws)

    assert result.warnings == [] and result.withheld == []
    assert "Use four spaces." in result.text
    assert "arch: monolith" in result.text
    assert "security-reviewer: Security review checklist" in result.text
    assert "Check it." in use_skill(ws, "security-reviewer")
    # a user-level skill is on this machine, not in the repository: unchanged
    assert "always add regression tests" in use_skill(ws, "tester")


def test_instructions_that_link_inside_the_workspace_still_load(layout):
    """Only links that leave the workspace are refused; an ordinary in-repo
    link still resolves, reads and loads."""
    (layout.repo / "docs").mkdir()
    (layout.repo / "docs" / "conventions.md").write_text("# Conventions\n\nUse tabs.\n")
    (layout.repo / INSTRUCTIONS_FILE).symlink_to(layout.repo / "docs" / "conventions.md")

    result = assemble(Workspace(layout.repo))
    assert "Use tabs." in result.text
    assert result.warnings == [] and result.withheld == []


# ---- repo map --------------------------------------------------------------

def test_repo_map_does_not_follow_escaping_symlinks(layout):
    private = layout.outside / "private"
    private.mkdir()
    (private / "secret-module.py").write_text("")
    (layout.outside / "notes.txt").write_text("")
    # a directory link would let the walk enumerate (and count) files outside
    (layout.repo / "vendor").symlink_to(private)
    # a file link would put an outside name in the map as if it were ours
    (layout.repo / "linked.py").symlink_to(layout.outside / "notes.txt")

    text = repo_map(Workspace(layout.repo))

    assert "secret-module.py" not in text
    assert "vendor" not in text
    assert "linked.py" not in text
    assert "app.py" in text  # ordinary entries are untouched
