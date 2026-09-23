"""The network surface of the GUI daemon.

Reachable beyond loopback, this daemon's access token is worth a shell on the
machine: it drives an agent that reads and writes files, runs commands, and
holds API keys. These tests hold the controls that guard it, and the record
that makes their decisions visible.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from silkcode.gui.server import GuiHandler, GuiState, _stamped_app_html

TOKEN = "test-token-abcdefghijklmnop"


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A token-protected daemon, as `--host 0.0.0.0` produces."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat",
                               "base_url": "http://127.0.0.1:1/v1",
                               "default_model": "stub-model"}},
    }))
    state = GuiState(str(workspace), None, "ask")

    class Handler(GuiHandler):
        pass

    Handler.state = state
    Handler.html = _stamped_app_html()
    Handler.token = TOKEN
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()
    Handler.token = None


def get(base, path="/api/state", token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.get(base + path, headers=headers, timeout=10, **kwargs)


# ---- the token gate ---------------------------------------------------------

def test_a_request_without_the_token_is_refused(daemon):
    base, _ = daemon
    assert get(base).status_code == 401


def test_a_request_with_the_wrong_token_is_refused(daemon):
    base, _ = daemon
    assert get(base, token="not-the-token").status_code == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(daemon):
    """Comparison is over the whole value, not a prefix."""
    base, _ = daemon
    assert get(base, token=TOKEN[:-1]).status_code == 401


def test_the_right_token_is_accepted_by_header_cookie_or_query(daemon):
    base, _ = daemon
    assert get(base, token=TOKEN).status_code == 200
    assert httpx.get(f"{base}/api/state?token={TOKEN}", timeout=10).status_code == 200
    assert httpx.get(base + "/api/state", timeout=10,
                     headers={"Cookie": f"silk_token={TOKEN}"}).status_code == 200


def test_another_sites_request_is_refused_even_with_the_token(daemon):
    """A browser attaches Origin to cross-origin requests. Carrying the token
    is not enough if the request was made on someone else's behalf."""
    base, _ = daemon
    resp = get(base, token=TOKEN, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


# ---- headers ----------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/state"])
def test_every_response_carries_the_security_headers(daemon, path):
    base, _ = daemon
    resp = get(base, path, token=TOKEN)
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_a_refusal_also_carries_them(daemon):
    """A 401 body is still a page a browser renders."""
    base, _ = daemon
    resp = get(base)
    assert resp.status_code == 401
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_the_page_url_carries_the_token_so_referrers_are_suppressed(daemon):
    """The page is opened as `/?token=...` and links out to silkcode.web.app.
    Browsers default to stripping the query cross-origin, but the credential
    at stake is worth asserting rather than inheriting."""
    base, _ = daemon
    resp = httpx.get(f"{base}/?token={TOKEN}", timeout=10)
    assert resp.status_code == 200
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_the_session_cookie_is_not_reachable_from_script(daemon):
    base, _ = daemon
    resp = httpx.get(f"{base}/?token={TOKEN}", timeout=10)
    cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


# ---- the record of what happened --------------------------------------------

def test_a_refused_request_is_recorded_with_its_reason(daemon):
    base, state = daemon
    get(base)                                   # no token
    get(base, token="wrong")                    # bad token
    snap = state.connections.snapshot()
    assert snap["denied_total"] == 2
    reasons = {d["reason"] for d in snap["denied_recent"]}
    assert reasons == {"no token presented", "token did not match"}


def test_the_presented_token_is_never_in_the_record(daemon):
    """A wrong token is often a real credential — the right one for another
    daemon, or one with a typo. It must not land in a buffer the GUI renders."""
    base, state = daemon
    get(base, token="sk-a-real-looking-secret")
    blob = json.dumps(state.connections.snapshot())
    assert "sk-a-real-looking-secret" not in blob
    assert TOKEN not in blob


def test_an_allowed_request_is_recorded_too(daemon):
    base, state = daemon
    get(base, token=TOKEN)
    clients = state.connections.snapshot()["clients"]
    assert clients[0]["requests"] >= 1
    assert clients[0]["denied"] == 0
    assert clients[0]["active"] is True


def test_the_connections_endpoint_needs_the_token_like_everything_else(daemon):
    """The monitor reports who is connecting; it must not itself be readable
    by whoever is being refused."""
    base, _ = daemon
    assert get(base, "/api/connections").status_code == 401
    assert get(base, "/api/connections", token=TOKEN).status_code == 200


def test_the_endpoint_reports_what_the_monitor_saw(daemon):
    base, _ = daemon
    get(base)                                   # refused
    body = get(base, "/api/connections", token=TOKEN).json()
    assert body["denied_total"] == 1
    assert body["clients"]
    assert "streams" in body


# ---- pairing on demand ------------------------------------------------------

def test_pairing_details_are_available_without_restarting(daemon, monkeypatch):
    """Startup prints the QR once. A terminal scrolls, a second device turns
    up later, and killing the daemon to see a code again is not an answer.

    Driven with an explicit binding: the test daemon listens on loopback, and
    pairing (correctly) reports nothing to pair with there."""
    _, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["100.101.102.103", "192.168.1.20"])
    body = state.pairing_info(8377, TOKEN, bound_host="0.0.0.0")
    assert body["reachable"] is True
    assert [a["label"] for a in body["addresses"]] == ["Tailscale", "LAN"]
    assert body["addresses"][0]["url"].startswith("http://100.101.102.103:")
    assert TOKEN in body["addresses"][0]["url"]


