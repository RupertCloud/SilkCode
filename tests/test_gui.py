"""GUI daemon API tests: real HTTP server, real agent, scripted model."""

import io
import json
import socket
import struct
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from conftest import sse_response

from silkcode.gui.server import GuiHandler, GuiState
from silkcode.lock import owner_of
from silkcode.tools.files import write_file
from silkcode.workspace import ToolError


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


def test_gui_sessions_list_and_resume(gui):
    base, state, ws = gui
    httpx.post(f"{base}/api/message", json={"text": "create hello.txt"})
    assert wait_until(lambda: not state.running and (ws / "hello.txt").exists())

    sessions = httpx.get(f"{base}/api/sessions").json()
    assert any(s["id"] == state.session["id"] for s in sessions)

    resp = httpx.post(f"{base}/api/session", json={"id": state.session["id"]})
    assert resp.status_code == 200
    transcript = httpx.get(f"{base}/api/transcript").json()
    assert transcript[0] == {"kind": "user", "text": "create hello.txt"}
    assert any(t["kind"] == "tool" and t["name"] == "write_file" for t in transcript)

    missing = httpx.post(f"{base}/api/session", json={"id": 99999})
    assert missing.status_code == 404


def test_gui_stop_endpoint(gui):
    base, state, _ws = gui
    resp = httpx.post(f"{base}/api/stop", json={})
    assert resp.status_code == 200
    assert state.agent.stop_requested is True


def test_gui_github_status_and_grants(gui, monkeypatch):
    base, state, _ws = gui
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    status = httpx.get(f"{base}/api/github/status").json()
    assert status["connected"] is False
    assert status["repo"] is None  # workspace has no origin remote
    assert status["grants"] == []

    resp = httpx.post(f"{base}/api/github/grants", json={"grants": ["push", "merge"]})
    assert resp.status_code == 200
    assert sorted(resp.json()["grants"]) == ["merge", "push"]
    assert state.permissions.grants == {"push", "merge"}
    # granted operations no longer prompt
    assert state.permissions.check_command("git push origin main") is True

    bad = httpx.post(f"{base}/api/github/grants", json={"grants": ["push", "rm"]})
    assert bad.status_code == 400


def test_gui_multiple_sessions(gui, stub_server, monkeypatch):
    base, state, ws = gui
    first_id = state.default_session_id

    # create a second session: fresh conversation, listed as open
    resp = httpx.post(f"{base}/api/session/new", json={})
    assert resp.status_code == 200
    second_id = resp.json()["session_id"]
    assert second_id != first_id
    sessions = {s["id"]: s for s in resp.json()["sessions"]}
    assert sessions[first_id]["open"] and sessions[second_id]["open"]

    # each session has its own transcript and agent; the second session on the
    # same project carries only the workspace-lock notice, not a conversation
    second_transcript = httpx.get(f"{base}/api/transcript", params={"session": second_id}).json()
    assert second_transcript and all(e["kind"] == "notice" for e in second_transcript)
    assert state.get_session(first_id).agent is not state.get_session(second_id).agent

    # a turn in session 1 doesn't touch session 2 (stub scripted for one turn)
    resp = httpx.post(f"{base}/api/message", json={"text": "create hello.txt", "session_id": first_id})
    assert resp.status_code == 200
    assert wait_until(lambda: not state.get_session(first_id).running and (ws / "hello.txt").exists())
    assert len(httpx.get(f"{base}/api/transcript", params={"session": first_id}).json()) >= 2
    # session 2's transcript is still only the lock notice - the turn didn't leak
    second_after = httpx.get(f"{base}/api/transcript", params={"session": second_id}).json()
    assert all(e["kind"] == "notice" for e in second_after)

    # per-session model switching
    httpx.post(f"{base}/api/providers", json={"name": "alt", "type": "openai_compat",
                                              "base_url": "https://alt.example/v1", "default_model": "m2"})
    resp = httpx.post(f"{base}/api/model", json={"spec": "alt", "session_id": second_id})
    assert resp.json()["model"] == "alt/m2"
    assert httpx.get(f"{base}/api/state", params={"session": first_id}).json()["model"] == "stub/stub-model"

    # switching back to session 1 restores it as the default
    resp = httpx.post(f"{base}/api/session", json={"id": first_id})
    assert resp.json()["session_id"] == first_id

    # unknown session -> 404
    assert httpx.get(f"{base}/api/state", params={"session": 99999}).status_code == 404


