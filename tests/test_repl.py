"""The REPL's slash commands, driven the way a user types them.

_handle_slash is the whole interactive control surface: switching models and
modes, reverting, pushing, inspecting usage. It is dispatched by string
matching, so a renamed helper or a changed signature behind any one branch
fails only when someone types that command. These tests type all of them.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.cli.repl import _handle_slash
from silkcode.config import Config
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, Usage
from silkcode.sessions import SessionStore
from silkcode.workspace import Workspace


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(d))
    for var in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return d


@pytest.fixture
def repl(tmp_path, home):
    """An agent plus the state _handle_slash mutates."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = Agent(
        FakeProvider([ChatResult(content="ok", usage=Usage(1, 1))]),
        "fake-model",
        Workspace(workspace),
        PermissionManager("ask"),
        checkpoints=Checkpoints(),
    )
    session = {"id": 1, "model": "fake", "mode": "ask", "title": "t", "messages": []}
    return {"agent": agent, "config": Config.load(), "session": session,
            "store": SessionStore()}


def slash(repl, line, **kwargs):
    return _handle_slash(line, repl["agent"], repl["config"], repl["session"],
                         repl["store"], **kwargs)


# --------------------------------------------------------------------------
# every command is reachable
# --------------------------------------------------------------------------

def test_no_slash_command_raises(repl, capsys, monkeypatch):
    """Type every command the help text advertises; none may explode."""
    from silkcode.cli.repl import HELP
    import re

    advertised = sorted(set(re.findall(r"/[a-z]+", HELP)))
    assert len(advertised) > 10, "expected the help text to list the commands"

    # /push would reach a real remote; stub the tool, not the dispatch.
    monkeypatch.setattr("silkcode.tools.git.git_push", lambda ws: "nothing to push")
    # /project with no argument opens an interactive picker; answer "quit".
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    for command in advertised:
        if command in ("/exit", "/quit", "/q"):
            continue
        exited, _ = slash(repl, command)
        assert exited is False, f"{command} should not exit the REPL"
    capsys.readouterr()


def test_exit_commands_end_the_repl(repl):
    for command in ("/exit", "/quit", "/q"):
        exited, _ = slash(repl, command)
        assert exited is True


def test_unknown_command_is_reported(repl, capsys):
    slash(repl, "/nonsense")
    assert "Unknown command" in capsys.readouterr().out


def test_commands_are_case_insensitive(repl, capsys):
    exited, _ = slash(repl, "/EXIT")
    assert exited is True


# --------------------------------------------------------------------------
# /model and /mode
# --------------------------------------------------------------------------

def test_model_without_argument_shows_the_current_model(repl, capsys):
    slash(repl, "/model")
    assert "fake/fake-model" in capsys.readouterr().out


def test_model_rejects_an_unknown_spec_without_switching(repl, capsys):
    slash(repl, "/model no-such-provider")
    out = capsys.readouterr().out
    assert "Unknown model" in out
    assert repl["agent"].model == "fake-model", "a failed switch must not change the model"


def test_model_switches_to_a_valid_spec(repl, capsys):
    slash(repl, "/model ollama/qwen2.5-coder")
    assert "Switched to ollama/qwen2.5-coder" in capsys.readouterr().out
    assert repl["agent"].model == "qwen2.5-coder"
    assert repl["session"]["model"] == "ollama/qwen2.5-coder"


def test_mode_without_argument_shows_the_current_mode(repl, capsys):
    slash(repl, "/mode")
    assert "mode: ask" in capsys.readouterr().out


def test_mode_switches_and_records_on_the_session(repl, capsys):
    slash(repl, "/mode agent")
    assert repl["agent"].permissions.mode == "agent"
    assert repl["session"]["mode"] == "agent"


def test_mode_rejects_an_unknown_mode(repl, capsys):
    slash(repl, "/mode yolo")
    assert "unknown mode" in capsys.readouterr().out
    assert repl["agent"].permissions.mode == "ask", "a bad mode must not loosen permissions"


