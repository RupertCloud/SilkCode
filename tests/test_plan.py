"""The plan that survives the turn that wrote it.

Without a plan file, a plan lives in one assistant message: the next turn
paraphrases it, compaction drops it, and nothing records which steps actually
happened. These tests pin the two halves - the plan artifact itself, and the
read-only `plan` permission mode whose deliverable it is.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeProvider

from silkcode.agent import Agent
from silkcode.checkpoints import Checkpoints
from silkcode.permissions import PermissionManager, Risk
from silkcode.plan import (
    PLAN_RELPATH,
    plan_path,
    progress,
    propose_plan,
    read_plan,
    update_plan,
)
from silkcode.providers.base import ChatResult, ToolCall
from silkcode.workspace import Workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    return Workspace(root)


# ---- the artifact -----------------------------------------------------------

def test_a_proposed_plan_is_a_file_a_person_can_read(ws):
    result = propose_plan(ws, "Add a JSON flag", "read the CLI\nadd --json\nrun tests")
    assert "3 steps" in result
    text = plan_path(ws).read_text()
    assert text.startswith("# Plan: Add a JSON flag")
    assert text.count("- [ ]") == 3


def test_a_new_proposal_replaces_the_old_plan(ws):
    propose_plan(ws, "First", "one\ntwo")
    propose_plan(ws, "Second", "alpha")
    text = read_plan(ws)
    assert "Second" in text and "First" not in text


def test_steps_are_marked_and_progress_is_reported(ws):
    propose_plan(ws, "Ship it", "write code\nrun tests\npush")
    update_plan(ws, 1, "done")
    update_plan(ws, 2, "in_progress")
    assert "1/3 steps done" in progress(ws)
    assert "in progress: run tests" in progress(ws)
    text = plan_path(ws).read_text()
    assert "- [x] write code" in text
    assert "- [>] run tests" in text


def test_a_skipped_step_carries_its_reason(ws):
    propose_plan(ws, "Ship it", "write code\nmigrate the database")
    update_plan(ws, 2, "skipped", note="no database in this deployment")
    text = plan_path(ws).read_text()
    assert "- [-] migrate the database — no database in this deployment" in text


def test_marking_a_step_that_does_not_exist_says_how_many_there_are(ws):
    propose_plan(ws, "Small", "only step")
    assert "the plan has 1" in update_plan(ws, 5, "done")


def test_no_plan_reads_as_an_instruction_not_an_error(ws):
    assert "propose_plan" in read_plan(ws)
    assert "No plan to update" in update_plan(ws, 1, "done")
    assert progress(ws) == "No plan."


def test_an_unhinged_step_count_is_pushed_back_on(ws):
    result = propose_plan(ws, "Everything", "\n".join(f"step {i}" for i in range(80)))
    assert "phases" in result
    assert not plan_path(ws).is_file()


def test_step_lines_are_normalized_not_double_bulleted(ws):
    """Models hand over steps already formatted as '- [ ] foo'. Writing that
    verbatim under our own bullet yields '- [ ] - [ ] foo'."""
    propose_plan(ws, "Tidy", "- first thing\n- [ ] second thing")
    text = plan_path(ws).read_text()
    assert "- [ ] first thing" in text
    assert "- [ ] second thing" in text
    assert "- [ ] - " not in text


def test_a_person_editing_the_plan_by_hand_is_not_broken_by_the_agent(ws):
    """The file is markdown so the user can edit it. Notes and blank lines
    they add must survive a step update."""
    propose_plan(ws, "Ship", "one\ntwo")
    path = plan_path(ws)
    path.write_text(path.read_text() + "\nDo NOT touch prod until Monday.\n")
    update_plan(ws, 1, "done")
    assert "Do NOT touch prod until Monday." in path.read_text()


# ---- plan mode --------------------------------------------------------------

def test_plan_mode_refuses_writes_without_prompting():
    asked = []
    manager = PermissionManager("plan", asker=lambda p: asked.append(p) or "yes")
    assert manager.check_write("src/app.py") is False
    assert asked == [], "plan mode must refuse, not prompt"


def test_plan_mode_still_allows_writing_the_plan_and_memory():
    manager = PermissionManager("plan")
    assert manager.check_write(PLAN_RELPATH) is True
    assert manager.check_write(".silkcode/memory.db") is True


def test_plan_mode_runs_read_only_commands_and_nothing_else():
    manager = PermissionManager("plan", asker=lambda p: "yes")
    assert manager.check_command("ls -la") is True
    assert manager.check_command("git log --oneline") is True
    assert manager.check_command("pip install requests") is False
    assert manager.check_command("git push") is False


def test_yes_to_all_does_not_override_plan_mode():
    """allow_all answers prompts; plan mode's refusals are not prompts."""
    manager = PermissionManager("plan")
    manager.allow_all()
    assert manager.check_write("src/app.py") is False
    assert manager.check_command("pip install requests") is False


def test_grants_do_not_override_plan_mode():
    manager = PermissionManager("plan", grants={"push"})
    assert manager.check_command("git push") is False


def test_plan_mode_refuses_mcp_tools():
    manager = PermissionManager("plan", asker=lambda p: "yes")
    assert manager.check_mcp("server.tool") is False


def test_plan_is_a_selectable_mode_everywhere():
    assert PermissionManager.MODES == ("plan", "ask", "edit", "agent")
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_text()
    assert '<option value="plan">plan</option>' in app


# ---- the loop explains the mode to the model --------------------------------

def _agent(ws, mode, results):
    return Agent(FakeProvider(results), "m", ws, PermissionManager(mode),
                 checkpoints=Checkpoints())


def test_a_denied_write_in_plan_mode_teaches_the_workflow(ws):
    agent = _agent(ws, "plan", [
        ChatResult(tool_calls=[ToolCall(id="c1", name="write_file",
                                        arguments=json.dumps({"path": "x.py", "content": "1"}))]),
        ChatResult(content="ok"),
    ])
    agent.run_turn("change x")
    tool_result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "propose_plan" in tool_result
    assert not (ws.root / "x.py").exists()


def test_a_denied_command_in_plan_mode_teaches_the_workflow(ws):
    agent = _agent(ws, "plan", [
        ChatResult(tool_calls=[ToolCall(id="c1", name="run_command",
                                        arguments=json.dumps({"command": "pip install x"}))]),
        ChatResult(content="ok"),
    ])
    agent.run_turn("set up")
    tool_result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "read-only commands" in tool_result


def test_the_agent_can_propose_a_plan_in_plan_mode(ws):
    agent = _agent(ws, "plan", [
        ChatResult(tool_calls=[ToolCall(id="c1", name="propose_plan",
                                        arguments=json.dumps({"title": "Fix the bug",
                                                              "steps": "find it\nfix it"}))]),
        ChatResult(content="proposed"),
    ])
    assert agent.run_turn("plan the fix") == "proposed"
    assert plan_path(ws).is_file()
    assert "- [ ] find it" in read_plan(ws)


def test_plan_risk_classification_is_the_ordinary_one():
    """Plan mode leans on classify_command; a LOW misclassification would
    open the mode up. Spot-check the boundary."""
    from silkcode.permissions import classify_command
    assert classify_command("cat README.md") == Risk.LOW
    assert classify_command("python setup.py install") != Risk.LOW
