"""Agent commits register Silk Code as co-author with model provenance."""

import json
import subprocess
import threading

import pytest
from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager
from silkcode.providers.base import ChatResult, ToolCall
from silkcode.tools.git import CO_AUTHOR, clear_attribution, get_attribution, git_commit, set_attribution
from silkcode.workspace import Workspace


@pytest.fixture(autouse=True)
def clean_attribution():
    clear_attribution()
    yield
    clear_attribution()


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    return Workspace(tmp_path)


def last_commit_message(ws):
    return subprocess.run(["git", "log", "-1", "--format=%B"], cwd=ws.root,
                          capture_output=True, text=True).stdout


def test_commit_carries_trailers_when_attributed(repo):
    set_attribution(model="deepseek/deepseek-chat", session=42)
    (repo.root / "a.py").write_text("x = 1\n")
    git_commit(repo, "Add a.py")
    message = last_commit_message(repo)
    assert CO_AUTHOR in message
    assert "X-Silk-Model: deepseek/deepseek-chat" in message
    assert "X-Silk-Session: 42" in message
    assert message.startswith("Add a.py\n\n")
    # human author identity is untouched
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=repo.root,
                            capture_output=True, text=True).stdout.strip()
    assert author == "Dev <dev@example.com>"


def test_human_commit_stays_clean(repo):
    (repo.root / "a.py").write_text("x = 1\n")
    git_commit(repo, "Add a.py")  # no attribution context: human-initiated
    message = last_commit_message(repo)
    assert "Silk Code" not in message
    assert "X-Silk-Model" not in message


def test_attribution_is_thread_local(repo):
    set_attribution(model="main-thread-model")
    seen = {}

    def other_thread():
        seen["info"] = get_attribution()

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert seen["info"] is None
    assert get_attribution()["model"] == "main-thread-model"


def test_agent_turn_attributes_its_own_model(repo):
    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="git_commit",
                                        arguments=json.dumps({"message": "Agent work"}))]),
        ChatResult(content="committed"),
    ])
    (repo.root / "work.py").write_text("y = 2\n")
    agent = Agent(provider, "fake-model", repo, PermissionManager("agent"),
                  checkpoints=Checkpoints(), session_id=7)
    agent.run_turn("commit your work")
    message = last_commit_message(repo)
    assert CO_AUTHOR in message
    assert "X-Silk-Model: fake/fake-model" in message
    assert "X-Silk-Session: 7" in message


def test_agent_attribution_can_be_disabled(repo):
    provider = FakeProvider([
        ChatResult(tool_calls=[ToolCall(id="c1", name="git_commit",
                                        arguments=json.dumps({"message": "Quiet work"}))]),
        ChatResult(content="done"),
    ])
    (repo.root / "work.py").write_text("y = 2\n")
    agent = Agent(provider, "fake-model", repo, PermissionManager("agent"),
                  checkpoints=Checkpoints(), attribution=False)
    agent.run_turn("commit your work")
    assert "Silk Code" not in last_commit_message(repo)
