"""A built-in live preview server (the Python-native answer to tapio/live-server).

Serves the workspace over HTTP and automatically reloads open pages whenever a
file in the workspace changes — exactly the live-reload workflow live-server
provides, with no Node.js dependency (Silk Code is Python-only, see
pyproject.toml).

How it works:
- A static file server serves the workspace directory tree.
- Each HTML response gets a tiny reload client injected just before ``</body>``:
  it keeps a long-poll against ``/__reload__`` and calls ``location.reload()``
  the moment the server reports a change.
- A watcher thread snapshots the workspace's files (mtime+size, skipping
  ignored dirs) every ``interval`` seconds; on any change it bumps the reload
  counter that the long-poll is waiting on.

The agent exposes this via the ``live_server`` tool (start/stop/status), and it
is also registered as a "default": the workspace gains a preview server on
demand so pages load in the browser while the agent edits them.
"""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .repomap import IGNORED_DIRS
from .workspace import ToolError, Workspace

# Client script injected into served HTML: long-polls /__reload__ and reloads
# the page when the counter it sees changes. ``cur`` is bumped server-side on
# any workspace change; the client holds the count it loaded with and refreshes
# when it differs.
_RELOAD_CLIENT = b"""<script>
(async function () {
  let cur = document.querySelector('meta[name="silk-reload"]');
  cur = cur ? parseInt(cur.content, 10) : 0;
  while (true) {
    try {
      const r = await fetch("/__reload__?since=" + cur);
      const d = await r.json();
      if (d.count > cur) { location.reload(); return; }
    } catch (e) { await new Promise(res => setTimeout(res, 1000)); }
  }
})();
</script>
"""


class _Registry:
    """Tracks the one live server per workspace (keyed by absolute root)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_root: dict[str, LiveServer] = {}

    def get(self, ws: Workspace) -> "LiveServer | None":
        with self._lock:
            return self._by_root.get(str(ws.root))

    def set(self, ws: Workspace, server: "LiveServer") -> None:
        with self._lock:
            self._by_root[str(ws.root)] = server

    def drop(self, ws: Workspace) -> None:
        with self._lock:
            self._by_root.pop(str(ws.root), None)


REGISTRY = _Registry()


def _snapshot(root: Path) -> dict:
    """Map of relative path -> (mtime_ns, size) for every non-ignored file."""
    files = {}
    for path in sorted(root.rglob("*")):
        if set(path.parts) & IGNORED_DIRS:
            continue
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        files[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return files


class LiveServer:
    """A running live-preview server for one workspace."""

    def __init__(self, ws: Workspace, host: str = "127.0.0.1", port: int = 0,
                 interval: float = 0.6):
        self.ws = ws
        self.host = host
        self.interval = interval
        self._reload_count = 0
        self._reload_event = threading.Event()
        self._stale = _snapshot(ws.root)

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # keep the console quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (http.server naming)
                path = urlparse(self.path).path
                if path == "/__reload__":
                    return self._reload()
                return self._serve(path)

            def _reload(self):
                since = urlparse(self.path).query
                try:
                    since = int(since.split("=", 1)[1].split("&", 1)[0])
                except (ValueError, IndexError):
                    since = 0
                # long-poll: block until the count moves past ``since`` or a cap
                outer._reload_event.wait(timeout=60)
                count = outer._reload_count
                outer._reload_event.clear() if count > since else None
                import json as _json
                self._send(200, _json.dumps({"count": count}).encode(),
                           "application/json")

            def _serve(self, path: str):
                rel = path.lstrip("/") or "index.html"
                target = (outer.ws.root / rel).resolve()
                if not str(target).startswith(str(outer.ws.root)):
                    return self._send(403, b"forbidden", "text/plain; charset=utf-8")
                if target.is_dir():
                    target = target / "index.html"
                if not target.exists() or not target.is_file():
                    return self._send(404, b"not found", "text/plain; charset=utf-8")
                body = target.read_bytes()
                if target.suffix.lower() in (".html", ".htm"):
                    # inject the reload client + a meta tag holding the count the
                    # page loaded with, so it can tell whether anything changed.
                    marker = (
                        f'<meta name="silk-reload" content="{outer._reload_count}">'
                    ).encode()
                    if b"</body>" in body:
                        body = body.replace(b"</body>", marker + _RELOAD_CLIENT + b"</body>")
                    else:
                        body = body + marker + _RELOAD_CLIENT
                    return self._send(200, body, "text/html; charset=utf-8")
                return self._send(200, body, _guess_type(target))

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._watch = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def _watch_loop(self) -> None:
        while True:
            time.sleep(self.interval)
            fresh = _snapshot(self.ws.root)
            if fresh != self._stale:
                self._stale = fresh
                self._reload_count += 1
                self._reload_event.set()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _guess_type(path: Path) -> str:
    return {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(path.suffix.lower(), "application/octet-stream")


# ---- agent-facing operations -------------------------------------------------


def live_server(ws: Workspace, action: str = "start", port: int = 0) -> str:
    """Start/stop/status the workspace's live preview server."""
    action = (action or "start").strip().lower()
    existing = REGISTRY.get(ws)

    if action == "status":
        if existing is None:
            return "no live preview server is running for this workspace"
        return f"live preview server running at {existing.url} (reloaded {existing._reload_count}x)"

    if action == "stop":
        if existing is None:
            return "no live preview server is running for this workspace"
        existing.stop()
        REGISTRY.drop(ws)
        return f"stopped the live preview server (was at {existing.url})"

    if action == "start":
        if existing is not None:
            return f"live preview server already running at {existing.url}"
        server = LiveServer(ws, port=port)
        REGISTRY.set(ws, server)
        return (
            f"live preview server running at {server.url} — open this in a browser; "
            "the page reloads automatically whenever a file in the workspace changes. "
            "Stop it with live_server(action=\"stop\")."
        )

    raise ToolError(f"unknown live_server action '{action}' (use start|stop|status)")
