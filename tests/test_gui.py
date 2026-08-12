"""GUI daemon API tests: real HTTP server, real agent, scripted model."""

import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import sse_response

from silkcode.gui.server import GuiHandler, GuiState


@pytest.fixture
def gui(tmp_path, stub_server, monkeypatch):
    """A running GUI server whose model is a scripted stub and whose config
    and sessions live under a temp SILKCODE_HOME."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")
    monkeypatch.setenv("SILKCODE_HOME", str(home))

    scripted = [
        sse_response(content="Sure - creating the file.",
                     tool_calls=[("write_file", json.dumps({"path": "hello.txt", "content": "hi"}))],
                     usage={"prompt_tokens": 10, "completion_tokens": 5}),
        sse_response(content="Done.", usage={"prompt_tokens": 20, "completion_tokens": 5}),
    ]
    server = stub_server(scripted)
    server.thread.start()

    config_path = home / "config.json"
    config_path.write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url, "default_model": "stub-model"}},
    }))

    state = GuiState(str(workspace), None, "edit")

    class Handler(GuiHandler):
        pass

    Handler.state = state
    Handler.html = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_bytes()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, state, workspace
    httpd.shutdown()
    httpd.server_close()
    server.httpd.shutdown()
    server.httpd.server_close()


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_gui_serves_page_and_state(gui):
    base, state, _ws = gui
    page = httpx.get(f"{base}/")
    assert page.status_code == 200
    assert "Silk Code" in page.text

    resp = httpx.get(f"{base}/api/state").json()
    assert resp["model"] == "stub/stub-model"
    assert resp["mode"] == "edit"
    assert resp["running"] is False


def test_gui_tree_file_and_diff(gui):
    base, _state, _ws = gui
    tree = httpx.get(f"{base}/api/tree").json()
    assert any(e["path"] == "README.md" for e in tree)

    content = httpx.get(f"{base}/api/file", params={"path": "README.md"}).json()
    assert content["content"] == "# demo\n"

    diff = httpx.get(f"{base}/api/diff").json()
    assert "diff" in diff

    escape = httpx.get(f"{base}/api/file", params={"path": "../secret.txt"})
    assert escape.status_code == 400


def test_gui_runs_a_turn_and_persists_session(gui):
    base, state, ws = gui
    resp = httpx.post(f"{base}/api/message", json={"text": "create hello.txt"})
    assert resp.status_code == 200

    assert wait_until(lambda: not state.running and (ws / "hello.txt").exists())
    assert (ws / "hello.txt").read_text() == "hi"

    transcript = httpx.get(f"{base}/api/transcript").json()
    kinds = [t["kind"] for t in transcript]
    assert kinds[0] == "user"
    assert "tool" in kinds
    assert any(t["kind"] == "assistant" and "Done." in t["text"] for t in transcript)

    # session was saved and is resumable from the CLI store (SRS section 47)
    from silkcode.sessions import SessionStore
    store = SessionStore()
    saved = store.load(state.session["id"])
    assert saved["title"] == "create hello.txt"
    assert any(m["role"] == "tool" for m in saved["messages"])

    usage = httpx.get(f"{base}/api/state").json()["usage"]
    assert usage["total_tokens"] == 40


def test_gui_provider_onboarding_and_model_switch(gui):
    base, _state, _ws = gui
    resp = httpx.post(f"{base}/api/providers", json={
        "name": "myserver", "type": "openai_compat",
        "base_url": "https://ai.example.com/v1", "default_model": "m9",
    })
    assert resp.status_code == 200
    assert any(p["name"] == "myserver" for p in resp.json())

    switched = httpx.post(f"{base}/api/model", json={"spec": "myserver"})
    assert switched.status_code == 200
    assert switched.json()["model"] == "myserver/m9"

    bad = httpx.post(f"{base}/api/model", json={"spec": "nope"})
    assert bad.status_code == 400


def test_gui_permission_flow(gui):
    base, state, _ws = gui
    decisions = {}

    def fake_broadcast(event):
        if event.get("type") == "permission_request":
            decisions["id"] = event["id"]

    state.subscribers.clear()
    original = state.broadcast
    state.broadcast = lambda ev: (fake_broadcast(ev), original(ev))

    result = {}

    def ask():
        result["decision"] = state._ask_via_gui("Run command: rm -rf build")

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    assert wait_until(lambda: "id" in decisions)
    resp = httpx.post(f"{base}/api/permission", json={"id": decisions["id"], "decision": "yes"})
    assert resp.status_code == 200
    t.join(timeout=5)
    assert result["decision"] == "yes"