# --------------------------------------------------------------------------
# /autopush  (it grants push, so it is a permission change)
# --------------------------------------------------------------------------

def test_autopush_on_adds_the_push_grant(repl, capsys):
    state = {"on": False}
    slash(repl, "/autopush on", autopush_state=state)
    assert state["on"] is True
    assert "push" in repl["agent"].permissions.grants


def test_autopush_off_does_not_grant_push(repl, capsys):
    state = {"on": False}
    slash(repl, "/autopush off", autopush_state=state)
    assert state["on"] is False
    assert "push" not in repl["agent"].permissions.grants


def test_autopush_without_an_argument_only_reports(repl, capsys):
    state = {"on": False}
    slash(repl, "/autopush", autopush_state=state)
    assert "auto-push is off" in capsys.readouterr().out
    assert "push" not in repl["agent"].permissions.grants


# --------------------------------------------------------------------------
# /revert and /clear
# --------------------------------------------------------------------------

def test_revert_with_nothing_to_undo(repl, capsys):
    slash(repl, "/revert")
    assert "Nothing to revert." in capsys.readouterr().out


def test_revert_restores_a_modified_file(repl, capsys, tmp_path):
    agent = repl["agent"]
    target = agent.workspace.root / "app.py"
    target.write_text("original\n")
    agent.checkpoints.snapshot(target)
    target.write_text("modified\n")

    slash(repl, "/revert")
    assert "Reverted:" in capsys.readouterr().out
    assert target.read_text() == "original\n"


def test_clear_keeps_the_system_prompt(repl, capsys):
    agent = repl["agent"]
    agent.messages.append({"role": "user", "content": "hello"})
    agent.messages.append({"role": "assistant", "content": "hi"})
    before = agent.messages[0]

    slash(repl, "/clear")
    assert agent.messages == [before], "only the system prompt should remain"


# --------------------------------------------------------------------------
# read-only reporting commands
# --------------------------------------------------------------------------

def test_usage_reports_tokens_and_context(repl, capsys):
    slash(repl, "/usage")
    out = capsys.readouterr().out
    assert "tokens:" in out and "context:" in out


def test_sessions_lists_the_store(repl, capsys):
    store = repl["store"]
    store.save({"id": store.new_id(), "model": "ollama/x", "cwd": ".",
                "title": "an earlier session", "messages": []})
    slash(repl, "/sessions")
    assert "an earlier session" in capsys.readouterr().out


def test_memory_explains_itself_when_empty(repl, capsys):
    slash(repl, "/memory")
    assert "No project memory yet" in capsys.readouterr().out


def test_memory_shows_saved_notes(repl, capsys):
    from silkcode.memory import remember
    remember(repl["agent"].workspace, "the parser is generated, do not edit it")
    slash(repl, "/memory")
    assert "do not edit it" in capsys.readouterr().out


def test_skills_points_at_the_directory_when_none_are_installed(repl, capsys):
    slash(repl, "/skills")
    assert "No skills installed" in capsys.readouterr().out


def test_skills_lists_installed_skills(repl, capsys):
    from silkcode.skills import skill_dirs
    directory = skill_dirs(repl["agent"].workspace)[0]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "deploy.md").write_text(
        "---\nname: deploy\ndescription: ship it to production\n---\n\nsteps\n")
    slash(repl, "/skills")
    assert "deploy" in capsys.readouterr().out


def test_mcp_says_so_when_none_are_configured(repl, capsys):
    slash(repl, "/mcp")
    assert "No MCP servers configured" in capsys.readouterr().out


def test_diff_on_a_clean_workspace(repl, capsys):
    slash(repl, "/diff")
    capsys.readouterr()  # a non-git workspace reports an error, but must not raise


# --------------------------------------------------------------------------
# /reload  — picks up config edits without losing the conversation
# --------------------------------------------------------------------------

def _write_config(home, data):
    (home / "config.json").write_text(json.dumps(data))


