"""Remote sandbox: protocol server + RemoteBackend, end to end over HTTP."""

from pathlib import Path
import threading

import httpx
import pytest

from silkcode.execbackend import LocalBackend, RemoteBackend, remote_backend_from_config, workspace_tar
from silkcode.sandbox_server import make_server
from silkcode.tools.shell import run_command
from silkcode.workspace import ToolError, Workspace

TOKEN = "test-secret"


@pytest.fixture
def sandbox(tmp_path):
    server = make_server(tmp_path / "sandbox-data", TOKEN, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "hello.py").write_text("print('from the sandbox')\n")
    (root / "data.txt").write_text("42\n")
    return Workspace(root)


def test_health_and_auth(sandbox):
    backend = RemoteBackend(sandbox, TOKEN)
    assert backend.health()["ok"] is True

    with pytest.raises(ToolError, match="health check failed"):
        RemoteBackend(sandbox, "wrong-token").health()


def test_sync_and_exec(sandbox, ws):
    backend = RemoteBackend(sandbox, TOKEN)
    out = backend.exec(ws, "python3 hello.py")
    assert out.startswith("[sandbox] exit code: 0")
    assert "from the sandbox" in out

    out = backend.exec(ws, "cat data.txt")
    assert "42" in out

    out = backend.exec(ws, "false")
    assert "[sandbox] exit code: 1" in out


def test_edits_are_resynced(sandbox, ws):
    backend = RemoteBackend(sandbox, TOKEN)
    assert "42" in backend.exec(ws, "cat data.txt")
    (ws.root / "data.txt").write_text("43\n")
    assert "43" in backend.exec(ws, "cat data.txt")


def test_sync_skipped_when_unchanged(sandbox, ws):
    backend = RemoteBackend(sandbox, TOKEN)
    backend.exec(ws, "true")
    first_manifest = backend._last_manifest
    backend.exec(ws, "true")
    assert backend._last_manifest is first_manifest  # no re-sync happened


def test_exec_without_sync_conflicts(sandbox, ws):
    client = httpx.Client()
    resp = client.post(f"{sandbox}/exec/{'a' * 16}", json={"command": "ls"},
                       headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 409


def test_tar_rejects_traversal(tmp_path):
    import io
    import tarfile
    from silkcode.sandbox_server import safe_extract

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        data = b"nope"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="unsafe path"):
        safe_extract(buffer.getvalue(), tmp_path / "out")


def test_workspace_tar_skips_ignored_dirs(ws):
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "big.js").write_text("x" * 1000)
    import io
    import tarfile
    names = tarfile.open(fileobj=io.BytesIO(workspace_tar(ws)), mode="r:gz").getnames()
    assert "hello.py" in names
    assert not any("node_modules" in n for n in names)


def test_run_command_dispatches_to_backend(ws, sandbox):
    assert "exit code: 0" in run_command(ws, "echo local")  # local by default
    ws.exec_backend = RemoteBackend(sandbox, TOKEN)
    out = run_command(ws, "echo remote")
    assert out.startswith("[sandbox]")
    assert "remote" in out


def test_local_backend_matches_previous_format(ws):
    out = LocalBackend().exec(ws, "echo hi")
    assert out == "exit code: 0\nhi"


def test_remote_backend_from_config(monkeypatch):
    assert remote_backend_from_config({}) is None
    monkeypatch.delenv("SILKCODE_SANDBOX_TOKEN", raising=False)
    with pytest.raises(ToolError, match="no token"):
        remote_backend_from_config({"sandbox": {"url": "http://x"}})
    monkeypatch.setenv("SILKCODE_SANDBOX_TOKEN", "t")
    backend = remote_backend_from_config({"sandbox": {"url": "http://x/"}})
    assert backend.url == "http://x"


# --------------------------------------------------------------------------
# clone timeouts and failures leave nothing half-finished
# --------------------------------------------------------------------------

def test_a_clone_that_times_out_is_reported_clearly(tmp_path, monkeypatch):
    """Running out of time on a big repository is an expected answer, not a
    crash, and the message has to name the limit so the caller knows to raise
    it rather than guess at a stack trace."""
    import subprocess as sp

    from silkcode.sandbox_server import _clone_repo

    def slow(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="git clone", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr("silkcode.sandbox_server.subprocess.run", slow)
    dest = tmp_path / "ws" / "w"
    with pytest.raises(ValueError, match="timed out after 7s"):
        _clone_repo("https://example.invalid/o/r.git", dest, timeout=7)


def test_a_timed_out_clone_leaves_nothing_behind(tmp_path, monkeypatch):
    """A half-written clone would be taken for a finished one by the .git
    check on the next attempt, and never repaired."""
    import subprocess as sp

    from silkcode.sandbox_server import _clone_repo

    def half_write(args, **kwargs):
        dest = Path(args[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        raise sp.TimeoutExpired(cmd="git clone", timeout=1)

    monkeypatch.setattr("silkcode.sandbox_server.subprocess.run", half_write)
    dest = tmp_path / "ws" / "w"
    with pytest.raises(ValueError):
        _clone_repo("https://example.invalid/o/r.git", dest, timeout=1)
    assert not (dest / ".git").exists(), "a partial clone was left looking complete"


def test_a_failed_clone_leaves_nothing_behind(tmp_path, monkeypatch):
    import subprocess as sp

    from silkcode.sandbox_server import _clone_repo

    def fail(args, **kwargs):
        dest = Path(args[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        return sp.CompletedProcess(args, 128, "", "fatal: repository not found\n")

    monkeypatch.setattr("silkcode.sandbox_server.subprocess.run", fail)
    dest = tmp_path / "ws" / "w"
    with pytest.raises(ValueError, match="repository not found"):
        _clone_repo("https://example.invalid/o/r.git", dest)
    assert not (dest / ".git").exists()


def test_a_fetch_timeout_keeps_the_existing_clone(tmp_path, monkeypatch):
    """Updating is best-effort: an existing checkout that is slightly stale
    beats failing the request outright."""
    import subprocess as sp

    from silkcode.sandbox_server import _clone_repo

    dest = tmp_path / "ws" / "w"
    (dest / ".git").mkdir(parents=True)
    (dest / "file.txt").write_text("kept\n")

    def slow(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="git fetch", timeout=1)

    monkeypatch.setattr("silkcode.sandbox_server.subprocess.run", slow)
    _clone_repo("https://example.invalid/o/r.git", dest, timeout=1)
    assert (dest / "file.txt").read_text() == "kept\n"


def test_a_missing_git_binary_is_reported(tmp_path, monkeypatch):
    from silkcode.sandbox_server import _clone_repo

    monkeypatch.setattr("silkcode.sandbox_server.subprocess.run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    with pytest.raises(ValueError, match="git is not installed"):
        _clone_repo("https://example.invalid/o/r.git", tmp_path / "ws" / "w")


def test_a_stalled_sandbox_is_distinguished_from_a_command_timeout(tmp_path):
    """A command that runs too long is answered by the sandbox with a
    "timed out" result. A round trip that stalls is a different failure with
    a different remedy, so the message has to say which one happened."""
    from silkcode.execbackend import RemoteBackend

    class Stalling(httpx.Client):
        def post(self, *a, **k):
            raise httpx.ReadTimeout("read timed out")

    backend = RemoteBackend("http://127.0.0.1:9", token="t", client=Stalling())
    ws = Workspace(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        backend.exec(ws, "sleep 1", timeout=30)
    message = str(excinfo.value)
    assert "did not respond" in message
    assert "30s" in message
