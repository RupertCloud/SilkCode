"""Benchmark tasks mined from repository history (silkcode/histbench.py)."""

import json
import subprocess

import pytest

from silkcode.histbench import (
    DIFFICULTIES,
    candidates,
    clean_instruction,
    difficulty_of,
    is_test_path,
    load_tasks,
    materialize,
    mine,
    save_tasks,
    to_bench_task,
)
from silkcode.workspace import ToolError, Workspace

# an implementation big enough to clear the "easy" threshold (>=20 lines)
WIDGET = "\n".join([f"def helper_{i}(x):\n    return x + {i}\n" for i in range(12)])
WIDGET_TESTS = (
    "from widget import helper_0, helper_11\n\n"
    "def test_helpers():\n"
    "    assert helper_0(1) == 1\n"
    "    assert helper_11(1) == 12\n"
)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A repo with three changes: a valid task, a tests-already-pass change,
    and a docs-only change."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "dev@example.com")
    git(root, "config", "user.name", "Dev")
    # baseline: a project with a test runner but no widget yet
    (root / "conftest.py").write_text("")
    (root / "README.md").write_text("# project\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_baseline.py").write_text("def test_ok():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Baseline project")

    # change 1: real task — tests + implementation together
    (root / "widget.py").write_text(WIDGET)
    (root / "tests" / "test_widget.py").write_text(WIDGET_TESTS)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Add widget helpers\n\nHelpers used by the dashboard.\n\n"
        "Co-Authored-By: Someone <a@b.c>\nX-Silk-Model: deepseek/deepseek-chat")

    # change 2: a substantial implementation change (so it clears the size
    # threshold) whose new test passes without any of it — must be rejected
    # by the fail-before check, not by size
    (root / "tests" / "test_trivial.py").write_text("def test_math():\n    assert 1 + 1 == 2\n")
    (root / "extra.py").write_text(
        "\n".join(f"def extra_{i}(x):\n    return x * {i}\n" for i in range(12)))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Add a test that already passes")

    # change 3: documentation only — no tests, must not be a candidate
    (root / "README.md").write_text("# project\n\nDocs.\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Document the project")
    return root


# --------------------------------------------------------------------------- #
# Classification helpers
# --------------------------------------------------------------------------- #

def test_is_test_path():
    for path in ("tests/test_x.py", "src/foo_test.py", "spec/thing.spec.ts",
                 "web/__tests__/a.test.tsx", "pkg/handler_test.go", "conftest.py"):
        assert is_test_path(path), path
    for path in ("silkcode/agent/loop.py", "README.md", "src/testing.py",
                 "docs/latest.md"):
        assert not is_test_path(path), path


def test_difficulty_tiers():
    assert difficulty_of(19, 1) is None          # too small to be a task
    assert difficulty_of(20, 1) == "easy"
    assert difficulty_of(60, 1) == "easy"        # lines alone are not enough
    assert difficulty_of(60, 2) == "medium"
    assert difficulty_of(120, 3) == "hard"
    assert set(DIFFICULTIES) == {"easy", "medium", "hard"}


def test_clean_instruction_strips_merge_boilerplate_and_trailers():
    text = clean_instruction(
        "Merge pull request #10 from acme/feature",
        "Add widget helpers\n\nThey power the dashboard.\n\n"
        "Co-Authored-By: X <x@y.z>\nX-Silk-Model: deepseek/deepseek-chat\n"
        "🤖 Generated with something\nhttps://claude.ai/code/session_x",
    )
    assert text.startswith("Add widget helpers")
    assert "They power the dashboard." in text
    assert "Co-Authored-By" not in text and "X-Silk-Model" not in text
    assert "claude.ai" not in text

    titled = clean_instruction("Merge pull request #7: Fix the redirect", "")
    assert titled == "Fix the redirect"

    plain = clean_instruction("Add widget helpers", "")
    assert plain == "Add widget helpers"


# --------------------------------------------------------------------------- #
# Mining
# --------------------------------------------------------------------------- #

def test_candidates_require_tests_and_implementation(repo):
    found = candidates(repo, scan=20)
    subjects = [c.subject for c in found]
    assert "Add widget helpers" in subjects
    assert "Document the project" not in subjects   # no tests touched
    assert any(c.impl_lines >= 20 for c in found)


