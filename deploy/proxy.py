#!/usr/bin/env python3
"""Route each browser to its own user's Silk Code container.

Slice 1 of the hosted product (docs/CLOUD.md M2). It proves the shape the real
thing needs: one daemon per user, the daemon's access token injected
server-side so a browser never holds it, and everything else - including the
event stream - passed through untouched.

WHAT THIS IS NOT: /_login takes a user name and believes it. There is no
authentication. Slice 2 replaces that endpoint with GitHub sign-in; until then
this binds to loopback and must not be published. Reach it over an SSH tunnel:

    ssh -L 8080:127.0.0.1:8080 you@instance

Three details are load-bearing, each learned from gui/server.py:

  * The daemon refuses a request whose `Origin` disagrees with the `Host` it
    was reached on (_same_origin). So the browser's original Host header is
    forwarded unchanged rather than replaced with 127.0.0.1:PORT.
  * The token goes in `Authorization: Bearer`, added here. The browser never
    sees it, so there is no token in a URL, a cookie or a bookmark.
  * /api/events is Server-Sent Events. A response without Content-Length is
    streamed with read1() and flushed per chunk; buffering it would hang the
    UI forever.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("SILK_STATE", str(Path.home() / ".silkdeploy")))
USER_COOKIE = "silk_user"

# Headers that describe one hop and must not be relayed to the next.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Silk Code</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font:16px system-ui;margin:0;display:grid;place-items:center;height:100vh;
      background:#f4f6fa;color:#17203a}
 main{max-width:26rem;padding:2rem}
 h1{font:600 1.5rem/1.2 system-ui;margin:0 0 .5rem}
 p{color:#3c4767}
 .warn{background:#f6ebd8;border-left:3px solid #b0731a;padding:.75rem 1rem;
       border-radius:0 6px 6px 0;font-size:.9rem}
 form{display:flex;gap:.5rem;margin:1.25rem 0}
 input{flex:1;padding:.55rem .7rem;border:1px solid #d2d9e8;border-radius:6px;font:inherit}
 button{padding:.55rem 1rem;border:0;border-radius:6px;background:#2f3f7a;color:#fff;
        font:inherit;cursor:pointer}
 ul{padding-left:1.1rem;color:#6b7797;font-size:.9rem}
</style>
<main>
  <h1>Silk Code</h1>
  <p>Pick the user whose workspace to open.</p>
  <form action="/_login" method="get">
    <input name="user" placeholder="user name" autofocus required
           pattern="[a-z0-9][a-z0-9_-]{0,31}">
    <button type="submit">Open</button>
  </form>
  <p class="warn"><strong>No authentication.</strong> This page believes whatever
  name it is given. It is a development placeholder — keep it on loopback.</p>
  <p>Running users:</p>
  <ul>%USERS%</ul>
</main>
"""


def backends() -> dict[str, dict]:
    """Every started user, as silkrun recorded them."""
    found: dict[str, dict] = {}
    if not STATE.is_dir():
        return found
    for path in sorted(STATE.glob("*.json")):
        try:
            info = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        user = info.get("user") or path.stem
        if isinstance(info.get("port"), int) and info.get("token"):
            found[user] = info
    return found


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "silk-proxy"

    def log_message(self, fmt, *args):  # noqa: A002
        # No access log: paths can carry query strings, and a log is one more
        # place for something sensitive to settle.
        pass

    # ---- helpers ------------------------------------------------------------

    def _selected_user(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(USER_COOKIE)
        return morsel.value if morsel else None

    def _html(self, body: str, status: int = 200, headers: list[tuple] | None = None) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _login_page(self) -> None:
        users = backends()
        items = "".join(
            f'<li><a href="/_login?user={u}">{u}</a> — port {i["port"]}</li>'
            for u, i in users.items()
        ) or "<li>none — run <code>silkrun start &lt;user&gt;</code></li>"
        self._html(LOGIN_PAGE.replace("%USERS%", items))

    def _do_login(self) -> None:
        from urllib.parse import parse_qs, urlparse

        user = (parse_qs(urlparse(self.path).query).get("user") or [""])[0].strip()
        if user not in backends():
            self._html(
                f"<p>No running container for <code>{user[:32]}</code>.</p>"
                '<p><a href="/">Back</a></p>', status=404)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        # HttpOnly: this selects a backend, so script has no business reading it.
        self.send_header("Set-Cookie",
                         f"{USER_COOKIE}={user}; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- the proxy itself ---------------------------------------------------

    def _forward(self) -> None:
        user = self._selected_user()
        info = backends().get(user or "")
        if not info:
            if self.path.startswith("/api/"):
                self._html("", status=401)
            else:
                self._login_page()
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {}
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP or name.lower() == "authorization":
                continue
            headers[name] = value
        # The daemon compares Origin against Host; forward the browser's own
        # Host so they still agree. Replacing it here is what breaks the GUI.
        headers["Host"] = self.headers.get("Host", "")
        headers["Authorization"] = f"Bearer {info['token']}"

        try:
            conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=600)
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream = conn.getresponse()
        except OSError as exc:
            self._html(
                f"<h1>502</h1><p>The workspace for <code>{user}</code> is not "
                f"answering ({exc.__class__.__name__}).</p>"
                f"<p>On the host: <code>silkrun list</code>, then "
                f"<code>silkrun start {user}</code>.</p>", status=502)
            return

        try:
            self._relay(upstream)
        finally:
            conn.close()

    def _relay(self, upstream: http.client.HTTPResponse) -> None:
        declared = upstream.getheader("Content-Length")
        self.send_response(upstream.status)
        for name, value in upstream.getheaders():
            if name.lower() in HOP_BY_HOP or name.lower() == "content-length":
                continue
            self.send_header(name, value)

        if declared is not None:
            self.send_header("Content-Length", declared)
            self.end_headers()
            remaining = int(declared)
            while remaining > 0:
                chunk = upstream.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
            return

        # No length: an event stream, or anything else open-ended. Close the
        # connection when it ends so the client knows where the body stopped,
        # and flush every chunk - an SSE frame held in a buffer is a hung UI.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        while True:
            chunk = upstream.read1(65536)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break  # the browser navigated away mid-stream

    def do_GET(self):  # noqa: N802
        if self.path == "/" and not self._selected_user():
            self._login_page()
        elif self.path.startswith("/_login"):
            self._do_login()
        elif self.path.startswith("/_logout"):
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{USER_COOKIE}=; Path=/; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._forward()

    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_HEAD = do_GET


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="proxy.py",
        description="Route browsers to per-user Silk Code containers (no auth yet).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default loopback; there is no "
                             "authentication, so do not change this yet)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("refusing to bind beyond loopback: /_login has no authentication,\n"
              "so anyone reaching this port gets a shell in someone's container.\n"
              "Use an SSH tunnel, or wait for slice 2.", file=sys.stderr)
        return 64

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    users = ", ".join(backends()) or "none started"
    print(f"silk proxy on http://{args.host}:{args.port}  (users: {users})")
    print(f"reading {STATE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
