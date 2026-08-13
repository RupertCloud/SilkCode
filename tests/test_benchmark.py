import json

import pytest

from silkcode.benchmark import TASKS, format_results, run_benchmark, run_task
from silkcode.providers.openai_compat import OpenAICompatProvider
from silkcode.workspace import Workspace
from conftest import sse_response


def apply_files(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_reference_solutions_pass_checks(task, tmp_path):
    apply_files(tmp_path, task.files)
    apply_files(tmp_path, task.solution)
    passed, detail = task.check(Workspace(tmp_path), task)
    assert passed, f"{task.id}: {detail}"


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_unsolved_tasks_fail_checks(task, tmp_path):
    apply_files(tmp_path, task.files)
    passed, _detail = task.check(Workspace(tmp_path), task)
    assert not passed


def test_protected_file_tampering_fails(tmp_path):
    task = next(t for t in TASKS if t.id == "fix-bug")
    apply_files(tmp_path, task.files)
    apply_files(tmp_path, task.solution)
    # cheat: weaken the checker instead of fixing the bug
    (tmp_path / "check_stats.py").write_text("print('ok')\n")
    passed, detail = task.check(Workspace(tmp_path), task)
    assert not passed
    assert "protected" in detail


def test_run_benchmark_end_to_end(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))

    solution = TASKS[0].solution["hello.py"]
    scripted = [
        sse_response(tool_calls=[("write_file", json.dumps({"path": "hello.py", "content": solution}))],
                     usage={"prompt_tokens": 50, "completion_tokens": 20}),
        sse_response(content="Done.", usage={"prompt_tokens": 60, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        results = run_benchmark(["stub"], task_ids=["hello-cli"])
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    entry = results["models"][0]
    assert entry["model"] == "stub/stub-model"
    assert entry["solved"] == 1 and entry["total"] == 1
    assert entry["tokens"] == 135
    assert entry["tasks"][0]["steps"] == 1

    saved = json.loads((home / "benchmarks" / f"benchmark-{int(results['started'])}.json").read_text())
    assert saved["models"][0]["solved"] == 1

    table = format_results(results)
    assert "stub/stub-model" in table
    assert "1/1" in table


def test_run_benchmark_ab_mode(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))

    solution = TASKS[0].solution["hello.py"]
    turn = [
        sse_response(tool_calls=[("write_file", json.dumps({"path": "hello.py", "content": solution}))],
                     usage={"prompt_tokens": 50, "completion_tokens": 20}),
        sse_response(content="Done.", usage={"prompt_tokens": 60, "completion_tokens": 5}),
    ]
    server = stub_server(turn * 2)  # two conditions, one turn each
    server.thread.start()
    try:
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        results = run_benchmark(["stub"], task_ids=["hello-cli"], ab=True)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert [e["condition"] for e in results["models"]] == ["baseline", "harness"]
    assert all(e["solved"] == 1 for e in results["models"])
    # baseline condition really ran without the repo map in the system prompt
    baseline_req, harness_req = server.requests[0], server.requests[2]
    assert "Repository map:" not in baseline_req["messages"][0]["content"]
    assert "Repository map:" in harness_req["messages"][0]["content"]
    # full traces captured for every run
    from pathlib import Path
    traces = sorted(Path(results["traces"]).glob("*.json"))
    assert len(traces) == 2
    trace = json.loads(traces[0].read_text())
    assert trace["result"]["task"] == "hello-cli"
    assert any(m["role"] == "tool" for m in trace["messages"])


def test_run_task_records_failure(tmp_path, stub_server, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    scripted = [sse_response(content="I refuse.", usage={"prompt_tokens": 10, "completion_tokens": 2})]
    server = stub_server(scripted)
    server.thread.start()
    try:
        provider = OpenAICompatProvider("stub", server.base_url)
        result = run_task(provider, "stub-model", TASKS[0])
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
    assert result["passed"] is False
    assert result["tokens"] == 12
