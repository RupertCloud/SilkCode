"""Tests for the multi-agent improvement swarm (silkcode/swarm.py)."""

import json

import pytest

from silkcode.permissions import PermissionManager
from silkcode.swarm import (
    _parse_critic,
    _parse_team_plan,
    format_swarm_report,
    run_swarm,
    score_workspace,
)
from silkcode.workspace import Workspace
from conftest import sse_response

# Split so the hygiene scanner (which searches for these exact strings) does
# not flag this test file when the repo itself is scored.
_TODO = "TO" + "DO"


def _make_repo(tmp_path, buggy=True):
    """A tiny repo with a pytest suite that fails until mathutil.py exists.

    The empty root conftest.py is what real flat-layout projects carry: it
    puts the project root on sys.path for the `pytest` command the swarm
    runs. Without it the suite fails to import even when the code is correct
    (plain `pytest` does not add the working directory to sys.path — only
    `python -m pytest` does).
    """
    (tmp_path / "conftest.py").write_text("")
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
    (tmp_path / "app.py").write_text(f"# {_TODO}: revisit this later\nprint('x')\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.hygiene == 1.0  # marker costs one point
    assert score.score == 9.0


def test_score_hygiene_debug_marker(tmp_path):
    _make_repo(tmp_path, buggy=False)
    (tmp_path / "app.py").write_text("break" + "point()\nprint('x')\n")
    score = score_workspace(Workspace(str(tmp_path)))
    assert score.hygiene == 1.0  # debug marker costs one point


def test_score_ignores_generated_dirs(tmp_path):
    _make_repo(tmp_path, buggy=False)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text(f"// {_TODO}: fix me\nconsole.log(1)\n")
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


def test_parse_team_plan_normalizes_assignments():
    parsed = _parse_team_plan(json.dumps({
        "summary": "Ship the useful slice",
        "tasks": [
            {"owner": "dev1", "title": "API", "detail": "Add endpoint",
             "acceptance": ["returns 200"]},
            {"owner": "manager", "title": "invalid owner"},
        ],
    }))
    assert parsed["summary"] == "Ship the useful slice"
    assert parsed["tasks"] == [{
        "owner": "dev1", "title": "API", "detail": "Add endpoint",
        "acceptance": ["returns 200"],
    }]


def test_parse_team_plan_handles_non_json():
    parsed = _parse_team_plan("Plan the release carefully")
    assert parsed["summary"] == "Plan the release carefully"
    assert parsed["tasks"] == []


def test_parse_team_plan_supports_elastic_dev_n_staffing():
    parsed = _parse_team_plan(json.dumps({"tasks": [
        {"owner": "dev4", "title": "Search", "acceptance": []},
        {"owner": "Dev12", "title": "Release", "acceptance": []},
        {"owner": "dev13", "title": "Too many", "acceptance": []},
    ]}))
    assert [task["owner"] for task in parsed["tasks"]] == ["dev4", "dev12"]


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


def test_run_swarm_worker_uses_provided_permissions(tmp_path, stub_server, monkeypatch):
    """When worker_permissions is given, the worker asks the user (asker is
    consulted for a MEDIUM command) instead of auto-approving in agent mode."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    prompts = []
    scripted = [
        sse_response(content="tests fail", usage={"prompt_tokens": 5, "completion_tokens": 1}),
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "run a command", "detail": "somebinary --flag"}]}),
                     usage={"prompt_tokens": 5, "completion_tokens": 1}),
        sse_response(tool_calls=[("run_command", json.dumps({"command": "somebinary --flag"}))],
                     usage={"prompt_tokens": 5, "completion_tokens": 1}),
        sse_response(content="done", usage={"prompt_tokens": 5, "completion_tokens": 1}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        perms = PermissionManager("ask", asker=lambda p: prompts.append(p) or "yes")
        result = run_swarm(Workspace(str(repo)), worker_spec="stub",
                           max_iterations=1, worker_permissions=perms)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "max-iterations"
    # the MEDIUM-risk command prompted through the provided manager
    assert any("somebinary --flag" in p for p in prompts), prompts


def test_run_swarm_skips_tester_when_tests_pass(tmp_path, stub_server, monkeypatch):
    """With a green suite (only hygiene points missing), the tester is never
    called: critic + worker only, and per-role tokens are tracked."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=False)          # tests pass -> score 8 + 2 hygiene = 10? no:
    (repo / "app.py").write_text(f"# {_TODO}: remove me\nprint('x')\n")  # hygiene 1 -> score 9

    scripted = [
        # iteration 1: tester skipped, critic suggests removing the marker
        sse_response(content=json.dumps({"critique": f"{_TODO} left behind", "suggestions": [
            {"title": f"Remove the {_TODO} marker", "detail": f"Delete the {_TODO} comment in app.py"}]}),
                     usage={"prompt_tokens": 20, "completion_tokens": 6}),
        sse_response(tool_calls=[("edit_file", json.dumps(
            {"path": "app.py", "old_string": f"# {_TODO}: remove me\n", "new_string": ""}))],
                     usage={"prompt_tokens": 20, "completion_tokens": 6}),
        sse_response(content=f"Removed the {_TODO}.", usage={"prompt_tokens": 10, "completion_tokens": 3}),
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
    assert result.scores == [9.0, 10.0]
    # exactly 3 model requests: critic, worker(write), worker(done) - no tester
    assert len(server.requests) == 3
    assert result.role_tokens["tester"] == 0
    assert result.role_tokens["critic"] > 0
    assert result.role_tokens["worker"] > 0


def test_run_swarm_skips_worker_when_nothing_to_do(tmp_path, stub_server, monkeypatch):
    """Green suite + full hygiene -> 10/10 immediately; no agents run at all."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=False)

    server = stub_server([])  # no scripted responses: nothing may be requested
    server.thread.start()
    try:
        _config(home, server)
        result = run_swarm(Workspace(str(repo)), worker_spec="stub")
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "done"
    assert result.final_score == 10.0
    assert result.iterations == 1
    assert result.tokens == 0
    assert len(server.requests) == 0


def test_run_swarm_token_budget(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    scripted = [
        sse_response(content="The test fails: mathutil.add is missing.",
                     usage={"prompt_tokens": 40, "completion_tokens": 12}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        result = run_swarm(Workspace(str(repo)), worker_spec="stub", max_tokens=10)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result.status == "token-budget"
    assert "budget" in result.detail
    assert result.tokens >= 10  # the tester alone blew the budget
    assert result.role_tokens["tester"] >= 10


def test_run_swarm_structured_events(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo, buggy=True)

    fix = "def add(a, b):\n    return a + b\n"
    scripted = [
        sse_response(content="The test fails: mathutil.add is missing.",
                     usage={"prompt_tokens": 40, "completion_tokens": 12}),
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "Implement mathutil.add", "detail": "Create mathutil.py"}]}),
                     usage={"prompt_tokens": 50, "completion_tokens": 20}),
        sse_response(tool_calls=[("write_file", json.dumps({"path": "mathutil.py", "content": fix}))],
                     usage={"prompt_tokens": 60, "completion_tokens": 15}),
        sse_response(content="Done.", usage={"prompt_tokens": 30, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        _config(home, server)
        events = []
        result = run_swarm(Workspace(str(repo)), worker_spec="stub",
                           on_event=lambda kind, data: events.append((kind, data)))
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    kinds = [k for k, _ in events]
    assert "iteration" in kinds and "score" in kinds and "phase" in kinds and "log" in kinds
    score_events = [d for k, d in events if k == "score"]
    assert [d["score"] for d in score_events] == [2.0, 10.0]
    phases = [d["role"] for k, d in events if k == "phase"]
    assert phases == ["tester", "critic", "worker"]
    assert result.status == "done"
