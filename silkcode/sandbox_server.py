"""Reference implementation of the Silk Sandbox Protocol v1.

Run it anywhere you want commands executed — a spare machine, a VM, a
container: `silkcode sandbox serve --token <secret>`. The Cloudflare Worker
in sandbox/cloudflare-worker implements the same protocol on Cloudflare
Sandboxes.

SECURITY: the sandbox executes arbitrary shell commands from authenticated
clients. Run it inside a container/VM you are willing to treat as
disposable, never expose it without a strong token, and put TLS in front of
it for anything beyond localhost.
"""

from __future__ import annotations

import hmac
import io
import json
import re
import subprocess
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL_VERSION = 1
WORKSPACE_ID_PATTERN = re.compile(r"^[a-f0-9]{8,64}$")
MAX_BODY_BYTES = 100_000_000


def safe_extract(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in archive: {name}")
            if member.issym() or member.islnk():
                raise ValueError(f"links not allowed in archive: {name}")
        tar.extractall(target)  # members validated above


class SandboxState:
    def __init__(self, base_dir: Path, token: str):
        self.base_dir = base_dir
        self.token = token

    def workspace_dir(self, workspace_id: str) -> Path:
        if not WORKSPACE_ID_PATTERN.match(workspace_id):
            raise ValueError("invalid workspace id")
        return self.base_dir / workspace_id


class SandboxHandler(BaseHTTPRequestHandler):
    state: SandboxState = None  # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        provided = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        return bool(provided) and hmac.compare_digest(provided, self.state.token)

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if self.path == "/health":
            return self._json({"ok": True, "version": PROTOCOL_VERSION})
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._json({"error": "body too large"}, 413)
        body = self.rfile.read(length)

        sync_match = re.fullmatch(r"/sync/([a-f0-9]+)", self.path)
        exec_match = re.fullmatch(r"/exec/([a-f0-9]+)", self.path)
        try:
            if sync_match:
                target = self.state.workspace_dir(sync_match.group(1))
                if target.exists():
                    import shutil
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                safe_extract(body, target)
                return self._json({"ok": True})
            if exec_match:
                workdir = self.state.workspace_dir(exec_match.group(1))
                if not workdir.is_dir():
                    return self._json({"error": "workspace not synced"}, 409)
                request = json.loads(body or b"{}")
                command = str(request.get("command", ""))
                timeout = min(max(int(request.get("timeout", 120)), 1), 600)
                if not command:
                    return self._json({"error": "empty command"}, 400)
                try:
                    proc = subprocess.run(
                        command, shell=True, cwd=workdir,
                        capture_output=True, text=True, timeout=timeout,
                    )
                    output = proc.stdout or ""
                    if proc.stderr:
                        output = output + ("\n" if output else "") + proc.stderr
                    return self._json({"exit_code": proc.returncode, "output": output})
                except subprocess.TimeoutExpired:
                    return self._json({"exit_code": -1,
                                       "output": f"command timed out after {timeout} seconds"})
            self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # keep the sandbox alive
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def make_server(base_dir: Path, token: str, host: str = "127.0.0.1", port: int = 8390) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("a non-empty token is required")
    base_dir.mkdir(parents=True, exist_ok=True)

    class Handler(SandboxHandler):
        pass

    Handler.state = SandboxState(base_dir, token)
    return ThreadingHTTPServer((host, port), Handler)


def serve(base_dir: Path, token: str, host: str = "127.0.0.1", port: int = 8390) -> int:
    server = make_server(base_dir, token, host, port)
    print(f"Silk sandbox (protocol v{PROTOCOL_VERSION}) on http://{host}:{port}")
    print(f"workspaces under {base_dir}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0
