import pytest

from silkcode.tools.files import edit_file, read_file, write_file
from silkcode.tools.search import glob_files, grep
from silkcode.tools.shell import run_command
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


def test_write_and_read(ws):
    write_file(ws, "src/app.py", "print('hi')\nprint('bye')\n")
    out = read_file(ws, "src/app.py")
    assert "1\tprint('hi')" in out
    assert "2\tprint('bye')" in out


def test_read_offset_limit(ws):
    write_file(ws, "big.txt", "\n".join(f"line{i}" for i in range(1, 101)))
    out = read_file(ws, "big.txt", offset=50, limit=2)
    assert "50\tline50" in out and "51\tline51" in out
    assert "line52" not in out
    assert "more lines" in out


def test_read_missing_file(ws):
    with pytest.raises(ToolError):
        read_file(ws, "nope.txt")


def test_edit_unique(ws):
    write_file(ws, "a.py", "x = 1\ny = 2\n")
    edit_file(ws, "a.py", "y = 2", "y = 3")
    assert "y = 3" in read_file(ws, "a.py")


def test_edit_requires_unique_match(ws):
    write_file(ws, "a.py", "x = 1\nx = 1\n")
    with pytest.raises(ToolError):
        edit_file(ws, "a.py", "x = 1", "x = 2")
    edit_file(ws, "a.py", "x = 1", "x = 2", replace_all=True)
    assert read_file(ws, "a.py").count("x = 2") == 2


def test_edit_missing_string(ws):
    write_file(ws, "a.py", "x = 1\n")
    with pytest.raises(ToolError):
        edit_file(ws, "a.py", "zzz", "yyy")


def test_glob(ws):
    write_file(ws, "src/a.py", "")
    write_file(ws, "src/deep/b.py", "")
    write_file(ws, "readme.md", "")
    out = glob_files(ws, "**/*.py")
    assert "src/a.py" in out and "src/deep/b.py" in out
    assert "readme.md" not in out


def test_grep(ws):
    write_file(ws, "src/a.py", "def alpha():\n    return 1\n")
    write_file(ws, "src/b.py", "def beta():\n    return 2\n")
    out = grep(ws, r"def \w+", glob="**/*.py")
    assert "src/a.py:1:def alpha():" in out
    assert "src/b.py:1:def beta():" in out


def test_workspace_escape_blocked(ws):
    with pytest.raises(ToolError):
        ws.resolve("../outside.txt")
    with pytest.raises(ToolError):
        write_file(ws, "/etc/hosts", "nope")


def test_run_command(ws):
    out = run_command(ws, "echo hello")
    assert "exit code: 0" in out
    assert "hello" in out


def test_run_command_failure(ws):
    out = run_command(ws, "false")
    assert "exit code: 1" in out
