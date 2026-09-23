"""The loopback git proxy that holds the sandbox's credential.

These run against a real git HTTP server (git-http-backend) that *requires*
an Authorization header, so the proxy is genuinely doing the authenticating -
a stub that accepted anything would prove nothing.
"""

from __future__ import annotations

import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from silkcode.gitproxy import GitAuthProxy, ProxyTarget

TOKEN = "ghp_supersecrettokenvalue"
EXPECTED = "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def _git(*args, **kwargs):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    env.update(kwargs.pop("env", {}))
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          timeout=120, env=env, **kwargs)


class GitHttpServer:
    """Serves one bare repo over smart HTTP, demanding an Authorization header."""

    def __init__(self, repo_root: Path, expected_auth: str):
        self.repo_root = repo_root
        self.expected_auth = expected_auth
        self.seen_auth: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                auth = self.headers.get("Authorization")
                outer.seen_auth.append(auth)
                if auth != outer.expected_auth:
                    body = b"unauthorized"
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                length = int(self.headers.get("Content-Length") or 0)
                if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                    payload = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0], 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        payload += self.rfile.read(size)
                        self.rfile.readline()
                else:
                    payload = self.rfile.read(length) if length else b""

                env = dict(os.environ)
                env.update({
                    "GIT_PROJECT_ROOT": str(outer.repo_root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": self.path.split("?")[0],
                    "QUERY_STRING": self.path.split("?")[1] if "?" in self.path else "",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(payload)),
                    "REMOTE_USER": "tester",
                    "REMOTE_ADDR": "127.0.0.1",
                })
                proc = subprocess.run(
                    [str(Path(subprocess.run(["git", "--exec-path"], capture_output=True,
                                             text=True).stdout.strip()) / "git-http-backend")],
                    input=payload, capture_output=True, env=env, timeout=120)
                raw = proc.stdout
                head, _, body = raw.partition(b"\r\n\r\n")
                status = 200
                headers = []
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"status:"):
                        status = int(line.split(b":", 1)[1].strip().split()[0])
                    elif b":" in line:
                        name, _, value = line.partition(b":")
                        headers.append((name.decode(), value.strip().decode()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def upstream(tmp_path):
    """A bare repo with one commit, served over authenticated HTTP as
    acme/widget.git."""
    root = tmp_path / "served"
    bare = root / "acme" / "widget.git"
    bare.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    # allow pushes into the checked-out branch of a bare repo
    _git("-C", str(bare), "config", "http.receivepack", "true")

    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("hello\n")
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), "commit", "-qm", "seed")
    _git("-C", str(seed), "push", "-q", "origin", "HEAD:main")

    server = GitHttpServer(root, EXPECTED)
    yield server, root
    server.close()


@pytest.fixture
def proxy(upstream):
    server, _ = upstream
    p = GitAuthProxy(ProxyTarget(server.origin, "/acme/widget.git", TOKEN))
    yield p, server
    p.close()


# --------------------------------------------------------------------------

def test_the_upstream_really_does_demand_authentication(upstream, tmp_path):
    """Sanity: without the proxy the clone must fail, or the tests below
    would pass for the wrong reason."""
    server, _ = upstream
    result = _git("clone", "-q", f"{server.origin}/acme/widget.git",
                  str(tmp_path / "direct"))
    assert result.returncode != 0
    assert not (tmp_path / "direct" / ".git").is_dir()


def test_cloning_through_the_proxy_succeeds(proxy, tmp_path):
    p, _ = proxy
    dest = tmp_path / "viaproxy"
    result = _git("clone", "-q", p.url, str(dest))
    assert result.returncode == 0, result.stderr
    assert (dest / "README.md").read_text() == "hello\n"


def test_the_clone_carries_no_credential(proxy, tmp_path):
    """The whole point: nothing in the working tree names the token."""
    p, _ = proxy
    dest = tmp_path / "viaproxy"
    _git("clone", "-q", p.url, str(dest))

    leaked = [str(f) for f in dest.rglob("*")
              if f.is_file() and TOKEN in f.read_text(errors="replace")]
    assert not leaked, f"token found in {leaked}"
    assert TOKEN not in _git("-C", str(dest), "remote", "-v").stdout
    assert TOKEN not in (dest / ".git" / "config").read_text()


def test_pushing_through_the_proxy_reaches_the_upstream(proxy, tmp_path, upstream):
    p, _ = proxy
    _, root = upstream
    dest = tmp_path / "viaproxy"
    _git("clone", "-q", p.url, str(dest))
    (dest / "new.txt").write_text("added\n")
    _git("-C", str(dest), "add", "-A")
    _git("-C", str(dest), "commit", "-qm", "add new.txt")
    result = _git("-C", str(dest), "push", "-q", "origin", "HEAD:main")
    assert result.returncode == 0, result.stderr

    log = _git("--git-dir", str(root / "acme" / "widget.git"),
               "log", "--oneline", "main").stdout
    assert "add new.txt" in log