def test_gui_push_and_autopush_endpoints(gui, tmp_path):
    base, state, ws = gui
    import subprocess
    origin = tmp_path / "push-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True)

    resp = httpx.post(f"{base}/api/push", json={})
    assert resp.status_code == 200
    assert resp.json()["result"].startswith("Pushed main")

    assert httpx.get(f"{base}/api/state").json()["auto_push"] is False
    resp = httpx.post(f"{base}/api/autopush", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["auto_push"] is True
    assert "push" in state.permissions.grants


def test_gui_device_flow_signin(gui, monkeypatch):
    base, state, _ws = gui
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    # not configured: endpoint explains, status reports unavailable
    status = httpx.get(f"{base}/api/github/status").json()
    assert status["device_flow_available"] is False
    resp = httpx.post(f"{base}/api/github/device", json={})
    assert resp.status_code == 400
    assert "client id" in resp.json()["error"]

    # configure a client id and mock GitHub's device endpoints
    state.config.data.setdefault("github", {})["client_id"] = "cid"

    import silkcode.github_oauth as gho
    def handler(request):
        if request.url.path == "/login/device/code":
            return httpx.Response(200, json={"device_code": "d1", "user_code": "WXYZ-9876",
                                             "verification_uri": "https://github.com/login/device",
                                             "interval": 1, "expires_in": 900})
        return httpx.Response(200, json={"access_token": "ghu_gui", "refresh_token": "ghr_gui",
                                         "expires_in": 28800})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gho, "_make_client", lambda: httpx.Client(transport=transport))

    events = state.subscribe()
    resp = httpx.post(f"{base}/api/github/device", json={})
    assert resp.status_code == 200
    assert resp.json()["user_code"] == "WXYZ-9876"

    # background poll completes and broadcasts success
    assert wait_until(lambda: state.config.data.get("github", {}).get("token") == "ghu_gui")
    import queue as _queue
    seen = []
    try:
        while True:
            seen.append(events.get(timeout=1))
            if any(e.get("type") == "github_connected" for e in seen):
                break
    except _queue.Empty:
        pass
    assert any(e.get("type") == "github_connected" for e in seen)


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


def test_gui_second_session_same_project_locked(gui):
    """A second session on the same project gets 'already in use': its agent
    cannot write, the first session's can, and a notice lands in its transcript."""
    base, state, ws = gui
    s1 = state.get_session()
    s2 = state.new_session()  # same default workspace
    assert s2.lock_conflict is not None and "session-1" in s2.lock_conflict
    assert owner_of(s1.workspace.root) == s1.lock_owner
    # the notice is visible in the second session's transcript
    assert any(e.get("kind") == "notice" for e in s2.transcript)
    # session 1 (lock owner) writes fine; session 2 is refused
    write_file(s1.workspace, "ok.py", "x = 1\n", _owner=s1.lock_owner)
    assert (ws / "ok.py").read_text() == "x = 1\n"
    with pytest.raises(ToolError, match="workspace locked"):
        write_file(s2.workspace, "nope.py", "x = 1\n", _owner=s2.lock_owner)


