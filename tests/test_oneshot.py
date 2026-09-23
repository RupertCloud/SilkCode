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


def test_reload_command_picks_up_config_changes(tmp_path, monkeypatch, capsys, stub_server):
    """/reload re-reads the config and rebuilds the provider so a new
    'timeout' (or other provider setting) applies without restarting."""
    from silkcode.agent import Agent
    from silkcode.config import Config, config_dir
    from silkcode.cli.repl import _reload_command
    from silkcode.context import build_context
    from silkcode.permissions import PermissionManager
    from silkcode.providers import build_provider
    from silkcode.workspace import Workspace

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")

    scripted = [sse_response(content="ok")]
    server = stub_server(scripted)
    server.thread.start()
    try:
        (config_dir() / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                   "default_model": "stub-model"}},
        }))
        config = Config.load()
        provider = build_provider("stub", config.providers["stub"], api_key=None)
        agent = Agent(provider, "stub-model", Workspace(str(workspace)),
                      PermissionManager(mode="ask"), context=build_context(Workspace(str(workspace))))
        session = {"model": "stub/stub-model"}

        assert agent.provider._client.timeout.connect == 180.0  # default

        # user edits the config: raises the timeout to 600
        (config_dir() / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                   "default_model": "stub-model", "timeout": 600}},
        }))
        mcp = _reload_command(agent, config, session, None)
        assert mcp is None
        assert agent.provider._client.timeout.connect == 600.0
        out = capsys.readouterr().out
        assert "config reloaded" in out
        assert "timeout 600s" in out
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_oneshot_pushes_when_auto_push_is_on(oneshot_env, monkeypatch, capsys):
    """--auto-push says "after each turn", and a one-shot run is a turn.

    It is also the case that needs it most: there is no prompt afterwards
    for anyone to type /push at, which is the whole point of running
    non-interactively.
    """
    pushed = []
    monkeypatch.setattr("silkcode.tools.git.push_if_needed",
                        lambda ws: pushed.append(ws) or "Pushed main to origin.")

    rc = run_repl(str(oneshot_env), None, "agent", prompt="create hello.txt",
                  auto_push=True)
    assert rc == 0
    assert pushed, "the one-shot turn finished without pushing"
    assert "auto-push" in capsys.readouterr().out


def test_oneshot_does_not_push_when_auto_push_is_off(oneshot_env, monkeypatch):
    pushed = []
    monkeypatch.setattr("silkcode.tools.git.push_if_needed",
                        lambda ws: pushed.append(ws) or "Pushed.")
    run_repl(str(oneshot_env), None, "agent", prompt="create hello.txt")
    assert not pushed


def test_auto_push_grants_push_without_prompting(oneshot_env, monkeypatch):
    """Turning auto-push on has to carry the push grant, or the agent stops
    to ask for permission on a run with nobody watching."""
    seen = {}
    monkeypatch.setattr("silkcode.tools.git.push_if_needed", lambda ws: None)
    real_init = __import__("silkcode.permissions", fromlist=["PermissionManager"]).PermissionManager.__init__

    def spy(self, *a, **kw):
        real_init(self, *a, **kw)
        seen["grants"] = set(self.grants)

    monkeypatch.setattr("silkcode.permissions.PermissionManager.__init__", spy)
    run_repl(str(oneshot_env), None, "agent", prompt="create hello.txt", auto_push=True)
    assert "push" in seen["grants"]
