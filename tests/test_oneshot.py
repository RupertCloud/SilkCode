"""One-shot CLI mode against a scripted provider."""

import json

import pytest
from conftest import sse_response

from silkcode.cli.repl import run_repl


@pytest.fixture
def oneshot_env(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()

    scripted = [
        sse_response(tool_calls=[("write_file", json.dumps({"path": "hello.txt", "content": "hi"}))],
                     usage={"prompt_tokens": 10, "completion_tokens": 4}),
        sse_response(content="Created hello.txt", usage={"prompt_tokens": 15, "completion_tokens": 3}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url, "default_model": "stub-model"}},
    }))
    yield workspace
    server.httpd.shutdown()
    server.httpd.server_close()


def test_oneshot_prompt_runs_and_saves_session(oneshot_env, capsys):
    workspace = oneshot_env
    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt")
    assert rc == 0
    assert (workspace / "hello.txt").read_text() == "hi"
    out = capsys.readouterr().out
    assert "Created hello.txt" in out

    from silkcode.sessions import SessionStore
    sessions = SessionStore().list()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "create hello.txt"


def test_oneshot_system_prompt_includes_repo_map(oneshot_env, stub_server, monkeypatch):
    workspace = oneshot_env
    (workspace / "app.py").write_text("x = 1\n")
    scripted = [sse_response(content="ok")]
    server = stub_server(scripted)
    server.thread.start()
    try:
        import json as _json
        from silkcode.config import config_dir
        (config_dir() / "config.json").write_text(_json.dumps({
            "default_model": "stub2",
            "providers": {"stub2": {"type": "openai_compat", "base_url": server.base_url, "default_model": "m"}},
        }))
        rc = run_repl(str(workspace), None, "agent", prompt="hello")
        assert rc == 0
        system = server.requests[0]["messages"][0]
        assert system["role"] == "system"
        assert "Repository map:" in system["content"]
        assert "app.py" in system["content"]
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
