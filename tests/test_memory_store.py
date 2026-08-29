"""The structured memory store.

Memory was an append-only markdown log: nothing superseded anything, and a
changed convention sat forever next to the note it replaced. These tests pin
what the store version must do instead - typed records, refresh-not-duplicate,
supersede-on-restatement - and what it must keep from the old design: a
human-readable file, survival of the old format, and revert really forgetting.
"""

from __future__ import annotations

import sqlite3

import pytest

from silkcode.memory import (
    DB_RELPATH,
    MAX_MEMORY_CHARS,
    MEMORY_RELPATH,
    db_path,
    load_memory,
    memory_path,
    remember,
)
from silkcode.workspace import Workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    return Workspace(root)


# ---- kinds ------------------------------------------------------------------

def test_notes_are_grouped_by_what_kind_of_knowledge_they_are(ws):
    remember(ws, "Prefers small commits", kind="preference")
    remember(ws, "The API layer is in src/api", kind="fact")
    remember(ws, "Run `make test` before pushing", kind="procedure")
    remember(ws, "pytest hangs without -p no:cacheprovider on NFS", kind="failure")

    content = load_memory(ws)
    order = [content.index(h) for h in
             ("Preferences", "Project facts", "Procedures & commands",
              "Known failures & fixes")]
    assert order == sorted(order), "kinds should render in a stable order"
    assert "Prefers small commits" in content
    assert "pytest hangs" in content


def test_an_unknown_kind_degrades_to_fact_rather_than_failing(ws):
    """The model will invent kinds. A bad label must not lose the note."""
    remember(ws, "Uses trunk-based development", kind="strategy")
    assert "Uses trunk-based development" in load_memory(ws)
    assert "Project facts" in load_memory(ws)


# ---- refresh and supersede --------------------------------------------------

def test_the_same_note_twice_is_one_note(ws):
    remember(ws, "We use PostgreSQL")
    message = remember(ws, "We use PostgreSQL")
    assert "Already remembered" in message
    assert load_memory(ws).count("We use PostgreSQL") == 1


def test_a_restatement_supersedes_what_it_restates(ws):
    remember(ws, "Deploy with `make deploy` from the release branch")
    message = remember(ws, "Deploy with `make deploy-v2` from the release branch")
    assert "supersedes 1 earlier note" in message
    content = load_memory(ws)
    assert "make deploy-v2" in content
    assert "`make deploy`" not in content, "the replaced note is still shown"


def test_superseded_records_are_kept_not_destroyed(ws):
    """Marked and hidden, so a mistake is recoverable by a person."""
    remember(ws, "Deploy with `make deploy` from the release branch")
    remember(ws, "Deploy with `make deploy-v2` from the release branch")
    rows = sqlite3.connect(db_path(ws)).execute(
        "SELECT status, superseded_by FROM memory ORDER BY id").fetchall()
    assert rows[0][0] == "superseded" and rows[0][1] is not None
    assert rows[1][0] == "active"


def test_different_knowledge_does_not_supersede(ws):
    remember(ws, "The frontend build uses Vite")
    remember(ws, "The backend is a Flask API")
    content = load_memory(ws)
    assert "Vite" in content and "Flask" in content


# ---- the mirror -------------------------------------------------------------

def test_the_markdown_file_still_exists_and_is_readable(ws):
    """The old design's virtue - a plain file a person can open - survives,
    as a rendering of the store that says what it is."""
    remember(ws, "We use PostgreSQL")
    text = memory_path(ws).read_text()
    assert text.startswith("# Project memory")
    assert "overwritten" in text            # it says it is generated
    assert "We use PostgreSQL" in text


def test_the_mirror_tracks_supersession(ws):
    remember(ws, "Deploy with `make deploy` from the release branch")
    remember(ws, "Deploy with `make deploy-v2` from the release branch")
    text = memory_path(ws).read_text()
    assert "make deploy-v2" in text
    assert "`make deploy`" not in text


# ---- the old format ---------------------------------------------------------

def test_a_hand_written_memory_file_is_imported_once(ws):
    memory_path(ws).parent.mkdir(parents=True)
    memory_path(ws).write_text(
        "- [2026-01-05] arch: monolith\n- [2026-02-10] uses redis for queues\n")
    content = load_memory(ws)
    assert "arch: monolith" in content
    assert "uses redis for queues" in content
    assert "[2026-01-05]" in content, "the original date should survive import"


def test_import_happens_exactly_once(ws):
    memory_path(ws).parent.mkdir(parents=True)
    memory_path(ws).write_text("- [2026-01-05] arch: monolith\n")
    load_memory(ws)
    remember(ws, "new note")            # rewrites the mirror
    load_memory(ws)                     # must not re-import the mirror
    assert load_memory(ws).count("arch: monolith") == 1


def test_the_generated_mirror_is_never_imported(ws):
    remember(ws, "We use PostgreSQL")
    db_path(ws).unlink()                # store gone, mirror left behind
    assert load_memory(ws) == ""        # the mirror is output, not input


# ---- limits -----------------------------------------------------------------

def test_over_the_cap_the_oldest_notes_go_first_and_it_says_so(ws):
    for i in range(200):
        remember(ws, f"note number {i}: " + "x" * 80)
    content = load_memory(ws)
    assert len(content) <= MAX_MEMORY_CHARS
    assert "note number 199" in content, "the newest note must survive"
    assert "note number 0:" not in content
    assert "older memories not shown" in content


def test_empty_workspace_has_no_memory_and_creates_no_files(ws):
    assert load_memory(ws) == ""
    assert not db_path(ws).exists()
    assert not memory_path(ws).exists()


# ---- revert -----------------------------------------------------------------

def test_reverting_the_turn_really_forgets(ws):
    """The checkpoint has to cover the store, not the rendering of it - and
    it has to snapshot bytes, because the store is not text."""
    from silkcode.checkpoints import Checkpoints

    remember(ws, "note from an earlier turn")
    checkpoints = Checkpoints()
    checkpoints.begin()
    checkpoints.snapshot(ws.resolve(DB_RELPATH))
    remember(ws, "note the user rejects")

    checkpoints.revert_last()
    content = load_memory(ws)
    assert "note from an earlier turn" in content
    assert "note the user rejects" not in content


def test_binary_files_survive_a_revert(tmp_path):
    """A checkpoint through read_text(errors='replace') mangles anything that
    is not UTF-8; a reverted image or database came back corrupted."""
    from silkcode.checkpoints import Checkpoints

    target = tmp_path / "blob.bin"
    original = bytes(range(256))
    target.write_bytes(original)
    checkpoints = Checkpoints()
    checkpoints.begin()
    checkpoints.snapshot(target)
    target.write_bytes(b"overwritten")
    checkpoints.revert_last()
    assert target.read_bytes() == original


def test_memory_relpath_still_names_the_markdown_file():
    assert MEMORY_RELPATH.endswith("memory.md")
