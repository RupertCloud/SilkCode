"""Advisory per-workspace lock tests (silkcode/lock.py) + file-tool integration."""

import json
import os
import time

import pytest

from silkcode.lock import (
    LOCK_RELPATH,
    LOCK_STALE_SECONDS,
    LockError,
    acquire,
    is_stale,
    owner_of,
    read_lock,
    release,
)
from silkcode.tools.files import edit_file, write_file
from silkcode.workspace import ToolError, Workspace


def test_acquire_and_release(tmp_path):
    acquire(tmp_path, "session-1")
    assert owner_of(tmp_path) == "session-1"
    assert release(tmp_path, "session-1") is True
    assert owner_of(tmp_path) is None


def test_acquire_refused_when_locked_by_other(tmp_path):
    acquire(tmp_path, "session-1")
    with pytest.raises(LockError, match="locked by session-1"):
        acquire(tmp_path, "session-2")


def test_acquire_same_owner_refreshes(tmp_path):
    acquire(tmp_path, "session-1")
    first = read_lock(tmp_path)["acquired_at"]
    time.sleep(0.01)
    acquire(tmp_path, "session-1")  # same owner: refresh, not refused
    assert read_lock(tmp_path)["acquired_at"] > first


def test_stale_lock_taken_over(tmp_path):
    acquire(tmp_path, "session-1")
    lock = read_lock(tmp_path)
    lock["acquired_at"] = time.time() - LOCK_STALE_SECONDS - 10
    (tmp_path / LOCK_RELPATH).write_text(json.dumps(lock))
    assert is_stale(read_lock(tmp_path))
    acquire(tmp_path, "session-2")  # stale -> takeover
    assert owner_of(tmp_path) == "session-2"


def test_dead_owner_lock_is_stale_immediately(tmp_path):
    """A lock whose owner process is gone must not block anyone for the whole
    staleness window - a dead process can never write again."""
    acquire(tmp_path, "session-1")
    lock = read_lock(tmp_path)
    lock["pid"] = 2**31 - 1  # a pid that cannot exist on this system
    lock["acquired_at"] = time.time()  # fresh timestamp on purpose
    (tmp_path / LOCK_RELPATH).write_text(json.dumps(lock))
    assert is_stale(read_lock(tmp_path))
    acquire(tmp_path, "session-2")  # takeover works despite the fresh timestamp
    assert owner_of(tmp_path) == "session-2"


def test_live_owner_lock_is_not_stale(tmp_path):
    acquire(tmp_path, "session-1")
    lock = read_lock(tmp_path)
    lock["pid"] = os.getpid()  # this test process is alive
    lock["acquired_at"] = time.time()
    (tmp_path / LOCK_RELPATH).write_text(json.dumps(lock))
    assert not is_stale(read_lock(tmp_path))
    with pytest.raises(LockError, match="locked by session-1"):
        acquire(tmp_path, "session-2")


def test_lock_state_describes_lock_for_gui(tmp_path):
    from silkcode.lock import lock_state
    assert lock_state(tmp_path) == {"held": False}
    acquire(tmp_path, "session-1")
    lock = read_lock(tmp_path)
    lock["pid"] = 2**31 - 1  # dead owner
    (tmp_path / LOCK_RELPATH).write_text(json.dumps(lock))
    st = lock_state(tmp_path)
    assert st["held"] is True
    assert st["owner"] == "session-1"
    assert st["alive"] is False
    assert st["stale"] is True
    assert st["age"] is not None


def test_release_only_by_owner(tmp_path):
    acquire(tmp_path, "session-1")
    assert release(tmp_path, "session-2") is False
    assert owner_of(tmp_path) == "session-1"


# ---- file-tool integration --------------------------------------------------

def test_write_refused_when_locked_by_other(tmp_path):
    ws = Workspace(str(tmp_path))
    acquire(tmp_path, "session-1")
    with pytest.raises(ToolError, match="workspace locked by session-1"):
        write_file(ws, "a.py", "x = 1\n", _owner="session-2")


def test_edit_refused_when_locked_by_other(tmp_path):
    ws = Workspace(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n")
    acquire(tmp_path, "session-1")
    with pytest.raises(ToolError, match="workspace locked"):
        edit_file(ws, "a.py", "x = 1", "x = 2", _owner="session-2")


def test_owner_write_allowed_and_refreshes(tmp_path):
    ws = Workspace(str(tmp_path))
    acquire(tmp_path, "session-1")
    write_file(ws, "a.py", "x = 1\n", _owner="session-1")
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    edit_file(ws, "a.py", "x = 1", "x = 2", _owner="session-1")
    assert "x = 2" in (tmp_path / "a.py").read_text()
    # the owner's writes refreshed the lock timestamp
    assert owner_of(tmp_path) == "session-1"


def test_write_without_owner_refused_when_locked(tmp_path):
    ws = Workspace(str(tmp_path))
    acquire(tmp_path, "session-1")
    with pytest.raises(ToolError, match="workspace locked"):
        write_file(ws, "a.py", "x = 1\n")  # no owner, held lock -> refused


def test_stale_lock_allows_write_and_takeover(tmp_path):
    ws = Workspace(str(tmp_path))
    acquire(tmp_path, "session-1")
    lock = read_lock(tmp_path)
    lock["acquired_at"] = time.time() - LOCK_STALE_SECONDS - 10
    (tmp_path / LOCK_RELPATH).write_text(json.dumps(lock))
    write_file(ws, "a.py", "x = 1\n", _owner="session-2")  # stale -> allowed
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert owner_of(tmp_path) == "session-2"  # write took the lock over


def test_release_unlocks_writes(tmp_path):
    ws = Workspace(str(tmp_path))
    acquire(tmp_path, "session-1")
    with pytest.raises(ToolError, match="workspace locked"):
        write_file(ws, "a.py", "x = 1\n", _owner="session-2")
    release(tmp_path, "session-1")
    write_file(ws, "a.py", "x = 1\n", _owner="session-2")  # now free
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