def test_gui_permission_yes_to_all(gui):
    """'Yes to all' approves the pending request AND flips the session-wide
    flag, so later writes/commands (including the swarm worker's) never prompt."""
    base, state, _ws = gui
    session = state.get_session()
    decisions = {}

    def fake_broadcast(event):
        if event.get("type") == "permission_request":
            decisions["id"] = event["id"]

    state.subscribers.clear()
    original = state.broadcast
    state.broadcast = lambda ev: (fake_broadcast(ev), original(ev))

    result = {}

    def ask():
        result["decision"] = state._ask_via_gui("Run command: rm -rf build",
                                                session.id, session.permissions)

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    assert wait_until(lambda: "id" in decisions)
    resp = httpx.post(f"{base}/api/permission", json={"id": decisions["id"], "decision": "all"})
    assert resp.status_code == 200
    t.join(timeout=5)
    assert result["decision"] == "yes"  # the pending request itself was approved

    # the flag is set on the session's manager: nothing prompts any more
    assert session.permissions._always_all is True
    assert session.permissions.check_write("anything.py") is True
    assert session.permissions.check_command("git push origin main") is True
    assert session.permissions.check_mcp("server.write") is True


def test_gui_swarm_end_to_end(gui, stub_server, monkeypatch):
    base, state, ws = gui
    # give the workspace a failing test so the swarm has something to fix
    # (the empty root conftest.py puts the project root on sys.path for the
    # plain `pytest` command the swarm runs, as real flat-layout repos do)
    (ws / "conftest.py").write_text("")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_math.py").write_text(
        "def test_add():\n    from mathutil import add\n    assert add(1, 2) == 3\n")

    fix = "def add(a, b):\n    return a + b\n"
    scripted = [
        sse_response(content="The test fails: mathutil.add is missing.",
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content=json.dumps({"critique": "add missing", "suggestions": [
            {"title": "Implement mathutil.add", "detail": "Create mathutil.py"}]}),
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(tool_calls=[("write_file", json.dumps({"path": "mathutil.py", "content": fix}))],
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content="Implemented and verified.",
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        # point the config at the fresh server so the swarm's own Config.load() uses it
        import silkcode.config as cfgmod
        cfg = cfgmod.Config.load()
        cfg.data["providers"]["stub"]["base_url"] = server.base_url
        cfg.save()

        resp = httpx.post(f"{base}/api/swarm/start", json={"target": 10})
        assert resp.status_code == 200
        assert resp.json()["running"] is True
        sid = state.default_session_id

        # a normal turn is refused while the swarm owns the workspace
        assert httpx.post(f"{base}/api/message", json={"text": "hi", "session_id": sid}).status_code == 409
        assert httpx.post(f"{base}/api/swarm/start", json={}).status_code == 400

        assert wait_until(lambda: not state.swarm_status(sid)["running"])
        st = state.swarm_status(sid)
        assert st["result"]["status"] == "done"
        assert st["result"]["final_score"] == 10.0
        assert st["result"]["iterations"] == 2
        assert (ws / "mathutil.py").read_text() == fix
        assert any("score: 2.0" in line or "score: 10.0" in line for line in st["log"])

        # stopping with no swarm running reports ok: false
        stop = httpx.post(f"{base}/api/swarm/stop", json={"session_id": sid}).json()
        assert stop["ok"] is False
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_gui_swarm_stop_requests_stop(gui, stub_server, monkeypatch):
    base, state, ws = gui
    (ws / "conftest.py").write_text("")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_math.py").write_text(
        "def test_add():\n    from mathutil import add\n    assert add(1, 2) == 3\n")

    scripted = [
        sse_response(content="The test fails: mathutil.add is missing.",
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "fix", "detail": "fix it"}]}), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content="Refusing to do anything.",
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content="Tester still failing.", usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content=json.dumps({"critique": "c", "suggestions": [
            {"title": "fix", "detail": "fix it"}]}), usage={"prompt_tokens": 10, "completion_tokens": 3}),
        sse_response(content="Refusing to do anything.",
                     usage={"prompt_tokens": 10, "completion_tokens": 3}),
    ]
    server = stub_server(scripted)
    server.thread.start()
    try:
        import silkcode.config as cfgmod
        cfg = cfgmod.Config.load()
        cfg.data["providers"]["stub"]["base_url"] = server.base_url
        cfg.save()

        resp = httpx.post(f"{base}/api/swarm/start", json={"max_iterations": 5})
        assert resp.status_code == 200
        sid = state.default_session_id
        assert wait_until(lambda: state.swarm_status(sid)["running"])
        # request a stop; the loop should end with status "stopped"
        stop = httpx.post(f"{base}/api/swarm/stop", json={"session_id": sid}).json()
        assert stop["ok"] is True
        assert wait_until(lambda: not state.swarm_status(sid)["running"])
        st = state.swarm_status(sid)
        assert st["result"]["status"] == "stopped"
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_gui_update_endpoint_refuses_non_git(gui, monkeypatch):
    base, state, ws = gui
    # simulate a wheel install (no git metadata): update is refused with 400
    import silkcode.update as upd
    monkeypatch.setattr(upd, "git_repo_root", lambda *a, **k: None)
    resp = httpx.post(f"{base}/api/update", json={})
    assert resp.status_code == 400
    assert "pip install" in resp.json()["error"]


