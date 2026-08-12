import json

from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, ToolCall, Usage
from silkcode.workspace import Workspace


def make_agent(tmp_path, results, mode="edit", asker=None):
    provider = FakeProvider(results)
    agent = Agent(
        provider,
        "fake-model",
        Workspace(tmp_path),
        PermissionManager(mode, asker=asker or (lambda p: "no")),
        checkpoints=Checkpoints(),
    )
    return agent, provider


def tc(name, **args):
    return ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(args))


def test_agent_executes_tools_then_finishes(tmp_path):
    agent, provider = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("write_file", path="hello.py", content="print('hi')\n")],
                   usage=Usage(10, 5)),
        ChatResult(content="Created hello.py", usage=Usage(20, 4)),
    ])
    reply = agent.run_turn("create hello.py")
    assert reply == "Created hello.py"
    assert (tmp_path / "hello.py").read_text() == "print('hi')\n"
    assert agent.usage.total_tokens == 39

    # conversation structure follows the OpenAI tool-calling contract
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert agent.messages[2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert agent.messages[3]["tool_call_id"] == "call_write_file"


def test_agent_reports_tool_errors_to_model(tmp_path):
    agent, _ = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("read_file", path="missing.txt")]),
        ChatResult(content="done"),
    ])
    agent.run_turn("read it")
    tool_msg = agent.messages[3]
    assert tool_msg["role"] == "tool"
    assert "Error" in tool_msg["content"]


def test_agent_denied_permission(tmp_path):
    agent, _ = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("write_file", path="a.txt", content="x")]),
        ChatResult(content="ok, stopping"),
    ], mode="ask", asker=lambda p: "no")
    agent.run_turn("write a file")
    assert not (tmp_path / "a.txt").exists()
    assert "denied" in agent.messages[3]["content"].lower()


def test_agent_unknown_tool(tmp_path):
    agent, _ = make_agent(tmp_path, [
        ChatResult(tool_calls=[ToolCall(id="c1", name="not_a_tool", arguments="{}")]),
        ChatResult(content="done"),
    ])
    agent.run_turn("go")
    assert "unknown tool" in agent.messages[3]["content"]


def test_agent_checkpoint_revert(tmp_path):
    (tmp_path / "app.py").write_text("v1")
    agent, _ = make_agent(tmp_path, [
        ChatResult(tool_calls=[tc("write_file", path="app.py", content="v2")]),
        ChatResult(content="updated"),
    ])
    agent.run_turn("update app.py")
    assert (tmp_path / "app.py").read_text() == "v2"
    agent.checkpoints.revert_last()
    assert (tmp_path / "app.py").read_text() == "v1"