def test_reload_rereads_the_config_from_disk(repl, home, capsys):
    _write_config(home, {"default_model": "ollama", "providers": {
        "ollama": {"type": "ollama", "base_url": "http://localhost:11434",
                   "default_model": "qwen2.5-coder"}}})
    slash(repl, "/reload")
    assert repl["config"].data["default_model"] == "ollama"
    assert "config reloaded" in capsys.readouterr().out


def test_reload_applies_a_changed_provider_timeout(repl, home, capsys):
    """The reason /reload exists: raising a slow provider's timeout without
    losing the conversation."""
    repl["session"]["model"] = "slow"
    _write_config(home, {"providers": {"slow": {
        "type": "openai_compat", "base_url": "http://127.0.0.1:1/v1",
        "default_model": "m", "timeout": 600}}})
    slash(repl, "/reload")
    assert "timeout 600s" in capsys.readouterr().out


def test_reload_keeps_the_conversation(repl, home, capsys):
    agent = repl["agent"]
    agent.messages.append({"role": "user", "content": "earlier turn"})
    before = list(agent.messages)
    _write_config(home, {"providers": {}})
    slash(repl, "/reload")
    assert agent.messages == before


def test_reload_reports_an_unresolvable_model_without_crashing(repl, home, capsys):
    repl["session"]["model"] = "gone/missing"
    _write_config(home, {"providers": {}})
    slash(repl, "/reload")
    assert "Unknown model" in capsys.readouterr().out


def test_reload_reports_no_mcp_servers(repl, home, capsys):
    _write_config(home, {"providers": {}})
    slash(repl, "/reload")
    assert "MCP: none configured" in capsys.readouterr().out
    assert repl["agent"].mcp is None


def test_reload_closes_the_previous_mcp_manager(repl, home, capsys):
    closed = []

    class FakeMcp:
        servers: dict = {}
        errors: dict = {}

        def close(self):
            closed.append(True)

    _write_config(home, {"providers": {}})
    slash(repl, "/reload", mcp=FakeMcp())
    assert closed, "the old MCP servers must be shut down, not leaked"


# --------------------------------------------------------------------------
# /project — switching which workspace the agent acts on
# --------------------------------------------------------------------------

def test_project_switches_the_workspace(repl, tmp_path, capsys):
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "main.py").write_text("print('other')\n")

    slash(repl, f"/project {other}")
    agent = repl["agent"]
    assert agent.workspace.root == other.resolve()
    assert repl["session"]["cwd"] == str(other.resolve())
    assert "Opened project" in capsys.readouterr().out


def test_project_reorients_the_system_prompt(repl, tmp_path, capsys):
    """The model has to be told which tree it is working in, or it keeps
    reasoning about the old one."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    slash(repl, f"/project {other}")
    assert str(other.resolve()) in repl["agent"].messages[0]["content"]


def test_project_records_the_choice_as_recent(repl, tmp_path, capsys):
    from silkcode.project import recent_projects
    other = tmp_path / "recent-one"
    other.mkdir()
    slash(repl, f"/project {other}")
    assert str(other.resolve()) in [r["spec"] for r in recent_projects()]


def test_project_rejects_a_missing_directory_without_switching(repl, tmp_path, capsys):
    before = repl["agent"].workspace.root
    slash(repl, f"/project {tmp_path / 'no-such-dir'}")
    assert repl["agent"].workspace.root == before, "a failed switch must not move the agent"
    assert capsys.readouterr().out.strip() != ""


def test_project_keeps_the_conversation_across_a_switch(repl, tmp_path, capsys):
    agent = repl["agent"]
    agent.messages.append({"role": "user", "content": "remember this"})
    other = tmp_path / "kept"
    other.mkdir()
    slash(repl, f"/project {other}")
    assert any(m.get("content") == "remember this" for m in agent.messages)


def test_project_cancelled_from_the_picker_changes_nothing(repl, monkeypatch, capsys):
    monkeypatch.setattr("silkcode.github.list_github_repos", lambda: [])
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    before = repl["agent"].workspace.root
    slash(repl, "/project")
    assert repl["agent"].workspace.root == before
