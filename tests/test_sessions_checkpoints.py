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
