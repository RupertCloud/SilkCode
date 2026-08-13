"""Remote sandbox: protocol server + RemoteBackend, end to end over HTTP."""

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
