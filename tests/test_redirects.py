"""No client replays a prompt or a credential to a redirect destination.

The provider client carries the API key and the whole conversation in every
POST body; the inference probe carries a Bearer token; the docsearch and
GitHub clients carry keys. A server that answers any of them with a 307 gets
an error surfaced to the user, never a re-sent request - the redirect target
is an address chosen by the responding server, not by anyone this side of
the wire.

The one deliberate exception is the git auth proxy: git hosts genuinely
redirect (renamed repositories), and the local git client only knows the
proxy's address, so the proxy follows - relying on httpx dropping the
Authorization header on any cross-origin hop. That behavior is load-bearing,
so it is pinned here: if an httpx upgrade stops stripping it, this file is
what fails.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import silkcode.docsearch as docsearch
from silkcode.providers.openai_compat import OpenAICompatProvider
from silkcode.workspace import ToolError


@pytest.fixture
def redirect_pair():
    """Server A answers everything with a 307 to server B; B records what
    actually arrives - if anything does."""
    arrived = {"requests": []}

    class Target(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length", 0))
            arrived["requests"].append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": self.rfile.read(length).decode(errors="replace"),
            })
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _handle

        def log_message(self, *args):
            pass

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target_server.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{target_server.server_address[1]}"

    class Redirector(BaseHTTPRequestHandler):
        def _handle(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(307)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _handle

        def log_message(self, *args):
            pass

    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    yield f"http://127.0.0.1:{redirect_server.server_address[1]}", arrived
    redirect_server.shutdown()
    redirect_server.server_close()
    target_server.shutdown()
    target_server.server_close()


def test_the_provider_never_follows_a_redirect(redirect_pair):
    """The POST body is the conversation and the header is the key. Neither
    may be replayed to an address the responding server chose."""
    origin, arrived = redirect_pair
    provider = OpenAICompatProvider("test", base_url=f"{origin}/v1",
                                    api_key="sk-conversation-key", retries=0)
    with pytest.raises(Exception):
        provider.chat("m", [{"role": "user", "content": "private prompt"}])
    assert arrived["requests"] == [], \
        "the conversation was replayed to the redirect target"


def test_the_inference_probe_never_follows_a_redirect(redirect_pair):
    origin, arrived = redirect_pair
    from silkcode.inference import probe
    probe(origin, token="lan-token")
    assert arrived["requests"] == [], \
        "the Bearer token was replayed to the redirect target"


def test_docsearch_never_follows_a_redirect(redirect_pair, tmp_path, monkeypatch):
    origin, arrived = redirect_pair
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    monkeypatch.setenv("TEST_REDIR_KEY", "fc-key")
    (home / "config.json").write_text(json.dumps({"doc_search": {
        "type": "firecrawl", "base_url": origin, "api_key_env": "TEST_REDIR_KEY"}}))
    repo = tmp_path / "repo"
    repo.mkdir()
    from silkcode.workspace import Workspace
    with pytest.raises(ToolError):
        docsearch.search_docs(Workspace(repo), "private query")
    assert arrived["requests"] == []


def test_the_github_client_never_follows_a_redirect(redirect_pair):
    origin, arrived = redirect_pair
    from silkcode.github import GitHubClient
    client = GitHubClient("ghp-token", api_url=origin)
    with pytest.raises(Exception):
        client.whoami()
    assert arrived["requests"] == []


def test_every_outbound_client_pins_its_redirect_choice():
    """Relying on a library default is how a behavior changes in a version
    bump without anyone noticing. Every httpx.Client this codebase builds
    says what it wants."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "silkcode"
    unpinned = []
    for path in root.rglob("*.py"):
        text = path.read_text(errors="replace")
        for match in re.finditer(r"httpx\.Client\(([^)]*)\)", text):
            args = match.group(1)
            if "follow_redirects" not in args and "client" not in args:
                line = text[:match.start()].count("\n") + 1
                unpinned.append(f"{path.relative_to(root)}:{line}")
    # cli/main.py's client only downloads from a URL the user typed, with no
    # credential attached; everything else must choose explicitly.
    unpinned = [u for u in unpinned if not u.startswith("cli/")]
    assert unpinned == [], f"httpx.Client without an explicit redirect choice: {unpinned}"


def test_the_git_proxy_follows_but_the_credential_does_not(redirect_pair):
    """The one deliberate follower. httpx drops Authorization on any
    cross-origin hop; the proxy's safety rests on that, so pin it - an httpx
    upgrade that stops stripping fails here, not in production."""
    import httpx

    origin, arrived = redirect_pair
    client = httpx.Client(follow_redirects=True)
    response = client.get(f"{origin}/info/refs",
                          headers={"Authorization": "Basic Z2l0OnRva2Vu"})
    assert response.status_code == 200
    assert len(arrived["requests"]) == 1, "the redirect was not followed"
    assert arrived["requests"][0]["auth"] is None, \
        "httpx replayed Authorization across origins; gitproxy must stop following"
