"""The harness adapter: `silkcode -p` as something a benchmark can drive.

An external harness needs three things from an agent it runs: a structured
trace of what happened, the final answer separated from the streamed noise,
and an exit code it can branch on without parsing anything. These tests pin
that contract - including the part where a provider being down is *not* a
task failure, because misfiling it as one corrupts a benchmark's numbers.
"""

from __future__ import annotations

import json

import pytest
from conftest import sse_response

from silkcode.cli.repl import run_repl


@pytest.fixture
def env(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def start(scripted):
        server = stub_server(scripted)
        server.thread.start()
        (home / "config.json").write_text(json.dumps({
            "default_model": "stub",
            "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                    "default_model": "stub-model"}},
        }))
        return server

    made = []

    def factory(scripted):
        server = start(scripted)
        made.append(server)
        return server

    yield workspace, factory
    for server in made:
        server.httpd.shutdown()
        server.httpd.server_close()


WRITES_A_FILE = lambda: [
    sse_response(tool_calls=[("write_file", json.dumps({"path": "hello.txt", "content": "hi"}))],
                 usage={"prompt_tokens": 10, "completion_tokens": 4}),
    sse_response(content="Created hello.txt", usage={"prompt_tokens": 15, "completion_tokens": 3}),
]


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_trace_records_what_happened_and_what_it_cost(env, tmp_path):
    workspace, factory = env
    factory(WRITES_A_FILE())
    trace = tmp_path / "out" / "trace.jsonl"

    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt",
                  trace_path=str(trace))

    assert rc == 0
    recorded = events(trace)
    kinds = [e["kind"] for e in recorded]
    assert "tool_start" in kinds and "tool_result" in kinds
    started = next(e for e in recorded if e["kind"] == "tool_start")
    assert started["name"] == "write_file"
    done = recorded[-1]
    assert done["kind"] == "done" and done["status"] == "success"
    assert done["total_tokens"] == done["prompt_tokens"] + done["completion_tokens"]
    assert done["total_tokens"] > 0
    assert all("ts" in e for e in recorded)


def test_streamed_text_is_folded_not_one_event_per_fragment(env, tmp_path):
    """The model streams a few characters at a time; a trace with a line per
    fragment is unreadable and enormous."""
    workspace, factory = env
    factory(WRITES_A_FILE())
    trace = tmp_path / "trace.jsonl"
    run_repl(str(workspace), None, "agent", prompt="create hello.txt",
             trace_path=str(trace))
    texts = [e for e in events(trace) if e["kind"] == "text"]
    assert len(texts) <= 2, texts
    assert any("Created hello.txt" in e["text"] for e in texts)


def test_the_final_answer_is_separated_from_the_stream(env, tmp_path):
    workspace, factory = env
    factory(WRITES_A_FILE())
    answer = tmp_path / "answer.txt"
    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt",
                  final_answer_path=str(answer))
    assert rc == 0
    assert answer.read_text() == "Created hello.txt"


def test_a_passing_check_is_exit_zero_and_a_failing_one_is_exit_one(env, tmp_path):
    workspace, factory = env
    factory(WRITES_A_FILE() + WRITES_A_FILE())

    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt",
                  check_command="cat hello.txt")
    assert rc == 0

    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt",
                  check_command="cat no-such-file.txt")
    assert rc == 1


def test_a_failed_check_is_a_task_failure_in_the_trace(env, tmp_path):
    workspace, factory = env
    factory(WRITES_A_FILE())
    trace = tmp_path / "trace.jsonl"
    rc = run_repl(str(workspace), None, "agent", prompt="create hello.txt",
                  trace_path=str(trace), check_command="cat no-such-file.txt")
    assert rc == 1
    done = events(trace)[-1]
    assert done["status"] == "task_failure"
    assert done["detail"], "the failing check's output should be in the trace"


def test_a_dead_provider_is_a_harness_error_not_a_task_failure(env, tmp_path):
    """Exit 2, distinct from exit 1: a benchmark that counts provider
    outages as failed tasks is measuring its network, not its model."""
    workspace, _factory = env
    home = tmp_path / "home"
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": "http://127.0.0.1:1",
                                "default_model": "stub-model", "retries": 0}},
    }))
    trace = tmp_path / "trace.jsonl"
    rc = run_repl(str(workspace), None, "agent", prompt="anything",
                  trace_path=str(trace))
    assert rc == 2
    done = events(trace)[-1]
    assert done["status"] == "harness_error"


def test_without_adapter_flags_the_old_exit_contract_is_unchanged(env, tmp_path):
    workspace, _factory = env
    (tmp_path / "home" / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": "http://127.0.0.1:1",
                                "default_model": "stub-model", "retries": 0}},
    }))
    assert run_repl(str(workspace), None, "agent", prompt="anything") == 1


def test_a_killed_run_still_leaves_its_events(tmp_path):
    """JSONL flushed per line: the trace of a crashed run is the interesting
    one, and it must not be sitting in a buffer when the process dies."""
    from silkcode.trace import TraceWriter

    trace = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace)
    writer.event("tool_start", {"name": "write_file", "args": {}})
    # no close, no done - read what is already on disk
    recorded = events(trace)
    assert recorded and recorded[0]["kind"] == "tool_start"


def test_the_cli_wires_the_flags_and_refuses_them_without_a_prompt(env, tmp_path, capsys):
    from silkcode.cli.main import main

    workspace, factory = env
    factory(WRITES_A_FILE())
    trace = tmp_path / "cli-trace.jsonl"
    answer = tmp_path / "cli-answer.txt"
    rc = main([str(workspace), "--mode", "agent", "-p", "create hello.txt",
               "--trace", str(trace), "--final-answer", str(answer)])
    assert rc == 0
    assert trace.is_file() and events(trace)[-1]["kind"] == "done"
    assert answer.read_text() == "Created hello.txt"

    with pytest.raises(SystemExit):
        main([str(workspace), "--trace", str(trace)])
    assert "--prompt" in capsys.readouterr().err
