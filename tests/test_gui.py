"""GUI daemon API tests: real HTTP server, real agent, scripted model."""

import io
import json
import os
import re
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

from silkcode.gui.server import GuiHandler, GuiState, _stamped_app_html
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
    # stamped, exactly as run_gui serves it, so the page's idea of the build
    # matches the one /api/state reports
    Handler.html = _stamped_app_html()
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


def test_project_picker_endpoint_returns_a_list(gui):
    base, _state, _ws = gui
    response = httpx.get(f"{base}/api/projects")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_gui_creates_git_project_and_opens_it(gui, tmp_path):
    base, state, _workspace = gui
    response = httpx.post(f"{base}/api/project/create", json={
        "name": "Fresh Product",
        "parent": str(tmp_path),
        "template": "blank",
        "description": "A new product",
        "start_swarm": False,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    root = tmp_path / "fresh-product"
    assert data["project"] == str(root)
    assert data["git"] in {"initialized", "already a git repository"}
    assert (root / ".git").is_dir()
    assert (root / "README.md").is_file()
    assert data["state"]["cwd"] == str(root)
    assert state.project_root(data["state"]["session_id"]) == str(root)


def test_gui_validates_swarm_objective_before_creating_project(gui, tmp_path):
    base, _state, _workspace = gui
    response = httpx.post(f"{base}/api/project/create", json={
        "name": "Should Not Exist",
        "parent": str(tmp_path),
        "template": "blank",
        "start_swarm": True,
    })
    assert response.status_code == 400
    assert "describe" in response.json()["error"].lower()
    assert not (tmp_path / "should-not-exist").exists()


def test_gui_serves_workspace_images_but_not_other_files(gui):
    base, _state, ws = gui
    payload = b"\x89PNG\r\n\x1a\nimage"
    (ws / "shot.png").write_bytes(payload)
    image = httpx.get(f"{base}/api/image", params={"path": "shot.png"})
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == payload
    assert httpx.get(f"{base}/api/image", params={"path": "README.md"}).status_code == 404
    assert httpx.get(f"{base}/api/image", params={"path": "../shot.png"}).status_code == 400


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


def test_gui_environment_page(gui, monkeypatch):
    base, state, ws = gui
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    env = httpx.get(f"{base}/api/environment").json()
    assert {"credentials", "usage", "live", "config_path", "running_count"} <= set(env)

    # live sessions include the one this daemon opened
    assert len(env["live"]) == 1
    live = env["live"][0]
    assert live["id"] == state.default_session_id
    assert live["running"] is False
    assert live["project"] == str(ws)
    assert env["running_count"] == 0

    # credentials never carry the secret itself
    creds = {c["provider"]: c for c in env["credentials"]}
    assert creds["deepseek"]["set"] is False
    assert creds["stub"]["base_url"].startswith("http://127.0.0.1")

    # a key can be stored and cleared from the page, and comes back masked only
    resp = httpx.post(f"{base}/api/environment/key",
                      json={"provider": "deepseek", "key": "sk-secret-value-8888"})
    assert resp.status_code == 200
    creds = {c["provider"]: c for c in resp.json()["credentials"]}
    assert creds["deepseek"]["set"] is True
    assert creds["deepseek"]["masked"] == "…8888"
    assert "sk-secret-value" not in resp.text

    resp = httpx.post(f"{base}/api/environment/key",
                      json={"provider": "deepseek", "clear": True})
    creds = {c["provider"]: c for c in resp.json()["credentials"]}
    assert creds["deepseek"]["set"] is False

    bad = httpx.post(f"{base}/api/environment/key",
                     json={"provider": "nope", "key": "x"})
    assert bad.status_code == 400


def test_gui_environment_usage_after_a_turn(gui):
    base, state, ws = gui
    httpx.post(f"{base}/api/message", json={"text": "create hello.txt"})
    assert wait_until(lambda: not state.running and (ws / "hello.txt").exists())

    env = httpx.get(f"{base}/api/environment").json()
    assert env["live"][0]["usage"]["total_tokens"] == 40
    # the saved session feeds the aggregate view too
    assert env["usage"]["totals"]["total_tokens"] >= 40
    assert any(m["model"].endswith("stub-model") and m["total_tokens"] >= 40
               for m in env["usage"]["by_model"])




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

    # These daemons are on different projects, so daemon A's switcher shows
    # its own project only - that is the point of the scoping.
    scoped = {s["id"] for s in daemon_a.sessions_summary()}
    assert daemon_a.default_session_id in scoped
    assert daemon_b.default_session_id not in scoped

    # Unscoped, the other instance's sessions are all there, each tagged with
    # its own address so two daemons can be told apart in one list.
    summary = {s["id"]: s for s in daemon_a.sessions_summary(all_projects=True)}
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
        # The real header helper, driven through the stubbed send_header
        # below: only the socket is dead here, so borrowing it keeps this
        # fake honest about what _json actually does.
        _security_headers = GuiHandler._security_headers

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


def test_gui_close_project_releases_all_sessions_and_hides_card(gui, tmp_path):
    base, state, _ws = gui
    current = state.default_session_id
    other = tmp_path / "other-project"
    other.mkdir()
    first = state.new_session(project=str(other))
    second = state.new_session(project=str(other))
    state.load_session(current)

    resp = httpx.post(f"{base}/api/project/close", json={
        "session_id": current, "project": str(other),
    })
    assert resp.status_code == 200
    assert resp.json()["closed"] == 2
    assert first.id not in state.sessions and second.id not in state.sessions
    assert all(p["path"] != str(other) for p in resp.json()["projects"])
    assert other.is_dir(), "closing a project deleted its repository"

    refused = httpx.post(f"{base}/api/project/close", json={
        "session_id": current, "project": str(state.get_session(current).workspace.root),
    })
    assert refused.status_code == 400
    assert "switch" in refused.json()["error"]


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


# --------------------------------------------------------------------------
# guards: what the API refuses
#
# These are the branches that stop a second request mutating a session while
# its agent is mid-turn. A regression here does not raise anything — it
# swaps the model or reverts files underneath a running agent.
# --------------------------------------------------------------------------

def _hold_session_running(state, session_id=None):
    """Mark a session as running without actually starting a turn."""
    session = state.get_session(session_id)
    session.running = True
    return session


def test_a_second_message_is_refused_while_the_agent_runs(gui):
    base, state, _ = gui
    _hold_session_running(state)
    resp = httpx.post(f"{base}/api/message", json={"text": "again"})
    assert resp.status_code == 409
    assert "already running" in resp.text


def test_an_empty_message_is_rejected(gui):
    base, _, _ = gui
    for text in ("", "   "):
        resp = httpx.post(f"{base}/api/message", json={"text": text})
        assert resp.status_code >= 400
        assert "empty message" in resp.text


def test_the_model_cannot_be_switched_mid_turn(gui):
    base, state, _ = gui
    _hold_session_running(state)
    resp = httpx.post(f"{base}/api/model", json={"spec": "stub"})
    assert resp.status_code == 409
    assert "while the agent is running" in resp.text


def test_revert_is_refused_mid_turn(gui):
    """Reverting under a running agent would restore files it is editing."""
    base, state, _ = gui
    _hold_session_running(state)
    resp = httpx.post(f"{base}/api/revert", json={})
    assert resp.status_code == 409


def test_push_is_refused_mid_turn(gui):
    base, state, _ = gui
    _hold_session_running(state)
    resp = httpx.post(f"{base}/api/push", json={})
    assert resp.status_code == 409


def test_an_unknown_mode_is_rejected(gui):
    base, state, _ = gui
    before = state.mode
    resp = httpx.post(f"{base}/api/mode", json={"mode": "yolo"})
    assert resp.status_code >= 400
    assert state.mode == before, "a rejected mode must not be applied"


def test_every_valid_mode_is_accepted(gui):
    base, state, _ = gui
    for mode in ("ask", "edit", "agent"):
        assert httpx.post(f"{base}/api/mode", json={"mode": mode}).status_code == 200
        assert state.mode == mode


def test_answering_an_unknown_permission_request_is_a_404(gui):
    base, _, _ = gui
    resp = httpx.post(f"{base}/api/permission",
                      json={"id": "no-such-request", "decision": "yes"})
    assert resp.status_code == 404


def test_grants_must_be_a_list(gui):
    base, _, _ = gui
    resp = httpx.post(f"{base}/api/github/grants", json={"grants": "push"})
    assert resp.status_code >= 400
    assert "must be a list" in resp.text


def test_a_malformed_body_is_reported_not_a_crash(gui):
    base, _, _ = gui
    resp = httpx.post(f"{base}/api/message", content=b"{not json",
                      headers={"Content-Type": "application/json"})
    assert resp.status_code >= 400
    assert "invalid JSON" in resp.text


def test_unknown_routes_are_404(gui):
    base, _, _ = gui
    assert httpx.get(f"{base}/api/nope").status_code == 404
    assert httpx.post(f"{base}/api/nope", json={}).status_code == 404


def test_loading_a_session_that_does_not_exist_is_a_404(gui):
    base, _, _ = gui
    assert httpx.post(f"{base}/api/session", json={"id": 9999}).status_code == 404


def test_switching_to_an_unknown_model_reports_the_error(gui):
    base, state, _ = gui
    before = state.get_session().agent.model
    resp = httpx.post(f"{base}/api/model", json={"spec": "no-such-provider"})
    assert resp.status_code >= 400
    assert state.get_session().agent.model == before, "a failed switch must not change the model"


# ---- the page knows which daemon handed it over -----------------------------

def test_the_page_and_the_daemon_agree_on_the_build(gui):
    """The GUI shows a "reload me" banner when its own UI_VERSION differs from
    the version /api/state reports. Both were the frozen literal "0.1.0", so
    the banner could never fire — and could never be wrong either. Now that it
    carries a real build id, a disagreement here would put a warning in front
    of every user on every load."""
    base, _state, _ws = gui
    page = httpx.get(f"{base}/").text
    ui = re.search(r'const UI_VERSION = "([^"]*)"', page)
    assert ui, "the page no longer declares UI_VERSION; the staleness check is gone"
    assert ui.group(1) == httpx.get(f"{base}/api/state").json()["version"]


def test_the_served_page_carries_the_build_not_the_source_literal():
    """`silkcode update` swaps the server's code while a browser tab still
    holds the page from the build before it. That is only detectable if the
    served page carries the serving daemon's identity rather than a literal
    checked into the file, so on a checkout the two must differ."""
    from silkcode.version import build_id
    on_disk = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_bytes()
    served = _stamped_app_html()
    stamped = re.search(rb'const UI_VERSION = "([^"]*)"', served).group(1).decode()
    assert stamped == build_id()
    if build_id() != re.search(rb'const UI_VERSION = "([^"]*)"', on_disk).group(1).decode():
        assert served != on_disk, "the build id was never stamped into the page"


def test_stamping_never_costs_us_the_page():
    """If the literal is ever renamed, the GUI must still load. A staleness
    hint is not worth a blank screen."""
    import silkcode.gui.server as server

    original = server.Path
    class _Fake:
        def __init__(self, *_a): pass
        @property
        def parent(self): return self
        def __truediv__(self, _o): return self
        def read_bytes(self): return b"<html>no version literal here</html>"
    try:
        server.Path = _Fake
        assert _stamped_app_html() == b"<html>no version literal here</html>"
    finally:
        server.Path = original


# ---- sessions belong to the project you have open ---------------------------

def _seed(store, project, title):
    from silkcode.sessions import new_session
    data = new_session(store.new_id(), title=title, model="stub/stub-model",
                       cwd=str(project), mode="edit", instance="127.0.0.1:1")
    store.save(data)
    return data["id"]


def test_the_session_list_is_scoped_to_the_open_project(gui, tmp_path):
    """Session files are per machine, not per project, so this listed every
    session the user had ever opened anywhere — work on one repository showing
    up in the switcher of another."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    from silkcode.sessions import SessionStore
    store = SessionStore()
    mine = _seed(store, workspace, "in this project")
    theirs = _seed(store, other, "in another project")

    listed = {s["id"] for s in httpx.get(f"{base}/api/sessions").json()}
    assert mine in listed
    assert theirs not in listed, "another project's session is in the list"


def test_nothing_is_stranded_by_the_scoping(gui, tmp_path):
    """Hiding work would be worse than showing too much of it: ?all=1 returns
    everything, and the state names the projects behind the reveal."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    from silkcode.sessions import SessionStore
    theirs = _seed(SessionStore(), other, "in another project")

    everything = {s["id"] for s in httpx.get(f"{base}/api/sessions?all=1").json()}
    assert theirs in everything

    reported = httpx.get(f"{base}/api/state").json()["other_projects"]
    assert [p["label"] for p in reported] == ["other-project"]
    assert reported[0]["count"] == 1


def test_the_list_follows_the_session_you_are_looking_at(gui, tmp_path):
    """A session can be opened on another project, so the scope cannot be the
    daemon's start-up workspace — it has to be the project of the session in
    front of you."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    from silkcode.sessions import SessionStore
    store = SessionStore()
    mine = _seed(store, workspace, "in this project")
    theirs = _seed(store, other, "in another project")

    switched = httpx.post(f"{base}/api/session", json={"id": theirs})
    assert switched.status_code == 200

    listed = {s["id"] for s in httpx.get(f"{base}/api/sessions?session={theirs}").json()}
    assert theirs in listed
    assert mine not in listed, "the list stayed on the daemon's start-up project"


def test_the_same_project_spelled_differently_is_the_same_project(gui, tmp_path):
    """Sessions record whatever path they were opened with, so a trailing
    slash or an unresolved `..` must not split one project into two."""
    base, state, workspace = gui
    from silkcode.sessions import SessionStore
    odd = _seed(SessionStore(), f"{workspace}{os.sep}", "trailing separator")
    listed = {s["id"] for s in httpx.get(f"{base}/api/sessions").json()}
    assert odd in listed, "a trailing separator hid a session from its own project"


def test_a_session_with_no_recorded_project_is_not_lost(gui):
    """Sessions predating per-project workspaces have no cwd. There is nowhere
    to file them, so they stay visible rather than vanishing from every list."""
    base, state, _ws = gui
    from silkcode.sessions import SessionStore
    store = SessionStore()
    legacy = _seed(store, "", "from before projects")

    listed = {s["id"] for s in httpx.get(f"{base}/api/sessions").json()}
    assert legacy in listed


def test_gui_update_reinstalls_a_pip_install_instead_of_refusing(gui, monkeypatch):
    """A `pip install git+https://...` is not a checkout, but it can still be
    updated from the source pip recorded. The endpoint used to refuse outright
    and recommend a PyPI package that does not exist."""
    base, state, ws = gui
    import silkcode.update as upd
    monkeypatch.setattr(upd, "git_repo_root", lambda *a, **k: None)
    monkeypatch.setattr(upd, "install_origin", lambda: {
        "kind": "vcs", "spec": "git+https://github.com/RupertCloud/SilkCode",
        "url": "https://github.com/RupertCloud/SilkCode", "commit_id": "a" * 40})
    monkeypatch.setattr(upd, "update_pip_install", lambda spec, on_progress=None: {
        "status": "updated", "detail": f"Updated: aaaaaaaaaaaa -> bbbbbbbbbbbb ({spec})"})
    resp = httpx.post(f"{base}/api/update", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "updated"


# ---- switching project ------------------------------------------------------

def test_opening_project_restores_its_conversation_without_moving_current(gui, tmp_path):
    base, state, workspace = gui
    original = state.get_session()
    original.transcript.append({"kind": "user", "text": "stay with alpha"})
    other = tmp_path / "other-project"
    other.mkdir()
    theirs = state.new_session(project=str(other))
    theirs.data["title"] = "beta conversation"
    state.load_session(original.id)

    resp = httpx.post(f"{base}/api/project/open", json={"project": str(other)})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == theirs.id
    assert original.workspace.root == workspace.resolve()
    assert any(t.get("text") == "stay with alpha" for t in original.transcript)

    back = httpx.post(f"{base}/api/project/open", json={"project": str(workspace)})
    assert back.json()["session_id"] == original.id


def test_a_session_can_move_to_another_project_keeping_its_conversation(gui, tmp_path):
    """The REPL has done this since `/project` shipped. The GUI could only ever
    open a *new* session on another project, which is why its picker was titled
    "Open a project for this session" while being wired to the new-session
    button — it described something the app could not do."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "README.md").write_text("# other\n")

    session = state.get_session()
    session.transcript.append({"kind": "user", "text": "remember me"})
    before = len(session.agent.messages)

    resp = httpx.post(f"{base}/api/session/project", json={"project": str(other)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["project"] == str(other.resolve())

    assert session.workspace.root == other.resolve()
    assert session.data["cwd"] == str(other.resolve())
    assert any(t.get("text") == "remember me" for t in session.transcript), \
        "the conversation was lost in the move"
    assert len(session.agent.messages) == before, "history was rebuilt, not kept"
    assert str(other.resolve()) in session.agent.messages[0]["content"], \
        "the system prompt still describes the old project"


def test_moving_project_takes_the_workspace_lock_with_it(gui, tmp_path):
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    session = state.get_session()

    assert owner_of(workspace) == session.lock_owner
    httpx.post(f"{base}/api/session/project", json={"project": str(other)})
    assert owner_of(other) == session.lock_owner, "the new project was not locked"
    assert owner_of(workspace) is None, "the old project is still held"


def test_a_session_mid_turn_does_not_move(gui, tmp_path):
    """Switching the tree under a running turn would leave its file tools and
    its checkpoints pointing at different projects."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    state.get_session().running = True
    try:
        resp = httpx.post(f"{base}/api/session/project", json={"project": str(other)})
        assert resp.status_code == 400
        assert "mid-turn" in resp.text
    finally:
        state.get_session().running = False
    assert state.get_session().workspace.root == workspace.resolve()


def test_moving_to_the_project_already_open_is_a_no_op(gui):
    base, state, workspace = gui
    resp = httpx.post(f"{base}/api/session/project", json={"project": str(workspace)})
    assert resp.status_code == 200
    assert state.get_session().workspace.root == workspace.resolve()


def test_switching_project_rescopes_the_session_list(gui, tmp_path):
    """What #27's scoping was waiting for: move the session, and the switcher
    follows it to the new project."""
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    from silkcode.sessions import SessionStore
    theirs = _seed(SessionStore(), other, "work over there")

    assert theirs not in {s["id"] for s in httpx.get(f"{base}/api/sessions").json()}
    httpx.post(f"{base}/api/session/project", json={"project": str(other)})
    assert theirs in {s["id"] for s in httpx.get(f"{base}/api/sessions").json()}


def test_an_empty_project_is_rejected_rather_than_guessed_at(gui):
    base, _state, _ws = gui
    assert httpx.post(f"{base}/api/session/project", json={}).status_code == 400


# ---- the project you launched on is a project you can go back to ------------

def test_the_daemons_own_project_is_remembered(gui, tmp_path):
    """record_recent_project used to fire only when a project was chosen from
    the picker, so `silkcode gui ~/payments-api` left payments-api as the one
    project never in the list — you had to type its path to get back to where
    you started."""
    base, state, workspace = gui
    from silkcode.project import recent_projects
    assert str(workspace.resolve()) in {r["spec"] for r in recent_projects()}


def test_a_projects_recent_entry_is_named_not_pathed(gui, tmp_path):
    base, state, workspace = gui
    from silkcode.project import recent_projects
    entry = next(r for r in recent_projects() if r["spec"] == str(workspace.resolve()))
    assert entry["label"] == workspace.name


def test_the_switcher_offers_current_open_and_recent_projects(gui, tmp_path):
    base, state, workspace = gui
    other = tmp_path / "other-project"
    other.mkdir()
    httpx.post(f"{base}/api/session/project", json={"project": str(other)})

    projects = httpx.get(f"{base}/api/state").json()["projects"]
    by_label = {p["label"]: p for p in projects}
    assert by_label[other.name]["current"] is True
    assert workspace.name in by_label, "the project we came from vanished from the switcher"
    assert len({p["path"] for p in projects}) == len(projects), "duplicate entries"


# ---- the access token has to survive the daemon restarting itself -----------

def test_a_token_is_reused_across_restarts(tmp_path, monkeypatch):
    """A daemon reachable beyond this machine mints a token and prints it
    inside the URL — then re-execs itself to hot-apply an update. The restart
    argv reproduces the launch configuration except for the token, so every
    self-update minted a new one and answered the tab you already had open
    with "Unauthorized ... open the URL printed when it started". On a phone
    driving a laptop, that URL is on the other device.
    """
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.gui.server import remember_token, remembered_token

    assert remembered_token("0.0.0.0", 8377) is None
    remember_token("0.0.0.0", 8377, "the-token")
    assert remembered_token("0.0.0.0", 8377) == "the-token"


def test_each_address_keeps_its_own_token(tmp_path, monkeypatch):
    """Two daemons on one machine are two daemons; one must not answer to the
    other's URL."""
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.gui.server import remember_token, remembered_token

    remember_token("0.0.0.0", 8377, "first")
    remember_token("0.0.0.0", 8378, "second")
    assert remembered_token("0.0.0.0", 8377) == "first"
    assert remembered_token("0.0.0.0", 8378) == "second"


def test_the_token_is_not_readable_by_anyone_else(tmp_path, monkeypatch):
    """It is a credential for something that runs commands, so it is stored
    the way this installation stores its API keys, not in argv where `ps`
    shows it to every user on the machine."""
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.gui.server import _token_path, remember_token

    remember_token("0.0.0.0", 8377, "the-token")
    path = _token_path("0.0.0.0", 8377)
    assert oct(path.stat().st_mode)[-3:] == "600", oct(path.stat().st_mode)
    assert oct(path.parent.stat().st_mode)[-3:] == "700"


def test_an_unwritable_config_directory_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """Remembering is a convenience. Failing to remember costs a new token,
    which is recoverable; refusing to start is not."""
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.gui.server import remember_token, remembered_token

    def deny(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", deny)
    remember_token("0.0.0.0", 8377, "the-token")   # must not raise
    assert remembered_token("0.0.0.0", 8377) is None


def test_a_corrupt_token_file_is_ignored_rather_than_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path / "home"))
    from silkcode.gui.server import _token_path, remembered_token

    path = _token_path("0.0.0.0", 8377)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n")
    assert remembered_token("0.0.0.0", 8377) is None, \
        "an empty file became an empty token, which would disable the check"
