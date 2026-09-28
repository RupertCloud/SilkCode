"""Session forking: continue one conversation down two roads.

unreal-agent's fork, on our store: the child copies the parent's history up
to a complete turn and records where it came from; parent and child then
diverge freely. In the GUI a fork also gets its own isolated git worktree
where the project allows one, so the two lines diverge on disk too.
"""

import json
import subprocess

import pytest

from silkcode.sessions import SessionStore, fork_session, new_session
from silkcode.worktree import BRANCH_PREFIX


def parent_with(messages):
    data = new_session(7, title="fix the login bug", model="stub/m",
                       cwd="/tmp/x", mode="edit")
    data["messages"] = messages
    return data


TOOL_CALL = {"id": "c1", "function": {"name": "run_command", "arguments": "{}"}}


# ---- the pure fork -------------------------------------------------------------

def test_fork_copies_history_and_records_its_parent():
    parent = parent_with([{"role": "system", "content": "s"},
                          {"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])
    child = fork_session(parent, 8)
    assert child["id"] == 8
    assert child["messages"] == parent["messages"]
    assert child["forked_from"] == {"version": 1, "id": 7, "at_message": 3}
    assert child["title"].startswith("⑂")
    assert "fix the login bug" in child["title"]


def test_fork_is_a_deep_copy_not_a_shared_reference():
    parent = parent_with([{"role": "user", "content": "hi"}])
    child = fork_session(parent, 8)
    child["messages"][0]["content"] = "changed in the child"
    assert parent["messages"][0]["content"] == "hi"


def test_fork_at_a_message_cuts_back_to_a_complete_turn():
    parent = parent_with([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]},
        {"role": "tool", "content": "exit code: 0"},
        {"role": "assistant", "content": "done"},
    ])
    # a cut inside the tool exchange pulls back before the dangling request
    child = fork_session(parent, 8, at_message=3)
    assert [m["role"] for m in child["messages"]] == ["system", "user"]
    # a cut on a complete turn keeps everything up to it
    child = fork_session(parent, 9, at_message=4)
    assert [m["role"] for m in child["messages"]] == ["system", "user",
                                                      "assistant", "tool"]


def test_fork_of_an_untitled_session_names_the_parent():
    parent = parent_with([])
    parent["title"] = ""
    child = fork_session(parent, 8)
    assert "#7" in child["title"]


# ---- forking in the GUI ---------------------------------------------------------

@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat",
                               "base_url": "http://127.0.0.1:1",
                               "default_model": "m"}},
    }))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")

    from silkcode.gui.server import GuiState
    return GuiState(str(workspace), None, "edit"), workspace


def test_gui_fork_outside_git_falls_back_in_place(gui_state):
    state, workspace = gui_state
    parent = state.get_session()
    parent.agent.messages.append({"role": "user", "content": "try plan A"})
    child = state.fork_session(parent.id)
    assert child.id != parent.id
    assert child.agent.messages[-1]["content"] == "try plan A"
    assert child.data["forked_from"]["id"] == parent.id
    assert "worktree" not in child.data["forked_from"]
    assert str(child.workspace.root) == str(workspace)
    notice = next(t["text"] for t in child.transcript if t["kind"] == "notice"
                  and t["text"].startswith("⑂"))
    assert "in place" in notice
    # the fork is durable: it can be loaded back from the store
    assert SessionStore().load(child.id)["forked_from"]["id"] == parent.id


def test_gui_fork_in_a_git_repo_gets_its_own_worktree(gui_state):
    state, workspace = gui_state
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=workspace, check=True)
    parent = state.get_session()
    child = state.fork_session(parent.id)
    fork_info = child.data["forked_from"]
    assert fork_info["worktree"]["branch"].startswith(BRANCH_PREFIX)
    assert str(child.workspace.root) != str(workspace)
    assert (child.workspace.root / "README.md").exists()
    # the parent's checkout is untouched and its session unchanged
    assert str(parent.workspace.root) == str(workspace)
    notice = next(t["text"] for t in child.transcript if t["kind"] == "notice")
    assert "git merge silk/" in notice


def test_gui_fork_refuses_while_the_agent_is_running(gui_state):
    from silkcode.workspace import ToolError
    state, _ = gui_state
    session = state.get_session()
    session.running = True
    with pytest.raises(ToolError):
        state.fork_session(session.id)
