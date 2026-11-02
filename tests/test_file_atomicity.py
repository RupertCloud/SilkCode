"""File-tool writes are atomic and preserve line endings (LOW-10).

write_file/edit_file used to truncate the target in place: a crash or ENOSPC
mid-write lost the whole file. They also read and wrote through the
universal-newline translator, silently turning a CRLF file into LF.
"""

from __future__ import annotations

import os

import pytest

from silkcode.tools.files import edit_file, read_file, write_file
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path))


# ---- line endings ------------------------------------------------------------

def test_edit_preserves_crlf_line_endings(ws, tmp_path):
    (tmp_path / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    edit_file(ws, "crlf.txt", "beta", "BETA")
    assert (tmp_path / "crlf.txt").read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_edit_matches_lf_old_string_against_a_crlf_file(ws, tmp_path):
    """read_file shows lines without their endings, so the model passes LF
    newlines; the edit must still match, and stay CRLF."""
    (tmp_path / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    edit_file(ws, "crlf.txt", "one\ntwo", "1\n2")
    assert (tmp_path / "crlf.txt").read_bytes() == b"1\r\n2\r\nthree\r\n"


def test_write_file_does_not_translate_line_endings(ws, tmp_path):
    write_file(ws, "crlf.txt", "a\r\nb\r\n")
    assert (tmp_path / "crlf.txt").read_bytes() == b"a\r\nb\r\n"


def test_a_crlf_read_then_edit_is_not_reported_as_stale(ws, tmp_path):
    (tmp_path / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    read_file(ws, "crlf.txt")
    edit_file(ws, "crlf.txt", "a", "A")  # same registry: base still matches
    assert (tmp_path / "crlf.txt").read_bytes() == b"A\r\nb\r\n"


# ---- atomicity ---------------------------------------------------------------

def _no_space(src, dst):
    raise OSError(28, "No space left on device")


def test_write_file_keeps_the_old_file_when_the_swap_fails(ws, tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_text("original\n")
    monkeypatch.setattr(os, "replace", _no_space)
    with pytest.raises(OSError, match="No space left"):
        write_file(ws, "keep.txt", "replacement that must not land\n")
    monkeypatch.undo()
    assert target.read_text() == "original\n"
    # the temp file was cleaned up, not left in the user's tree
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_write_file_keeps_the_old_file_when_the_write_fails(ws, tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_text("original\n")

    def no_space(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_space)  # fails before the swap
    with pytest.raises(OSError, match="No space left"):
        write_file(ws, "keep.txt", "replacement that must not land\n")
    monkeypatch.undo()
    assert target.read_text() == "original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_edit_file_keeps_the_old_file_when_the_swap_fails(ws, tmp_path, monkeypatch):
    target = tmp_path / "keep.py"
    target.write_text("x = 1\n")
    monkeypatch.setattr(os, "replace", _no_space)
    with pytest.raises(OSError, match="No space left"):
        edit_file(ws, "keep.py", "x = 1", "x = 2")
    monkeypatch.undo()
    assert target.read_text() == "x = 1\n"


def test_atomic_write_keeps_the_file_mode(ws, tmp_path):
    """Swapping in a temp file must not turn an executable script into a
    plain 0o644 file."""
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\necho hi\n")
    target.chmod(0o755)
    write_file(ws, "run.sh", "#!/bin/sh\necho bye\n")
    assert os.stat(target).st_mode & 0o777 == 0o755


def test_edit_without_a_match_reports_the_same_error(ws, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("hello\n")
    with pytest.raises(ToolError, match="old_string not found"):
        edit_file(ws, "keep.txt", "nope", "yes")
    assert target.read_text() == "hello\n"
