import json

import pytest
from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.context import build_context
from silkcode.memory import load_memory, remember
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, ToolCall
from silkcode.skills import load_skills, use_skill
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    return Workspace(root)


def write_skill(directory, filename, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(text)


def test_skill_frontmatter_and_fallbacks(ws, tmp_path):
    user_skills = tmp_path / "home" / "skills"
    write_skill(user_skills, "react.md", "---\nname: react-expert\ndescription: React guidance\n---\nUse hooks.")
    write_skill(user_skills, "plain.md", "# Django tips\nUse the ORM.")

    skills = load_skills(ws)
    assert skills["react-expert"].description == "React guidance"
    assert skills["plain"].description == "Django tips"
    assert use_skill(ws, "react-expert") == "Skill 'react-expert':\nUse hooks."


def test_project_skill_overrides_user_skill(ws, tmp_path):
    write_skill(tmp_path / "home" / "skills", "x.md", "user version")
    write_skill(ws.root / ".silkcode" / "skills", "x.md", "project version")
    assert "project version" in use_skill(ws, "x")


def test_use_skill_unknown(ws):
    with pytest.raises(ToolError, match="Unknown skill"):
        use_skill(ws, "nope")


def test_memory_roundtrip(ws):
    assert load_memory(ws) == ""
    remember(ws, "We use PostgreSQL")
    remember(ws, "Run pytest after auth changes")
    content = load_memory(ws)
    assert "We use PostgreSQL" in content
    assert "Run pytest after auth changes" in content


def test_build_context_includes_everything(ws):
    (ws.root / "SILKCODE.md").write_text("Use TypeScript. Never use any.")
    (ws.root / "app.py").write_text("x = 1\n")
    remember(ws, "arch: monolith")
    write_skill(ws.root / ".silkcode" / "skills", "sec.md",
                "---\nname: security-reviewer\ndescription: Security review checklist\n---\n...")

    context = build_context(ws)
    assert "Repository map:" in context
    assert "Use TypeScript. Never use any." in context
    assert "arch: monolith" in context
    assert "security-reviewer: Security review checklist" in context


def test_agent_remember_tool_writes_memory(ws):
    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="remember",
                                        arguments=json.dumps({"text": "uses sqlite"}))]),
        ChatResult(content="noted"),
    ])
    agent = Agent(provider, "m", ws, PermissionManager("edit"), checkpoints=Checkpoints())
    assert agent.run_turn("remember that") == "noted"
    assert "uses sqlite" in load_memory(ws)
    # revert also undoes memory writes (checkpointed like any file write)
    agent.checkpoints.revert_last()
    assert load_memory(ws) == ""


def test_agent_use_skill_tool(ws, tmp_path):
    write_skill(tmp_path / "home" / "skills", "tester.md",
                "---\nname: test-engineer\ndescription: testing standards\n---\nAlways add regression tests.")
    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="use_skill",
                                        arguments=json.dumps({"name": "test-engineer"}))]),
        ChatResult(content="done"),
    ])
    agent = Agent(provider, "m", ws, PermissionManager("edit"), checkpoints=Checkpoints())
    agent.run_turn("load the skill")
    tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]
    assert "Always add regression tests." in tool_msg["content"]
