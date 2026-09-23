"""A loopback git proxy that holds the credential the sandbox must not.

The sandbox has to push, so it needs a GitHub token. Anywhere that token is
reachable as a *string*, the agent can read it and carry it into a model's
context: an environment variable is read by `printenv`, a URL by `git remote
-v`, a config value by `cat .git/config`. Handing it to git and hoping the
agent does not look is not a boundary - git itself will run commands of the
repository's choosing while holding it (`-c alias.x='!sh'`, a
`credential.helper`, a `pre-push` hook the agent wrote).

So git is never given the credential at all. The clone's origin points at
this proxy on loopback, the proxy adds the Authorization header on the way
out, and the token lives only in the proxy's own process.

What that does and does not buy:

  * The token cannot be read by anything running in the sandbox. There is
    no string to find.
  * It cannot be used against any other repository: the proxy serves one
    repo path and refuses everything else.
  * It can still be *used* for this repository - the agent can push here.
    That is unavoidable: pushing is the feature. Confidentiality is
    achievable, exclusivity is not.
"""

from __future__ import annotations

import base64
import posixpath
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import httpx

# Requests git makes over smart HTTP. Anything else is not part of the
# transport and has no reason to carry the credential.
ALLOWED_SUFFIXES = ("/info/refs", "/git-upload-pack", "/git-receive-pack",
                    "/HEAD", "/objects/info/alternates",
                    "/objects/info/http-alternates", "/objects/info/packs")

# Headers that describe the hop, not the request; re-created by the client.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "te",
              "proxy-authorization", "trailers", "transfer-encoding", "upgrade",
              "host", "authorization", "content-length"}

MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# Per-operation, deliberately not a total budget. Cloning a large repository
# streams for minutes while every individual read stays fast, so a single
# overall deadline would kill healthy transfers; what actually indicates
# trouble is one read or connect hanging.
DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0)


class ProxyTarget:
    """The single repository this proxy will speak to.

    `repo_path` is the repository's path on the upstream host, with or
    without the .git suffix ("/acme/widget.git"). Keeping it a path rather
    than an owner/repo pair means any git host works - github.com, an
    Enterprise install, a self-hosted server - since the proxy only needs to
    know which path it is allowed to reach.
    """

    def __init__(self, upstream: str, repo_path: str, token: str):
        self.upstream = upstream.rstrip("/")
        self.repo_path = "/" + repo_path.strip("/")
        self.token = token

    @classmethod
    def from_url(cls, url: str, token: str) -> "ProxyTarget":
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"not an http(s) git URL: {url!r}")
        return cls(f"{parts.scheme}://{parts.netloc}", parts.path, token)

    @property
    def path_prefix(self) -> str:
        return self.repo_path if self.repo_path.endswith(".git") else self.repo_path + ".git"

    @property
    def _bare_prefix(self) -> str:
        return self.path_prefix[: -len(".git")]

    def upstream_url(self, path: str) -> str:
        return f"{self.upstream}{path}"

    def normalized(self, path: str) -> str | None:
        """This repository's path for `path`, or None if it is not ours.

        The prefix must be checked on a *resolved* path. A request for
        /owner/repo.git/info/refs/../../../other/repo.git/info/refs starts
        with our prefix and ends with an allowed suffix, yet an upstream that
        resolves it serves a different repository - with our credential
        attached. So: decode first (%2e%2e is the same attack spelled
        differently), refuse any traversal segment outright rather than
        collapsing it, and forward only what we validated.
        """
        route = unquote(urlparse(path).path)
        if not route.startswith("/"):
            return None
        segments = route.split("/")
        if any(seg in ("..", ".") for seg in segments):
            return None
        route = posixpath.normpath(route)

        base = self.path_prefix
        if not (route == base or route.startswith(base + "/")):
            # tolerate a remote written without the .git suffix
            bare = self._bare_prefix
            if not (route == bare or route.startswith(bare + "/")):
                return None
            route = base + route[len(bare):]
        tail = route[len(base):]
        if tail and not any(tail.startswith(s) for s in ALLOWED_SUFFIXES):
            return None
        return route

    def permits(self, path: str) -> bool:
        return self.normalized(path) is not None

    def authorization(self) -> str:
        raw = f"x-access-token:{self.token}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """Body of the incoming request, Content-Length or chunked."""
    if (handler.headers.get("Transfer-Encoding") or "").lower() == "chunked":
        chunks = []
        total = 0
        while True:
            line = handler.rfile.readline(64).strip()
            if not line:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                handler.rfile.readline()  # trailing CRLF
                break
            total += size
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("upload too large")
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_UPLOAD_BYTES:
        raise ValueError("upload too large")
    return handler.rfile.read(length) if length else b""


class _Handler(BaseHTTPRequestHandler):
    target: ProxyTarget = None  # type: ignore[assignment]
    client: httpx.Client = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A002
        pass

    def _refuse(self, status: int, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        # Forward the validated path, never the raw one: anything the check
        # had to reject or rewrite must not reach the upstream.
        path = self.target.normalized(self.path)
        if path is None:
            # The credential is scoped to one repository; refusing here is
            # what stops it being pointed at another.
            return self._refuse(403, "this proxy serves one repository only")
        try:
            body = _read_body(self)
        except ValueError as exc:
            return self._refuse(413, str(exc))

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        headers["Authorization"] = self.target.authorization()
        query = urlparse(self.path).query
        url = self.target.upstream_url(path + (f"?{query}" if query else ""))

        try:
            with self.client.stream(self.command, url, headers=headers,
                                    content=body or None) as upstream:
                self.send_response(upstream.status_code)
                for name, value in upstream.headers.items():
                    if name.lower() in HOP_BY_HOP or name.lower() == "content-length":
                        continue
                    self.send_header(name, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in upstream.iter_bytes():
                    if not piece:
                        continue
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.write(b"0\r\n\r\n")
        except httpx.TimeoutException as exc:
            # Distinct from a transport failure: the upstream is reachable but
            # slow, and the caller's remedy is a longer limit, not a retry.
            self._refuse(504, f"upstream timed out: {exc}")
        except httpx.HTTPError as exc:
            self._refuse(502, f"upstream request failed: {exc}")
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = _forward


class GitAuthProxy:
    """A running proxy. `url` is what the clone's origin should be set to."""

    def __init__(self, target: ProxyTarget, host: str = "127.0.0.1",
                 timeout: httpx.Timeout | None = None):
        self.target = target
        # The one client that keeps following redirects: git hosts genuinely
        # redirect (renamed repositories, trailing slashes), and refusing
        # would surface a bare 301 to the local git client, which cannot act
        # on it - it only knows this proxy's address. The credential is safe
        # to keep here because httpx drops the Authorization header on any
        # cross-origin hop, a behavior test_redirects.py pins.
        self._client = httpx.Client(timeout=timeout or DEFAULT_TIMEOUT,
                                    follow_redirects=True)

        class Handler(_Handler):
            pass

        Handler.target = target
        Handler.client = self._client
        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.target.path_prefix}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._client.close()