def test_the_pairing_qr_is_a_matrix_the_page_can_draw(daemon, monkeypatch):
    _, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20"])
    body = state.pairing_info(8377, TOKEN, bound_host="0.0.0.0")
    qr = body["qr"]
    assert qr and len(qr) == len(qr[0]), "not square"
    assert all(isinstance(cell, bool) for row in qr for cell in row)
    # it encodes the address it says it does
    assert body["qr_for"]["address"] == "192.168.1.20"


def test_the_qr_encodes_the_url_that_is_displayed(daemon, monkeypatch):
    """The URL under the code and the code itself have to agree, or a scan
    silently lands somewhere other than what the operator read."""
    from silkcode.qr import encode
    _, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20"])
    body = state.pairing_info(8377, TOKEN, bound_host="0.0.0.0")
    expected = [[bool(v) for v in row] for row in encode(body["qr_for"]["url"])]
    assert body["qr"] == expected


def test_pairing_needs_the_token_like_everything_else(daemon):
    """It hands back a URL containing the credential, so it must sit behind
    the credential. (A caller with the token learns nothing new — which is
    why returning it here is not a leak.)"""
    base, _ = daemon
    assert get(base, "/api/pairing").status_code == 401


def test_a_machine_with_no_address_at_all_says_pairing_is_not_possible(daemon, monkeypatch):
    """Bound everywhere, but the machine is offline: still nothing to pair
    with. Saying so beats rendering an empty box to interpret."""
    _, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses", lambda: [])
    body = state.pairing_info(8377, TOKEN, bound_host="0.0.0.0")
    assert body["reachable"] is False
    assert body["qr"] is None


# ---- pairing must reflect the binding, not the interfaces -------------------

def test_a_loopback_daemon_does_not_advertise_a_lan_address(daemon, monkeypatch):
    """Having a LAN interface is not the same as listening on it. The default
    `silkcode gui` binds 127.0.0.1, and on a machine with a LAN address this
    used to hand out a QR for a URL nothing could connect to — a confident
    wrong answer, which is worse than no answer."""
    base, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20", "100.101.102.103"])
    info = state.pairing_info(8377, "tok", bound_host="127.0.0.1")
    assert info["reachable"] is False
    assert info["loopback_only"] is True
    assert info["addresses"] == []
    assert info["qr"] is None


def test_a_daemon_bound_to_every_interface_does_advertise(daemon, monkeypatch):
    base, state = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20"])
    info = state.pairing_info(8377, "tok", bound_host="0.0.0.0")
    assert info["reachable"] is True
    assert info["loopback_only"] is False
    assert info["addresses"][0]["url"] == "http://192.168.1.20:8377/?token=tok"


def test_the_live_endpoint_uses_the_real_binding(daemon, monkeypatch):
    """The fixture binds 127.0.0.1, so the endpoint must say so even on a
    machine that has other interfaces."""
    base, _ = daemon
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20"])
    body = get(base, "/api/pairing", token=TOKEN).json()
    assert body["loopback_only"] is True
    assert body["reachable"] is False
