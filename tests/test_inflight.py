"""Durable in-flight command records: honest answers after a dead session.

The local backend records every command before it starts and clears the
record when there is an outcome to report. A record left behind with no live
process is an orphan, and the next session reports it - unreal-agent's
taxonomy: "never started" and "interrupted before an exit status" are
different answers, and the captured output comes along.
"""

import json
import subprocess

from silkcode import inflight
from silkcode.context import assemble
from silkcode.execbackend import LocalBackend
from silkcode.workspace import Workspace


def inflight_dir(root):
    return root / ".silkcode" / "inflight"


def make_orphan(root, command="pytest -q", pid=None, output=""):
    record = inflight.begin(root, command)
    if pid is not None:
        inflight.started(record, pid)
    if output:
        record.out_path.write_text(output)
    return record


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ---- the normal life of a command: no trace left ------------------------------

def test_a_completed_command_leaves_no_records(tmp_path):
    ws = Workspace(tmp_path)
    result = LocalBackend().exec(ws, "echo hello; echo oops >&2")
    assert result.startswith("exit code: 0\n")
    assert "hello" in result and "oops" in result
    assert not list(inflight_dir(tmp_path).glob("*"))


def test_a_timed_out_command_reports_and_leaves_no_records(tmp_path):
    ws = Workspace(tmp_path)
    result = LocalBackend().exec(ws, "sleep 30", timeout=1)
    assert "timed out after 1 seconds" in result
    assert not list(inflight_dir(tmp_path).glob("*"))


def test_output_format_is_unchanged(tmp_path):
    ws = Workspace(tmp_path)
    assert LocalBackend().exec(ws, "true") == "exit code: 0\n(no output)"
    assert LocalBackend().exec(ws, "exit 3") == "exit code: 3\n(no output)"


def test_the_records_directory_is_git_ignored(tmp_path):
    LocalBackend().exec(Workspace(tmp_path), "true")
    assert (tmp_path / ".silkcode" / ".gitignore").exists()


# ---- orphans: what a dead session left behind ---------------------------------

def test_a_dead_process_is_reported_with_its_output(tmp_path):
    make_orphan(tmp_path, "pytest -q", pid=dead_pid(),
                output="collected 12 items\n\ntests/test_x.py ....F")
    notices = inflight.report(tmp_path)
    assert len(notices) == 1
    assert "pytest -q" in notices[0]
    assert "interrupted before an exit status was recorded" in notices[0]
    assert "tests/test_x.py ....F" in notices[0]


def test_a_command_that_never_started_says_so(tmp_path):
    make_orphan(tmp_path, "rm -rf build", pid=None)
    notices = inflight.report(tmp_path)
    assert "may not have run at all" in notices[0]
    assert "re-running anything destructive" in notices[0]


def test_a_live_process_is_not_an_orphan(tmp_path):
    import os
    make_orphan(tmp_path, "long build", pid=os.getpid())
    assert inflight.report(tmp_path) == []
    assert list(inflight_dir(tmp_path).glob("*.json")), "live record was consumed"


def test_reporting_consumes_the_orphan(tmp_path):
    make_orphan(tmp_path, "make", pid=dead_pid())
    assert inflight.report(tmp_path)
    assert inflight.report(tmp_path) == []
    assert not list(inflight_dir(tmp_path).glob("*"))


def test_a_corrupt_record_is_cleared_without_a_claim(tmp_path):
    directory = inflight_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "x.json").write_text("{ half a rec")
    assert inflight.report(tmp_path) == []
    assert not list(directory.glob("*.json"))


def test_long_output_is_tailed_not_dumped(tmp_path):
    make_orphan(tmp_path, "verbose", pid=dead_pid(),
                output="x" * 9000 + "THE END")
    notice = inflight.report(tmp_path)[0]
    assert "THE END" in notice
    assert len(notice) < 2500


def test_records_are_versioned(tmp_path):
    record = inflight.begin(tmp_path, "true")
    assert json.loads(record.path.read_text())["version"] == 1


# ---- the user actually hears about it -----------------------------------------

def test_assemble_surfaces_the_orphan_as_a_warning_once(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    make_orphan(tmp_path, "npm run build", pid=dead_pid(), output="Bundling…")
    ws = Workspace(tmp_path)
    first = assemble(ws)
    assert any("npm run build" in w for w in first.warnings)
    assert any("Bundling…" in w for w in first.warnings)
    assert not any("npm run build" in w for w in assemble(ws).warnings)
