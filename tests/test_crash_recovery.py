"""What survives a crash: kill Silk Code at the worst moment and look.

unreal-agent fuzzes fault injection over its whole recovery loop; our durable
state is smaller - session files, in-flight command records - so these tests
walk the interesting moments directly: die mid-save, die mid-command, come
back, and check that nothing lies and nothing is lost.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from silkcode import inflight
from silkcode.execbackend import LocalBackend
from silkcode.sessions import SessionStore, new_session
from silkcode.workspace import Workspace


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


# ---- session files -------------------------------------------------------------

def test_a_crash_mid_save_keeps_the_previous_conversation(store, monkeypatch):
    data = new_session(1, title="precious", model="m", cwd=".", mode="edit")
    data["messages"] = [{"role": "user", "content": "the old history"}]
    store.save(data)

    # die between writing the temp file and swapping it into place
    def crash(src, dst):
        raise OSError("killed")
    monkeypatch.setattr(os, "replace", crash)
    data["messages"].append({"role": "user", "content": "never landed"})
    with pytest.raises(OSError):
        store.save(data)
    monkeypatch.undo()

    saved = store.load(1)
    assert [m["content"] for m in saved["messages"]] == ["the old history"]
    assert json.loads(store._path(1).read_text())  # intact, parseable JSON


def test_a_half_written_session_file_does_not_poison_the_store(store):
    store.save(new_session(1, title="fine", model="m", cwd=".", mode="edit"))
    (store.dir / "2.json").write_text('{"id": 2, "title": "torn')  # a torn write

    listed = store.list()
    assert [s["id"] for s in listed] == [1]
    with pytest.raises(ValueError):
        store.load(2)
    # id allocation keeps counting past the corpse instead of reusing its id
    assert store.new_id() > 2


def test_temp_files_from_interrupted_saves_are_invisible(store):
    store.save(new_session(3, title="t", model="m", cwd=".", mode="edit"))
    (store.dir / "4.tmp").write_text('{"id": 4}')  # a save that never finished
    assert [s["id"] for s in store.list()] == [3]


# ---- in-flight commands ---------------------------------------------------------

def test_a_command_killed_with_its_session_is_reported_next_time(tmp_path):
    """The real thing: a command is running, the whole process tree dies
    (SIGKILL - no cleanup, exactly like a crash), and the next session in the
    workspace finds out what was in flight and what it printed."""
    ws_root = tmp_path / "repo"
    ws_root.mkdir()
    runner = (
        "from silkcode.execbackend import LocalBackend\n"
        "from silkcode.workspace import Workspace\n"
        "import sys\n"
        f"LocalBackend().exec(Workspace({str(ws_root)!r}),"
        " 'echo progress was made; sleep 30', timeout=60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", runner],
                            cwd=os.path.dirname(os.path.dirname(__file__)))
    # wait until the record says the command started
    deadline = time.time() + 10
    directory = ws_root / ".silkcode" / "inflight"
    while time.time() < deadline:
        records = list(directory.glob("*.json")) if directory.exists() else []
        if records and json.loads(records[0].read_text()).get("pid"):
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("the in-flight record never appeared")

    proc.kill()  # the harness dies; the child sleep is orphaned or dies with it
    proc.wait()
    # the recorded shell may outlive its parent briefly; wait for it to go
    pid = json.loads(records[0].read_text())["pid"]
    subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    for _ in range(100):
        if not inflight._alive(pid):
            break
        time.sleep(0.05)

    notices = inflight.report(ws_root)
    assert len(notices) == 1
    assert "sleep 30" in notices[0]
    assert "interrupted before an exit status" in notices[0]
    assert "progress was made" in notices[0]


def test_recovery_after_crash_then_normal_runs_stay_clean(tmp_path):
    """After an orphan is reported, the workspace is clean and new commands
    behave normally - the crash leaves no permanent residue."""
    from test_inflight import dead_pid, make_orphan
    make_orphan(tmp_path, "make", pid=dead_pid())
    assert inflight.report(tmp_path)
    ws = Workspace(tmp_path)
    assert LocalBackend().exec(ws, "echo ok").startswith("exit code: 0")
    assert inflight.report(tmp_path) == []
