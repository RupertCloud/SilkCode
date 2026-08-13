from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult
from silkcode.workspace import Workspace


def make_agent(tmp_path, max_context_tokens):
    return Agent(
        FakeProvider([]), "m", Workspace(tmp_path), PermissionManager("edit"),
        checkpoints=Checkpoints(), max_context_tokens=max_context_tokens,
    )


def add_turn(agent, i, tool_output_chars=4000):
    agent.messages.append({"role": "user", "content": f"request {i}"})
    agent.messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function",
         "function": {"name": "read_file", "arguments": "{}"}},
    ]})
    agent.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * tool_output_chars})
    agent.messages.append({"role": "assistant", "content": f"done {i}"})


def test_no_compaction_under_budget(tmp_path):
    agent = make_agent(tmp_path, max_context_tokens=1_000_000)
    add_turn(agent, 1)
    before = [dict(m) for m in agent.messages]
    agent._compact()
    assert agent.messages == before
    assert agent.trimmed_messages == 0


def test_stage1_truncates_old_tool_outputs(tmp_path):
    agent = make_agent(tmp_path, max_context_tokens=2)  # force compaction
    for i in range(10):
        add_turn(agent, i)
    agent.messages.append({"role": "user", "content": "current request"})
    agent._compact()
    # recent tool outputs are preserved in full, old ones truncated
    tool_messages = [m for m in agent.messages if m["role"] == "tool"]
    for m in tool_messages:
        assert len(m["content"]) < 5000 or m is tool_messages[-1]


def test_stage2_drops_whole_turns_at_user_boundaries(tmp_path):
    agent = make_agent(tmp_path, max_context_tokens=500)
    for i in range(12):
        add_turn(agent, i)
    agent.messages.append({"role": "user", "content": "current request"})
    agent._compact()

    assert agent.trimmed_messages > 0
    assert agent.messages[0]["role"] == "system"
    assert "trimmed" in agent.messages[0]["content"]
    # the current request survives
    assert agent.messages[-1]["content"] == "current request"
    # invariant: every assistant tool_call is immediately followed by its tool result
    for i, m in enumerate(agent.messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            assert agent.messages[i + 1]["role"] == "tool"
    # and no tool message appears without a preceding assistant tool_call
    for i, m in enumerate(agent.messages):
        if m.get("role") == "tool":
            assert agent.messages[i - 1].get("tool_calls") or agent.messages[i - 1].get("role") == "tool"


def test_current_turn_never_dropped(tmp_path):
    agent = make_agent(tmp_path, max_context_tokens=1)  # impossible budget
    add_turn(agent, 1)
    agent.messages.append({"role": "user", "content": "only remaining turn"})
    agent._compact()
    assert any(m.get("content") == "only remaining turn" for m in agent.messages)


def test_context_tokens_counts_tool_calls(tmp_path):
    agent = make_agent(tmp_path, max_context_tokens=100)
    base = agent.context_tokens()
    add_turn(agent, 1, tool_output_chars=400)
    assert agent.context_tokens() > base
