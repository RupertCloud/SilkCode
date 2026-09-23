"""The interactive REPL loop, driven with scripted input.

run_repl's while-loop is what a user actually sits in front of: it reads a
line, routes slash commands, runs turns, persists the session and handles
Ctrl-C. Every branch of it was untested because it blocks on input(), which
is only an obstacle until you script the input.
"""

from __future__ import annotations

import json

import pytest
from conftest import sse_response

from silkcode.cli.repl import run_repl
from silkcode.sessions import SessionStore


@pytest.fixture
def repl_env(tmp_path, stub_server, monkeypatch):
    """A workspace and a scripted model; returns a runner taking input lines."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")

    servers = []

    def run(lines, scripted=None, **kwargs):
        """Feed `lines` to the REPL as if typed, then EOF."""
        scripted = scripted if scripted is not None else [
            sse_response(content="All done.",
                         usage={"prompt_tokens": 10, "completion_tokens": 4}),
        ]
        server = stub_server(scripted)
        server.thread.start()
        servers.append(server)
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat",
                                   "base_url": server.base_url,
                                   "default_model": "stub-model"}},
        }))
        pending = list(lines)

        def fake_input(prompt=""):
            if not pending:
                raise EOFError
            return pending.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        return run_repl(str(workspace), None, "agent", **kwargs)

    yield run, workspace, home
    for server in servers:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_end_of_input_exits_cleanly(repl_env):
    run, _, _ = repl_env
    assert run([]) == 0


def test_exit_command_leaves_the_loop(repl_env, capsys):
    run, _, _ = repl_env
    assert run(["/exit"]) == 0


def test_blank_lines_are_ignored(repl_env, capsys):
    run, _, _ = repl_env
    assert run(["", "   ", "/exit"]) == 0


def test_a_typed_request_runs_a_turn(repl_env, capsys):
    run, workspace, _ = repl_env
    scripted = [
        sse_response(tool_calls=[("write_file",
                                  json.dumps({"path": "made.txt", "content": "yes"}))],
                     usage={"prompt_tokens": 5, "completion_tokens": 2}),
        sse_response(content="Created it.",
                     usage={"prompt_tokens": 6, "completion_tokens": 2}),
    ]
    run(["make a file", "/exit"], scripted=scripted)
    assert (workspace / "made.txt").read_text() == "yes"


def test_the_session_is_saved_with_the_first_line_as_its_title(repl_env):
    run, _, home = repl_env
    run(["fix the flaky test", "/exit"])
    sessions = SessionStore().list()
    assert sessions and sessions[0]["title"] == "fix the flaky test"


def test_usage_is_persisted_on_the_session(repl_env):
    run, _, _ = repl_env
    run(["do a thing", "/exit"])
    saved = SessionStore().load(SessionStore().list()[0]["id"])
    assert saved["usage"]["prompt_tokens"] == 10
    assert saved["usage"]["completion_tokens"] == 4


def test_an_empty_session_is_not_persisted(repl_env):
    """Starting the REPL and quitting should not litter the session list."""
    run, _, _ = repl_env
    run(["/exit"])
    assert SessionStore().list() == []


def test_slash_commands_do_not_become_the_session_title(repl_env):
    run, _, _ = repl_env
    run(["/mode ask", "a real request", "/exit"])
    sessions = SessionStore().list()
    assert sessions[0]["title"] == "a real request"


def test_a_provider_error_does_not_end_the_session(repl_env, monkeypatch, capsys):
    """A failed turn should return to the prompt, not drop the user out.

    A dropped connection or a rate limit is routine; losing the session over
    it would mean losing the conversation too.
    """
    run, _, _ = repl_env

    from silkcode.agent import Agent
    from silkcode.providers import ProviderError

    calls = []

    def flaky(self, user_input):
        calls.append(user_input)
        if len(calls) == 1:
            raise ProviderError("connection reset by peer")
        self.messages.append({"role": "assistant", "content": "Recovered."})
        print("Recovered.")
        return "Recovered."

    monkeypatch.setattr(Agent, "run_turn", flaky)
    assert run(["first request", "second request", "/exit"]) == 0
    out = capsys.readouterr().out
    assert "provider error" in out
    assert "Recovered." in out, "the REPL should still accept the next request"
    assert calls == ["first request", "second request"]


def test_an_interrupt_leaves_a_valid_conversation(repl_env, monkeypatch, capsys):
    """Ctrl-C mid-turn must not leave an assistant tool_calls message with no
    matching tool results — the next request would be rejected as malformed."""
    run, _, _ = repl_env

    from silkcode.agent import Agent
    real_run_turn = Agent.run_turn

    def interrupted(self, user_input):
        # simulate a turn that got as far as requesting a tool, then Ctrl-C
        self.messages.append({"role": "user", "content": user_input})
        self.messages.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        raise KeyboardInterrupt

    monkeypatch.setattr(Agent, "run_turn", interrupted)
    run(["do something long", "/exit"])

    saved = SessionStore().load(SessionStore().list()[0]["id"])
    messages = saved["messages"]
    assert "[interrupted]" in capsys.readouterr().out

    # every tool_call id must have a matching tool result
    requested = [tc["id"] for m in messages if m.get("tool_calls")
                 for tc in m["tool_calls"]]
    answered = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
    assert requested and set(requested) <= set(answered), (
        "an interrupted turn left tool calls unanswered; the next request "
        "would be rejected by the provider")


def test_a_session_can_be_resumed_with_its_history(repl_env):
    run, workspace, _ = repl_env
    run(["remember this request", "/exit"])
    saved = SessionStore().load(SessionStore().list()[0]["id"])
    assert any(m.get("content") == "remember this request" for m in saved["messages"])

    resumed = run(["/exit"], resume=saved)
    assert resumed == 0


def test_auto_push_runs_after_an_interactive_turn(repl_env, monkeypatch, capsys):
    run, _, _ = repl_env
    pushed = []
    monkeypatch.setattr("silkcode.tools.git.push_if_needed",
                        lambda ws: pushed.append(ws) or "Pushed main to origin.")
    run(["do a thing", "/exit"], auto_push=True)
    assert pushed, "auto-push did not run after the turn"
    assert "auto-push" in capsys.readouterr().out
