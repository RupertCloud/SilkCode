"""Tests for the multi-agent improvement swarm (silkcode/swarm.py)."""

import json

import pytest

from silkcode.swarm import (
    _parse_critic,
    format_swarm_report,
    run_swarm,
    score_workspace,
)
from silkcode.workspace import Workspace
from conftest import sse_response


def _make_repo(tmp_path, buggy=True):
    """A tiny repo with a pytest suite that fails until mathutil.py exists."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math.py").write_text(
        "def test_add():\n"
        "    from mathutil import add\n"
        "    assert add(1, 2) == 3\n"
    )
    if not buggy:
        (tmp_path / "mathutil.py").write_text("def add(a, b):\n    return a + b\n")


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def test_score_workspace_passing(tmp_path):
    _make_repo(tmp_path, buggy=False)
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.score == 10.0
    assert score.tests == 8.0
    assert score.hygiene == 2.0
    assert score.tests_passed == 1
    assert score.tests_failed == 0


def test_score_workspace_failing(tmp_path):
    _make_repo(tmp_path, buggy=True)
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.tests == 0.0
    assert score.tests_failed == 1
    assert score.score == 2.0  # hygiene only


def test_score_workspace_no_tests(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.tests == 0.0
    assert score.test_command is None
    assert score.score == score.hygiene


def test_score_hygiene_todo_marker(tmp_path):
    _make_repo(tmp_path, buggy=False)
    (tmp_path / "app.py").write_text("# TODO: revisit this later\nprint('x')\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.hygiene == 1.0  # TODO marker costs one point
    assert score.score == 9.0


def test_score_hygiene_debug_marker(tmp_path):
    _make_repo(tmp_path, buggy=False)
    (tmp_path / "app.py").write_text("breakpoint()\nprint('x')\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.hygiene == 1.0  # debug marker costs one point


def test_score_ignores_generated_dirs(tmp_path):
    _make_repo(tmp_path, buggy=False)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("// TODO: fix me\nconsole.log(1)\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.hygiene == 2.0


# --------------------------------------------------------------------------- #
# Critic output parsing
# --------------------------------------------------------------------------- #

def test_parse_critic_json_with_fences():
    content = '```json\n{"critique": "needs work", "suggestions": [{"title": "Fix add", "detail": "make it sum"}]}\n```'
    parsed = _parse_critic(content)
    assert parsed["critique"] == "needs work"
    assert parsed["suggestions"][0]["title"] == "Fix add"


def test_parse_critic_plain_text_fallback():
    parsed = _parse_critic("I think the tests are failing.")
    assert parsed["critique"] == "I think the tests are failing."
    assert parsed["suggestions"]


def test_parse_critic_caps_suggestions():
    many = [{"title": f"s{i}", "detail": f"d{i}"} for i in range(20)]
    parsed = _parse_critic(json.dumps({"critique": "c", "suggestions": many}))
    assert len(parsed["suggestions"]) == 5


# --------------------------------------------------------------------------- #
# The swarm loop
# --------------------------------------------------------------------------- #

def _config(home, server):
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                "default_model": "stub-model"}},
    }))


def test_run_swarm_reaches_target(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    fix = "def add(a, b):\n    return a + b\n"
    scripted = [
        sse_response(content="The test fails: mathutil.add is missing. Create mathutil.py.",
                     usage={"prompt_tokens": 40, "completion_tokens": 12}),
        sse_response(content=json.dumps({
            "critique": "add() is missing so the suite fails",
            "suggestions": [{"title": "Implement mathutil.add",
                             "detail": "Create mathutil.py with def add(a, b): return a + b"}],
        }), usage={"prompt_tokens": 50, "completion_tokens": 20}),
        sse_response(tool_calls=[("write_file", json.dumps({"path": "mathutil.py", "content": fix}))],
                     usage={"prompt_tokens": 60, "completion_tokens": 15}),
        sse_response(content="Implemented mathutil.add and verified.", usage={"prompt_tokens": 30, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        result = run_swarm(Workspace(str(repo)), worker_spec="stub")
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "done"
    assert result.final_score == 10.0
    assert result.iterations == 2
    assert result.scores == [2.0, 10.0]
    assert (repo / "mathutil.py").read_text() == fix

    saved = json.loads(open(result.saved_to).read())
    assert saved["status"] == "done"
    assert len(list(__import__("pathlib").Path(result.traces).glob("*.json"))) == 1

    report = format_swarm_report(result)
    assert "done" in report and "10.0/10" in report


def test_run_swarm_stalls(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    refuse = sse_response(content="I will not modify anything.",
                          usage={"prompt_tokens": 10, "completion_tokens": 3})
    scripted = [
        refuse,  # tester (iteration 1)
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "fix", "detail": "fix the tests"}]}), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        refuse,  # worker refuses (iteration 1)
        refuse,  # tester (iteration 2)
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "fix", "detail": "fix the tests"}]}), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        refuse,  # worker refuses (iteration 2)
        refuse,  # tester (iteration 3)
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "fix", "detail": "fix the tests"}]}), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        refuse,  # worker refuses (iteration 3)
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        result = run_swarm(Workspace(str(repo)), worker_spec="stub", stall_limit=2)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "stalled"
    assert result.iterations == 3
    assert result.final_score == 2.0
    assert "stuck" in result.detail


def test_run_swarm_max_iterations(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    refuse = sse_response(content="No.", usage={"prompt_tokens": 5, "completion_tokens": 1})
    scripted = [
        refuse,  # tester
        sse_response(content=json.dumps({"critique": "c", "suggestions": []}),
                     usage={"prompt_tokens": 5, "completion_tokens": 1}),
        refuse,  # worker
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        result = run_swarm(Workspace(str(repo)), worker_spec="stub", max_iterations=1)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "max-iterations"
    assert result.iterations == 1


def test_run_swarm_validates_target():
    with pytest.raises(ValueError):
        run_swarm(Workspace("."), "stub", target=11.0)