def test_mine_validates_fail_before_pass_after(repo):
    messages = []
    tasks = mine(repo, limit=5, scan=20, on_progress=messages.append)

    ids = {t.subject for t in tasks}
    assert "Add widget helpers" in ids
    # the change whose tests pass without it is rejected by validation
    assert "Add a test that already passes" not in ids
    assert any("already pass" in m for m in messages)

    task = next(t for t in tasks if t.subject == "Add widget helpers")
    assert task.difficulty in DIFFICULTIES
    assert task.test_paths == ["tests/test_widget.py"]
    assert "pytest" in task.test_command
    assert task.instruction.startswith("Add widget helpers")
    assert "Co-Authored-By" not in task.instruction
    assert task.impl_files >= 1 and task.impl_lines >= 20
    assert task.id.startswith("hist-")


def test_mine_rejects_non_git_directory(tmp_path):
    with pytest.raises(ToolError, match="not a git repository"):
        mine(tmp_path)


def test_mine_rejects_unknown_difficulty(repo):
    with pytest.raises(ToolError, match="unknown difficulty"):
        mine(repo, difficulties=["trivial"])


def test_mine_difficulty_filter(repo):
    assert mine(repo, limit=5, scan=20, difficulties=["hard"]) == []


def test_save_and_load_roundtrip(repo, tmp_path):
    tasks = mine(repo, limit=1, scan=20)
    assert tasks
    path = save_tasks(tasks, tmp_path / "tasks.json")
    assert json.loads(path.read_text())[0]["id"] == tasks[0].id
    assert [t.to_json() for t in load_tasks(path)] == [t.to_json() for t in tasks]


# --------------------------------------------------------------------------- #
# Task execution shape
# --------------------------------------------------------------------------- #

def test_materialize_states(repo, tmp_path):
    task = mine(repo, limit=1, scan=20)[0]

    before = tmp_path / "before"
    materialize(repo, task.base_commit, task.head_commit, task.test_paths, before)
    assert (before / "tests" / "test_widget.py").exists()   # the tests are present
    assert not (before / "widget.py").exists()              # the implementation is not

    after = tmp_path / "after"
    materialize(repo, task.base_commit, task.head_commit, task.test_paths, after,
                with_implementation=True)
    assert (after / "widget.py").exists()


def test_to_bench_task_checks_tests_and_protects_them(repo, tmp_path):
    mined = mine(repo, limit=1, scan=20)[0]
    bench = to_bench_task(mined)

    assert bench.id == mined.id
    assert mined.instruction.splitlines()[0] in bench.prompt
    assert "Do not modify the test files" in bench.prompt
    assert bench.protected == ("tests/test_widget.py",)

    # unsolved starting state fails the check
    start = tmp_path / "start"
    bench.setup(start)
    ws = Workspace(str(start))
    passed, _ = bench.check(ws, bench)
    assert not passed

    # applying the real implementation passes it
    (start / "widget.py").write_text(WIDGET)
    passed, detail = bench.check(ws, bench)
    assert passed, detail

    # deleting the tests to "pass" is caught
    (start / "tests" / "test_widget.py").write_text("def test_nothing():\n    pass\n")
    passed, detail = bench.check(ws, bench)
    assert not passed
    assert "protected" in detail


def test_mined_task_runs_through_the_benchmark_runner(repo, stub_server, tmp_path,
                                                      monkeypatch):
    """A mined task is executed by the real runner: snapshot -> agent -> check."""
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.benchmark import run_task
    from silkcode.providers.openai_compat import OpenAICompatProvider
    from conftest import sse_response

    bench = to_bench_task(mine(repo, limit=1, scan=20)[0])
    scripted = [
        sse_response(tool_calls=[("write_file", json.dumps(
            {"path": "widget.py", "content": WIDGET}))],
            usage={"prompt_tokens": 100, "completion_tokens": 20}),
        sse_response(content="Implemented the helpers.",
                     usage={"prompt_tokens": 120, "completion_tokens": 10}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        provider = OpenAICompatProvider("stub", server.base_url)
        result = run_task(provider, "stub-model", bench)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

    assert result["passed"] is True, result["detail"]
    assert result["task"] == bench.id
    assert result["tokens"] == 250
    # the agent saw the repository snapshot, not an empty directory
    system_prompt = server.requests[0]["messages"][0]["content"]
    assert "Repository map:" in system_prompt
    assert "tests" in system_prompt
