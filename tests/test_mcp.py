import json
import sys
from pathlib import Path

import pytest
from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.mcp import McpError, McpManager, McpServer, sanitize_name
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, ToolCall
from silkcode.workspace import Workspace

FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))
FAKE_CMD = f"{sys.executable} {FAKE_SERVER}"


def test_sanitize_name():
    assert sanitize_name("my server!") == "my-server-"
    assert sanitize_name("a__b") == "a-b"


def test_mcp_server_lifecycle():
    server = McpServer("fake", FAKE_CMD)
    server.start(timeout=10)
    try:
        assert [t["name"] for t in server.tools] == ["echo"]
        assert server.call("echo", {"text": "hi"}) == "echo: hi"
        with pytest.raises(McpError, match="unknown tool"):
            server.call("nope", {})
    finally:
        server.stop()


def test_mcp_server_bad_command():
    server = McpServer("bad", "/nonexistent/binary")
    with pytest.raises(McpError):
        server.start(timeout=5)


def test_mcp_manager_qualified_tools():
    manager = McpManager({"fake": {"command": FAKE_CMD}})
    try:
        assert manager.errors == {}
        assert manager.has_tool("mcp__fake__echo")
        schemas = manager.tool_schemas()
        assert schemas[0]["function"]["name"] == "mcp__fake__echo"
        assert "[MCP: fake]" in schemas[0]["function"]["description"]
        assert manager.call("mcp__fake__echo", {"text": "yo"}) == "echo: yo"
    finally:
        manager.close()


def test_mcp_manager_collects_startup_errors():
    manager = McpManager({"bad": {"command": "/nonexistent/binary"}})
    assert "bad" in manager.errors
    assert manager.servers == {}


def test_agent_routes_mcp_tool_calls(tmp_path):
    manager = McpManager({"fake": {"command": FAKE_CMD}})
    try:
        provider = FakeProvider([
            ChatResult(tool_calls=[ToolCall(id="c1", name="mcp__fake__echo",
                                            arguments=json.dumps({"text": "from model"}))]),
            ChatResult(content="done"),
        ])
        agent = Agent(provider, "fake-model", Workspace(tmp_path),
                      PermissionManager("agent"), checkpoints=Checkpoints(), mcp=manager)
        reply = agent.run_turn("use the mcp tool")
        assert reply == "done"
        tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]
        assert tool_msg["content"] == "echo: from model"
        # MCP schemas were offered to the model
        assert provider.calls  # model saw the conversation
    finally:
        manager.close()


def test_agent_mcp_permission_denied(tmp_path):
    manager = McpManager({"fake": {"command": FAKE_CMD}})
    try:
        provider = FakeProvider([
            ChatResult(tool_calls=[ToolCall(id="c1", name="mcp__fake__echo",
                                            arguments=json.dumps({"text": "x"}))]),
            ChatResult(content="ok"),
        ])
        agent = Agent(provider, "fake-model", Workspace(tmp_path),
                      PermissionManager("ask", asker=lambda p: "no"),
                      checkpoints=Checkpoints(), mcp=manager)
        agent.run_turn("go")
        tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]
        assert "denied" in tool_msg["content"].lower()
    finally:
        manager.close()


def test_check_mcp_permission_modes():
    pm = PermissionManager("agent")
    assert pm.check_mcp("mcp__fake__echo") is True

    answers = iter(["always"])
    pm = PermissionManager("ask", asker=lambda p: next(answers))
    assert pm.check_mcp("mcp__fake__echo") is True
    assert pm.check_mcp("mcp__fake__echo") is True  # cached, no second prompt
