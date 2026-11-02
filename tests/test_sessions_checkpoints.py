import json
import os
import threading
from pathlib import Path

import pytest

from silkcode.checkpoints import Checkpoints
from silkcode.sessions import SessionStore, new_session


def test_session_roundtrip(tmp_path):
    store = SessionStore(tmp_path)
    assert store.new_id() == 1
    session = new_session(1, "Fix bug", "deepseek", "/repo", "ask")
    session["messages"] = [{"role": "user", "content": "hi"}]
    store.save(session)

    loaded = store.load(1)
    assert loaded["title"] == "Fix bug"
    assert loaded["messages"][0]["content"] == "hi"
    assert store.new_id() == 2

    listing = store.list()
    assert len(listing) == 1
    assert listing[0]["id"] == 1


def test_active_session_marker_roundtrip(tmp_path):
    """The GUI records which session was active per daemon instance so a
    self-update restart can reopen it instead of collapsing to a fresh empty
    conversation."""
    store = SessionStore(tmp_path)
    assert store.active_session("127.0.0.1:8377") is None
    store.set_active("127.0.0.1:8377", 42)
    assert store.active_session("127.0.0.1:8377") == 42
    # instances are isolated: one daemon's marker does not leak into another's
    assert store.active_session("127.0.0.1:8378") is None
    store.set_active("127.0.0.1:8377", 7)
    assert store.active_session("127.0.0.1:8377") == 7


def test_new_session_records_instance(tmp_path):
    """The GUI tags sessions with the daemon address that created them, so
    `silkcode sessions` can tell instances apart when several daemons run on
    one machine (different ports/projects)."""
    session = new_session(1, "Fix bug", "deepseek", "/repo", "ask",
                          instance="127.0.0.1:8377")
    assert session["instance"] == "127.0.0.1:8377"
    # not required: CLI/older sessions simply carry None
    assert new_session(2, "", "deepseek", "/repo", "ask")["instance"] is None


def test_session_ids_unique_across_stores(tmp_path):
    """Two stores (two GUI daemons sharing ~/.silkcode/sessions) must never
    hand out the same id, even when they interleave."""
    store_a = SessionStore(tmp_path)
    store_b = SessionStore(tmp_path)
    ids = [store_a.new_id() for _ in range(5)] + [store_b.new_id() for _ in range(5)]
    assert len(set(ids)) == 10


def test_new_id_continues_past_existing_sessions(tmp_path):
    """A store that predates the counter file keeps the ids already on disk."""
    store = SessionStore(tmp_path)
    store.save(new_session(3, "old", "deepseek", "/repo", "ask"))
    store.save(new_session(7, "older", "deepseek", "/repo", "ask"))
    (tmp_path / ".counter").unlink(missing_ok=True)  # pre-counter layout
    fresh = SessionStore(tmp_path)
    assert fresh.new_id() == 8


def test_session_ids_unique_under_threads(tmp_path):
    """Allocation is serialized in-process (and cross-process via the counter
    file lock), so concurrent callers never collide."""
    store = SessionStore(tmp_path)
    results: list[int] = []
    lock = threading.Lock()

    def alloc():
        n = store.new_id()
        with lock:
            results.append(n)

    threads = [threading.Thread(target=alloc) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 25


def test_checkpoint_revert_restores_and_deletes(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("original")
    created = tmp_path / "created.txt"

    cp = Checkpoints()
    cp.begin()
    cp.snapshot(existing)
    existing.write_text("modified")
    cp.snapshot(created)
    created.write_text("new file")

    restored = cp.revert_last()
    assert existing.read_text() == "original"
    assert not created.exists()
    assert len(restored) == 2
    assert cp.revert_last() == []


def test_checkpoint_generations_are_independent(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("v1")
    cp = Checkpoints()

    cp.begin()
    cp.snapshot(f)
    f.write_text("v2")

    cp.begin()
    cp.snapshot(f)
    f.write_text("v3")

    cp.revert_last()
    assert f.read_text() == "v2"
    cp.revert_last()
    assert f.read_text() == "v1"


# ---- concurrent writers of one session ---------------------------------------

def test_save_uses_a_unique_temp_file_per_writer(tmp_path, monkeypatch):
    """The GUI and the CLI used to write the same <id>.tmp before swapping it
    into place: two saves could mix their bytes in that one file and the
    loser's os.replace installed the mixture as a session."""
    store = SessionStore(tmp_path)
    session = new_session(1, "shared", "m", ".", "edit")
    store.save(session)

    seen = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    store.save(session | {"messages": [{"role": "user", "content": "one"}]})
    store.save(session | {"messages": [{"role": "user", "content": "two"}]})

    assert len(seen) == 2
    assert seen[0] != seen[1], "both writers used the same temp file"
    assert all(name.endswith(".tmp") for name in seen)
    assert "1.tmp" not in seen  # the old shared name is gone
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert store.load(1)["messages"][0]["content"] == "two"


def test_concurrent_saves_of_one_session_never_corrupt_the_json(tmp_path):
    """Hammer one session from two stores (the GUI and the CLI shape) while a
    reader reloads it: every version it sees must be complete, parseable JSON.
    """
    store_a = SessionStore(tmp_path)
    store_b = SessionStore(tmp_path)
    base = new_session(1, "shared", "m", ".", "edit")
    base["messages"] = [{"role": "user", "content": "start"}]
    store_a.save(base)

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(store, tag):
        try:
            for i in range(25):
                store.save(base | {"messages": [
                    {"role": "user", "content": f"{tag}-{i}-" + "x" * 4096}
                ]})
        except BaseException as exc:  # pragma: no cover - the test reports it
            errors.append(exc)

    def reader():
        while not stop.is_set():
            try:
                loaded = store_a.load(1)
                assert loaded["messages"][0]["role"] == "user"
            except BaseException as exc:  # pragma: no cover - the test reports it
                errors.append(exc)
                return

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writers = [threading.Thread(target=writer, args=(store_a, "a")),
               threading.Thread(target=writer, args=(store_b, "b"))]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    reader_thread.join()

    assert errors == []
    final = store_a.load(1)
    assert final["id"] == 1
    # a whole payload from one writer, never a mixture of both
    content = final["messages"][0]["content"]
    assert content.startswith(("a-", "b-"))
    assert content.count("x") == 4096
    assert json.loads(store_a._path(1).read_text())["id"] == 1


def test_saves_of_one_session_wait_for_its_lock(tmp_path):
    """A per-session lock file serializes writers across processes, so two
    replaces can never interleave even when both save at once."""
    fcntl = pytest.importorskip("fcntl")
    store = SessionStore(tmp_path)
    store.save(new_session(1, "locked", "m", ".", "edit"))

    held = os.open(tmp_path / "1.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX)
    done = threading.Event()
    thread = threading.Thread(target=lambda: (store.save(
        new_session(1, "locked", "m", ".", "edit")), done.set()))
    thread.daemon = True
    thread.start()
    try:
        assert not done.wait(0.3), "save did not wait for the per-session lock"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert done.wait(5)
    thread.join(5)
