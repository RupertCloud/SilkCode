import threading

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