def test_gui_update_refuses_while_swarm_running(gui, monkeypatch):
    base, state, ws = gui
    # a swarm is "running" -> update must refuse without touching the repo
    state.swarms[state.default_session_id] = {"running": True}
    resp = httpx.post(f"{base}/api/update", json={})
    assert resp.status_code == 400
    assert "swarm" in resp.json()["error"]


def test_gui_new_session_pointed_at_local_project(gui):
    base, state, default_ws = gui
    first_id = state.default_session_id
    other = default_ws.parent / "other-repo"
    other.mkdir()
    (other / "OTHER.md").write_text("other project\n")

    # a session created with a project spec points at that workspace
    resp = httpx.post(f"{base}/api/session/new", json={"project": str(other)})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    session = state.get_session(sid)
    assert session.workspace.root == other.resolve()
    assert session.workspace.root != default_ws.resolve()

    # tree/file/state all reflect the session's project
    tree = httpx.get(f"{base}/api/tree", params={"session": sid}).json()
    assert any(e["path"] == "OTHER.md" for e in tree)
    default_tree = httpx.get(f"{base}/api/tree", params={"session": first_id}).json()
    assert "OTHER.md" not in [e["path"] for e in default_tree]

    content = httpx.get(f"{base}/api/file", params={"path": "OTHER.md", "session": sid}).json()
    assert content["content"] == "other project\n"

    st = httpx.get(f"{base}/api/state", params={"session": sid}).json()
    assert st["cwd"].endswith("other-repo")
    assert st["session_id"] == sid

    # a session with no project spec still uses the default workspace
    plain = httpx.post(f"{base}/api/session/new", json={}).json()
    plain_session = state.get_session(plain["session_id"])
    assert plain_session.workspace.root == default_ws.resolve()


