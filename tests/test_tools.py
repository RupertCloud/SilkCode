import pytest

from silkcode.tools.files import edit_file, read_file, write_file
from silkcode.tools.search import glob_files, grep
from silkcode.tools.shell import run_command
from silkcode.tools.images import IMAGE_MARKER, show_image
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


# ---- optimistic concurrency (stale-base check) -----------------------------

def test_edit_stale_base_detected(ws):
    """A file changed on disk since read_file -> edit refuses, tells to re-read."""
    write_file(ws, "a.py", "x = 1\ny = 2\n")
    read_file(ws, "a.py")
    # another agent/editor changes the file behind our back
    (ws.root / "a.py").write_text("x = 100\ny = 2\n")
    with pytest.raises(ToolError, match="changed on disk"):
        edit_file(ws, "a.py", "y = 2", "y = 3")


def test_write_stale_base_detected(ws):
    write_file(ws, "a.py", "x = 1\n")
    read_file(ws, "a.py")
    (ws.root / "a.py").write_text("x = 999\n")
    with pytest.raises(ToolError, match="changed on disk"):
        write_file(ws, "a.py", "x = 2\n")


def test_stale_base_cleared_by_reread(ws):
    """Re-reading the file refreshes the fingerprint, so the retry succeeds."""
    write_file(ws, "a.py", "x = 1\ny = 2\n")
    read_file(ws, "a.py")
    (ws.root / "a.py").write_text("x = 100\ny = 2\n")
    read_file(ws, "a.py")  # agent follows the error's advice: re-read
    edit_file(ws, "a.py", "y = 2", "y = 3")
    assert "y = 3" in read_file(ws, "a.py")


def test_sequential_edits_ok(ws):
    """Each successful edit refreshes the fingerprint, so edit chains work."""
    write_file(ws, "a.py", "x = 1\ny = 2\n")
    read_file(ws, "a.py")
    edit_file(ws, "a.py", "x = 1", "x = 2")
    edit_file(ws, "a.py", "y = 2", "y = 3")
    assert "x = 2" in read_file(ws, "a.py") and "y = 3" in read_file(ws, "a.py")


def test_edit_without_prior_read_ok(ws):
    """No recorded fingerprint (file never read) -> no stale check, write allowed."""
    (ws.root / "a.py").write_text("x = 1\n")  # created by another process
    edit_file(ws, "a.py", "x = 1", "x = 2")
    assert "x = 2" in read_file(ws, "a.py")


def test_edit_deleted_since_read_detected(ws):
    write_file(ws, "a.py", "x = 1\n")
    read_file(ws, "a.py")
    (ws.root / "a.py").unlink()  # vanished since we read it
    with pytest.raises(ToolError, match="changed on disk"):
        edit_file(ws, "a.py", "x = 1", "x = 2")


def test_two_sessions_same_file(ws):
    """The scenario this exists for: session B edits after session A already did."""
    session_a, session_b = {}, {}  # each session owns a fingerprint registry
    write_file(ws, "a.py", "x = 1\n", _registry=session_a)
    read_file(ws, "a.py", _registry=session_b)              # session B reads the original
    write_file(ws, "a.py", "x = 2\n", _registry=session_a)  # session A edits first
    with pytest.raises(ToolError, match="changed on disk"):
        edit_file(ws, "a.py", "x = 1", "x = 3", _registry=session_b)  # B's edit is stale


def test_own_write_does_not_mask_another_sessions_stale_base(ws):
    """A's write refreshes A's registry but must NOT clear B's stale base."""
    session_a, session_b = {}, {}
    write_file(ws, "a.py", "x = 1\n", _registry=session_a)
    read_file(ws, "a.py", _registry=session_b)              # B reads v1
    write_file(ws, "a.py", "x = 2\n", _registry=session_a)  # A writes v2
    # A can keep editing v2 (its own base is current)...
    edit_file(ws, "a.py", "x = 2", "x = 3", _registry=session_a)
    # ...but B is still told to re-read before touching the file.
    with pytest.raises(ToolError, match="changed on disk"):
        edit_file(ws, "a.py", "x = 1", "x = 9", _registry=session_b)


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


def test_show_image_validates_workspace_image(ws):
    image = ws.root / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert show_image(ws, "shot.png") == IMAGE_MARKER + "shot.png"
    with pytest.raises(ToolError):
        show_image(ws, "../shot.png")
    (ws.root / "notes.txt").write_text("not an image")
    with pytest.raises(ToolError, match="Only PNG"):
        show_image(ws, "notes.txt")


# ---- live preview server (the Python-native live-server) ---------------------

def test_live_server_start_serves_and_injects_reload(ws):
    import urllib.request
    import silkcode.liveserver as L

    write_file(ws, "index.html", "<html><body>hello</body></html>")
    try:
        out = L.live_server(ws, "start", port=0)
        assert "live preview server running at" in out
        srv = L.REGISTRY.get(ws)
        assert srv is not None
        body = urllib.request.urlopen(srv.url + "index.html", timeout=5).read()
        assert b"hello" in body                    # the page is served
        assert b"silk-reload" in body              # the reload meta is injected
        assert b"location.reload()" in body        # and the reload client script
    finally:
        L.live_server(ws, "stop")
    assert "no live preview server is running" in L.live_server(ws, "status")


def test_live_server_status_and_stop(ws):
    import silkcode.liveserver as L
    write_file(ws, "index.html", "<html><body>x</body></html>")
    assert "no live preview server is running" in L.live_server(ws, "status")
    L.live_server(ws, "start", port=0)
    assert "already running" in L.live_server(ws, "start", port=0)
    assert "running at" in L.live_server(ws, "status")
    assert "stopped" in L.live_server(ws, "stop")


def test_live_server_unknown_action(ws):
    import silkcode.liveserver as L
    with pytest.raises(ToolError, match="unknown live_server action"):
        L.live_server(ws, "boom")


def test_live_server_watcher_detects_changes(ws):
    import time
    import silkcode.liveserver as L
    write_file(ws, "index.html", "<html><body>v1</body></html>")
    try:
        L.live_server(ws, "start", port=0)
        srv = L.REGISTRY.get(ws)
        before = srv._reload_count
        time.sleep(0.05)
        write_file(ws, "index.html", "<html><body>v2 changed</body></html>")
        time.sleep(srv.interval + 0.4)
        assert srv._reload_count > before
    finally:
        L.live_server(ws, "stop")


def test_live_server_blocks_path_escape(ws):
    import urllib.request
    import urllib.error
    import silkcode.liveserver as L
    write_file(ws, "index.html", "<html><body>x</body></html>")
    try:
        L.live_server(ws, "start", port=0)
        srv = L.REGISTRY.get(ws)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(srv.url + "../" + "pyproject.toml", timeout=5)
        assert exc.value.code == 403
    finally:
        L.live_server(ws, "stop")
