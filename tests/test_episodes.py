"""Retained episodes: iteration N+1 must not rediscover what N knew.

The swarm's workers used to return free-form reports that _summarize clipped
to a line; the next iteration started cold. Now each role's final message is
a retained episode (nac's idea: a compressed work record, not a
conversational reply), kept across iterations and fed forward as input.
"""

from __future__ import annotations

import json

import pytest
from conftest import sse_response

from silkcode.swarm import (
    _episode_of,
    _episodes_section,
    _tester_prompt,
    _worker_prompt,
    run_swarm,
)
from silkcode.workspace import Workspace


# ---- the episode extractor ---------------------------------------------------

def test_the_episode_block_is_what_is_retained():
    report = ("I looked at many things and thought many thoughts.\n\n"
              "EPISODE\n- goal: fix the suite\n- done: repaired mathutil\n"
              "- verified: pytest -q -> 12 passed\n- blocker: none\n- next: hygiene")
    episode = _episode_of(report)
    assert episode.startswith("EPISODE")
    assert "thought many thoughts" not in episode
    assert "pytest -q -> 12 passed" in episode


def test_a_model_that_ignores_the_contract_still_leaves_a_record():
    """Small models will ignore the format. The tail of the report is the
    closest thing to a work record; keeping nothing would be worse."""
    report = "Fixed the import in app.py and reran the tests; all green now."
    assert "all green now" in _episode_of(report)


def test_episodes_are_bounded():
    from silkcode.swarm import EPISODE_CHARS
    episode = _episode_of("EPISODE\n" + "x" * 10_000)
    assert len(episode) <= EPISODE_CHARS


def test_no_episodes_means_no_section():
    assert _episodes_section({}, "worker", "tester") == ""
    prompt = _tester_prompt(_fake_score())
    assert "Retained episodes" not in prompt


def _fake_score():
    from silkcode.swarm import Score
    return Score(score=2.0, tests=0.0, hygiene=2.0, tests_passed=0,
                 tests_failed=1, test_output="1 failed", detail="1 failed",
                 test_command="pytest -q")


def test_the_tester_sees_what_the_worker_did_last_round():
    episodes = {"worker": "EPISODE\n- done: created mathutil.py\n- verified: pytest -> 3 passed"}
    prompt = _tester_prompt(_fake_score(), episodes)
    assert "created mathutil.py" in prompt
    assert "do not rediscover" in prompt


def test_the_worker_sees_its_own_last_episode():
    episodes = {"worker": "EPISODE\n- next: add the edge-case test"}
    prompt = _worker_prompt({"critique": "", "suggestions": []}, _fake_score(), episodes)
    assert "add the edge-case test" in prompt


# ---- through a real two-iteration run -----------------------------------------

def test_iteration_two_receives_iteration_ones_worker_episode(tmp_path, stub_server,
                                                              monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_app.py").write_text(
        "from mathutil import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")

    worker_episode = ("EPISODE\n- goal: make the suite pass\n"
                      "- done: wrote a stub mathutil that still fails\n"
                      "- verified: pytest -q -> 1 failed\n"
                      "- blocker: add() returns 0\n- next: return a + b")
    fix = "def add(a, b):\n    return a + b\n"
    scripted = [
        # iteration 1: tester, critic, worker (worker writes a broken stub)
        sse_response(content="tests fail: no mathutil.\n\nEPISODE\n- done: diagnosed",
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(content=json.dumps({"critique": "missing module",
                                         "suggestions": [{"title": "create mathutil",
                                                          "detail": "add()"}]}),
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(tool_calls=[("write_file", json.dumps(
            {"path": "mathutil.py", "content": "def add(a, b):\n    return 0\n"}))],
            usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(content=worker_episode, usage={"prompt_tokens": 10, "completion_tokens": 5}),
        # iteration 2: tester, critic, worker (worker fixes it)
        sse_response(content="add() returns 0.\n\nEPISODE\n- done: confirmed the bug",
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(content=json.dumps({"critique": "fix add",
                                         "suggestions": [{"title": "return a+b",
                                                          "detail": "mathutil.add"}]}),
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(tool_calls=[("write_file", json.dumps({"path": "mathutil.py",
                                                            "content": fix}))],
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(content="EPISODE\n- done: fixed add\n- verified: pytest green",
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        result = run_swarm(Workspace(str(repo)), worker_spec="stub",
                           skip_tester_when_tests_pass=False, max_iterations=2)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    # Requests 5-8 are iteration 2. Its tester (request 5) must carry the
    # worker's iteration-1 episode - the whole point of retention.
    second_tester = server.requests[4]
    user_text = "\n".join(m["content"] for m in second_tester["messages"]
                          if m["role"] == "user")
    assert "wrote a stub mathutil that still fails" in user_text
    assert "return a + b" in user_text

    # and the noisy prose around the episode was not carried, only the record
    assert result.iterations == 2


def test_the_tester_and_worker_prompts_carry_the_contract(tmp_path, stub_server,
                                                          monkeypatch):
    """The critic is deliberately exempt - it must return strict JSON, and
    appending the episode format would corrupt it. Its suggestions already
    ARE its structured record."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_x.py").write_text("def test_x():\n    assert False\n")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")

    server = stub_server([
        sse_response(content="EPISODE\n- done: looked",
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
        sse_response(content=json.dumps({"critique": "x", "suggestions": []}),
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
        sse_response(content="EPISODE\n- done: attempted",
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
    ])
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        run_swarm(Workspace(str(repo)), worker_spec="stub", max_iterations=1,
                  skip_tester_when_tests_pass=False)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    systems = ["\n".join(m["content"] for m in request["messages"]
                         if m["role"] == "system")
               for request in server.requests]
    tester_system, critic_system, worker_system = systems[0], systems[1], systems[2]
    for contracted in (tester_system, worker_system):
        assert "retained episode" in contracted
        assert "Do not claim work is complete without verification evidence" in contracted
    assert "retained episode" not in critic_system


def test_episodes_land_in_the_saved_trace(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_x.py").write_text("def test_x():\n    assert False\n")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")

    server = stub_server([
        sse_response(content="EPISODE\n- done: looked", usage={"prompt_tokens": 5, "completion_tokens": 5}),
        sse_response(content=json.dumps({"critique": "x", "suggestions": []}),
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
        sse_response(content="EPISODE\n- done: attempted a fix",
                     usage={"prompt_tokens": 5, "completion_tokens": 5}),
    ])
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        result = run_swarm(Workspace(str(repo)), worker_spec="stub", max_iterations=1,
                           skip_tester_when_tests_pass=False)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    from pathlib import Path
    trace_files = list(Path(result.traces).glob("*.json"))
    assert trace_files
    trace = json.loads(trace_files[0].read_text())
    assert "episodes" in trace
    assert "attempted a fix" in trace["episodes"].get("worker", "")