def test_gui_two_instances_different_ports_and_projects(tmp_path, monkeypatch):
    """Two daemons on one machine - different addresses (host:port) and
    different projects - share the session store without id collisions or
    clobbering, and each session is tagged with the instance that made it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat",
                               "base_url": "http://stub.invalid/v1",
                               "default_model": "stub-model"}},
    }))

    daemon_a = GuiState(str(repo_a), None, "edit", instance="127.0.0.1:8377")
    daemon_b = GuiState(str(repo_b), None, "edit", instance="127.0.0.1:8378")

    # distinct session ids (no cross-daemon clobbering) and no lock conflict
    # because the projects differ
    assert daemon_a.default_session_id != daemon_b.default_session_id
    assert daemon_a.get_session().lock_conflict is None
    assert daemon_b.get_session().lock_conflict is None

    # each session is tagged with the instance that created it
    assert daemon_a.get_session().data["instance"] == "127.0.0.1:8377"
    assert daemon_b.get_session().data["instance"] == "127.0.0.1:8378"

    # saving both must not clobber: each session file holds its own data
    daemon_a._save_session(daemon_a.get_session())
    daemon_b._save_session(daemon_b.get_session())
    from silkcode.sessions import SessionStore
    store = SessionStore()
    saved_a = store.load(daemon_a.default_session_id)
    saved_b = store.load(daemon_b.default_session_id)
    assert saved_a["cwd"] == str(repo_a.resolve()) and saved_a["instance"] == "127.0.0.1:8377"
    assert saved_b["cwd"] == str(repo_b.resolve()) and saved_b["instance"] == "127.0.0.1:8378"

    # summaries show the other instance's sessions too, tagged with their own
    # address so users can tell them apart
    summary = {s["id"]: s for s in daemon_a.sessions_summary()}
    assert summary[daemon_a.default_session_id]["instance"] == "127.0.0.1:8377"
    assert summary[daemon_b.default_session_id]["instance"] == "127.0.0.1:8378"
    assert summary[daemon_b.default_session_id]["open"] is False


def test_json_swallows_dead_socket(gui):
    """_json() must not let a dead client socket escape as an unhandled error.

    This is the unit-level guarantee behind the GUI fix: a client that
    disconnects mid-request (e.g. a double-fired swarm start where the loser
    aborts the connection) must not crash the request handler thread with a
    socketserver traceback.
    """
    class DeadWfile:
        def write(self, _data):
            raise BrokenPipeError(32, "Broken pipe")

    class DeadHandler:
        def __init__(self):
            self.wfile = DeadWfile()

        def send_response(self, _status):
            pass

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    # must not raise
    GuiHandler._json(DeadHandler(), {"error": "a swarm is already running"}, 400)


def _dead_socket_request(host: str, port: int, mode: str) -> None:
    """Open a raw connection to the GUI server and kill it mid-request.

    mode:
      "before-request"  - RST immediately after connecting
      "mid-body"        - send headers + partial body, then RST
      "after-request"   - send a full request, then FIN without reading
    """
    s = socket.create_connection((host, port))
    if mode == "before-request":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        return
    if mode == "mid-body":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.sendall(b"POST /api/message HTTP/1.1\r\n"
                  b"Host: 127.0.0.1\r\n"
                  b"Content-Type: application/json\r\n"
                  b"Content-Length: 100\r\n\r\n"
                  b'{"text": "')
        s.close()
        return
    # after-request: full POST, then FIN without reading the reply
    body = b'{"text": ""}'  # -> fast 400 "empty message", no side effects
    s.sendall(b"POST /api/message HTTP/1.1\r\n"
              b"Host: 127.0.0.1\r\n"
              b"Content-Type: application/json\r\n"
              b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    s.shutdown(socket.SHUT_WR)
    time.sleep(0.2)
    s.close()


@pytest.mark.parametrize("mode", ["before-request", "mid-body", "after-request"])
def test_gui_survives_dead_client_sockets(gui, mode):
    """Clients that abort mid-request must not produce socketserver tracebacks.

    Regression for the "Exception occurred during processing of request"
    tracebacks seen when a browser double-fires a swarm start and the loser
    disconnects before the server can reply: the error response (or the very
    next read on the dead connection) used to raise BrokenPipeError /
    ConnectionResetError out of the request handler.
    """
    base, _state, _ws = gui
    host = urlparse(base).hostname
    port = urlparse(base).port

    # socketserver prints dead-handler tracebacks to sys.stderr; swap stderr
    # for just this window so unrelated background-thread noise from other
    # tests can't cause a false positive.
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        _dead_socket_request(host, port, mode)
        deadline = time.time() + 1.5
        while time.time() < deadline and not buf.getvalue():
            time.sleep(0.05)  # give the handler thread time to hit the socket
    finally:
        sys.stderr = old

    assert "Exception occurred during processing" not in buf.getvalue()

    # the server thread survived and keeps serving normal requests
    resp = httpx.get(f"{base}/api/state")
    assert resp.status_code == 200


def test_gui_close_session_releases_lock(gui):
    """Closing a session stops it and releases its workspace lock; the daemon
    switches to another session (or a fresh one when it was the last)."""
    base, state, ws = gui
    sid = state.default_session_id
    assert owner_of(ws) == f"session-{sid}"

    # open a second session on the same project so closing the first has a
    # non-default fallback target
    second = state.new_session(project=str(ws))
    assert second.lock_conflict is not None  # first session holds the lock

    resp = httpx.post(f"{base}/api/session/close", json={"session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert sid not in state.sessions
    # the closed session's lock is gone (owner_of is None: the second session
    # is read-only and never acquired it)
    assert owner_of(ws) is None
    # the daemon switched to the remaining session
    assert state.default_session_id == second.id
    assert state.default_session_id in state.sessions


def test_gui_close_session_refuses_running_swarm(gui):
    base, state, _ws = gui
    sid = state.default_session_id
    state.swarms[sid] = {"running": True}
    resp = httpx.post(f"{base}/api/session/close", json={"session_id": sid})
    assert resp.status_code == 400
    assert "swarm" in resp.json()["error"]


def test_gui_takeover_of_dead_owners_lock(gui, tmp_path):
    """When the lock's owner process is dead (liveness check), the GUI lets the
    session take the workspace over with one call instead of waiting for the
    30-minute staleness window."""
    import json as _json
    from silkcode.lock import LOCK_RELPATH, acquire, read_lock

    base, state, _ws = gui
    ws2 = tmp_path / "other-repo"
    ws2.mkdir()
    (ws2 / "README.md").write_text("# other\n")

    acquire(ws2, "session-999")  # a live owner (this test process)
    session = state.new_session(project=str(ws2))
    assert session.lock_conflict is not None  # refused at open time

    # the owner "dies": impossible pid, timestamp kept fresh on purpose
    lock = read_lock(ws2)
    lock["pid"] = 2**31 - 1
    (ws2 / LOCK_RELPATH).write_text(_json.dumps(lock))

    resp = httpx.post(f"{base}/api/lock/takeover", json={"session_id": session.id})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert session.lock_conflict is None
    assert owner_of(ws2) == f"session-{session.id}"
    # the new owner's writes are accepted
    from silkcode.tools.files import write_file
    write_file(session.workspace, "a.py", "x = 1\n", _owner=session.lock_owner)
    assert (ws2 / "a.py").read_text() == "x = 1\n"


def test_gui_takeover_refused_while_live_owner_holds_lock(gui):
    base, state, ws = gui
    sid = state.default_session_id  # this live test process owns the workspace
    session = state.new_session(project=str(ws))  # second session -> conflict
    assert session.lock_conflict is not None
    resp = httpx.post(f"{base}/api/lock/takeover", json={"session_id": session.id})
    assert resp.status_code == 400  # LockError -> 400 error response
    assert "locked by" in resp.json()["error"]
    assert session.lock_conflict is not None


def test_gui_restores_active_session_after_restart(tmp_path, monkeypatch):
    """A self-update restart reopens the session that was active before the
    daemon stopped, instead of collapsing the view to a fresh empty session."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat",
                               "base_url": "http://stub.invalid/v1",
                               "default_model": "stub-model"}},
    }))

    instance = "127.0.0.1:8377"
    daemon = GuiState(str(workspace), None, "edit", instance=instance)
    # the daemon's default session is the active one after startup
    assert daemon.store.active_session(instance) == daemon.default_session_id

    # simulate the user switching to another session, then the daemon dying
    # (self-update re-exec) and coming back with the same instance address
    other = daemon.new_session()
    daemon.load_session(other.id)
    daemon.save_all_sessions()  # the restart path persists open sessions
    assert daemon.store.active_session(instance) == other.id

    restarted = GuiState(str(workspace), None, "edit", instance=instance)
    assert restarted.default_session_id == other.id
    assert other.id in restarted.sessions