def test_the_proxy_is_what_authenticates(proxy, tmp_path):
    p, server = proxy
    _git("clone", "-q", p.url, str(tmp_path / "viaproxy"))
    assert server.seen_auth, "the upstream saw no requests"
    assert all(a == EXPECTED for a in server.seen_auth), \
        "a request reached the upstream without the injected credential"


def test_the_proxy_serves_only_its_own_repository(proxy):
    """The credential is scoped: it cannot be pointed at another repo."""
    p, _ = proxy
    base = p.url.rsplit("/acme/", 1)[0]
    for path in ("/someone/else.git/info/refs?service=git-upload-pack",
                 "/acme/other.git/info/refs?service=git-upload-pack"):
        resp = httpx.get(f"{base}{path}")
        assert resp.status_code == 403, path
        assert TOKEN not in resp.text


def _raw_request(port: int, target: str) -> str:
    """Send a request line verbatim - an HTTP client would normalize away the
    traversal we are trying to test."""
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     f"Connection: close\r\n\r\n".encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


@pytest.mark.parametrize("target", [
    "/acme/widget.git/info/refs/../../../someone/else.git/info/refs",
    "/acme/widget.git/../../someone/else.git/info/refs",
    "/acme/widget.git/info/refs/..%2f..%2f..%2fsomeone%2felse.git%2finfo%2frefs",
    "/acme/widget.git/info/refs/%2e%2e/%2e%2e/%2e%2e/someone/else.git/info/refs",
])
def test_a_traversal_cannot_point_the_credential_at_another_repo(proxy, target):
    """The prefix and suffix both match here, yet an upstream that resolves
    the path serves a different repository - with our Authorization header
    attached. The path has to be validated after decoding, and forwarded as
    validated."""
    p, server = proxy
    before = len(server.seen_auth)
    response = _raw_request(p.port, target)
    assert " 403 " in response.splitlines()[0], response.splitlines()[0]
    assert TOKEN not in response
    assert len(server.seen_auth) == before, \
        "the request reached the upstream carrying the credential"


def test_the_proxy_refuses_non_transport_paths(proxy):
    """Only the smart-HTTP endpoints; not an arbitrary API call."""
    p, _ = proxy
    resp = httpx.get(f"{p.url}/../../user")
    assert resp.status_code == 403


def test_the_proxy_never_echoes_the_token(proxy):
    p, _ = proxy
    for url in (f"{p.url}/info/refs?service=git-upload-pack",
                f"{p.url}/nope",
                p.url.rsplit("/acme/", 1)[0] + "/other/repo.git/info/refs"):
        resp = httpx.get(url)
        assert TOKEN not in resp.text
        assert TOKEN not in str(resp.headers)


# --------------------------------------------------------------------------
# timeouts
# --------------------------------------------------------------------------

def test_a_slow_upstream_is_reported_as_a_timeout(tmp_path):
    """The upstream is reachable but not answering. That is a distinct
    condition from a transport failure - the remedy is a longer limit, not a
    retry - so it gets its own status and message."""
    import time

    class Slow(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(5)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    proxy = GitAuthProxy(
        ProxyTarget(origin, "/acme/widget.git", TOKEN),
        timeout=httpx.Timeout(connect=5.0, read=0.5, write=5.0, pool=5.0))
    try:
        resp = httpx.get(f"{proxy.url}/info/refs?service=git-upload-pack", timeout=30)
        assert resp.status_code == 504, resp.status_code
        assert "timed out" in resp.text
        assert TOKEN not in resp.text
    finally:
        proxy.close()
        httpd.shutdown()
        httpd.server_close()


def test_an_unreachable_upstream_is_reported_as_a_bad_gateway():
    proxy = GitAuthProxy(ProxyTarget("http://127.0.0.1:1", "/acme/widget.git", TOKEN))
    try:
        resp = httpx.get(f"{proxy.url}/info/refs?service=git-upload-pack", timeout=30)
        assert resp.status_code == 502
        assert TOKEN not in resp.text
    finally:
        proxy.close()


def test_a_large_push_survives_chunked_transfer(proxy, tmp_path, upstream):
    """git switches to chunked transfer once a push outgrows its buffer, and
    the proxy's dechunking is hand-rolled - so this pushes a payload big
    enough to exercise it and checks the objects arrive intact."""
    import os

    p, _ = proxy
    _, root = upstream
    work = tmp_path / "big"
    assert _git("clone", "-q", p.url, str(work)).returncode == 0

    for i in range(6):
        (work / f"blob{i}.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    _git("-C", str(work), "add", "-A")
    _git("-C", str(work), "commit", "-qm", "big")
    _git("-C", str(work), "config", "http.postBuffer", "16384")  # force chunking

    result = _git("-C", str(work), "push", "origin", "HEAD:main")
    assert result.returncode == 0, result.stderr + result.stdout

    bare = root / "acme" / "widget.git"
    assert "big" in _git("--git-dir", str(bare), "log", "--oneline", "main").stdout
    size = sum(f.stat().st_size for f in bare.rglob("*") if f.is_file())
    assert size > 8 * 1024 * 1024, f"objects did not arrive intact ({size} bytes)"
