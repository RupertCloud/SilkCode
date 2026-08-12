"""End-to-end: the real Agent + OpenAICompatProvider against a scripted
OpenAI-compatible HTTP server, building and running a small project."""

import json

from conftest import sse_response

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager
from silkcode.providers.openai_compat import OpenAICompatProvider
from silkcode.workspace import Workspace

APP = """def add(a, b):
    return a + b

if __name__ == "__main__":
    print(add(2, 3))
"""

CHECK = """from app import add

assert add(2, 3) == 5
assert add(-1, 1) == 0
print("all checks passed")
"""


def test_agent_builds_and_verifies_a_project(tmp_path, stub_server):
    scripted = [
        sse_response(content="I'll create the project.",
                     tool_calls=[("write_file", json.dumps({"path": "app.py", "content": APP}))],
                     usage={"prompt_tokens": 100, "completion_tokens": 40}),
        sse_response(tool_calls=[("write_file", json.dumps({"path": "check.py", "content": CHECK}))],
                     usage={"prompt_tokens": 150, "completion_tokens": 30}),
        sse_response(tool_calls=[("run_command", json.dumps({"command": "python3 check.py"}))],
                     usage={"prompt_tokens": 180, "completion_tokens": 20}),
        sse_response(content="Project created and verified: all checks passed.",
                     usage={"prompt_tokens": 200, "completion_tokens": 15}),
    ]
    events = []
    with stub_server(scripted) as server:
        provider = OpenAICompatProvider("stub", server.base_url, api_key="test-key")
        agent = Agent(
            provider,
            "stub-model",
            Workspace(tmp_path),
            PermissionManager("agent"),  # medium-risk commands auto-approved
            checkpoints=Checkpoints(),
            on_event=lambda kind, data: events.append((kind, data)),
        )
        reply = agent.run_turn("Build a small calculator project and verify it works")

    assert reply == "Project created and verified: all checks passed."
    assert (tmp_path / "app.py").read_text() == APP
    assert (tmp_path / "check.py").read_text() == CHECK

    # the run_command tool actually executed the verification script
    tool_results = [d for k, d in events if k == "tool_result" and d["name"] == "run_command"]
    assert "all checks passed" in tool_results[0]["output"] or any(
        "all checks passed" in m["content"] for m in agent.messages if m["role"] == "tool"
    )

    # usage accumulated across all four model calls
    assert agent.usage.total_tokens == 735

    # requests carried the tool schemas and an API key
    first = server.requests[0]
    assert any(t["function"]["name"] == "write_file" for t in first["tools"])
