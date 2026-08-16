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
import os
import re
import subprocess
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROTOCOL_VERSION = 1
WORKSPACE_ID_PATTERN = re.compile(r"^[a-f0-9]{8,64}$")
MAX_BODY_BYTES = 100_000_000
MAX_FILES_LISTED = 50_000
MAX_GREP_RESULTS = 200
MAX_GREP_FILE_BYTES = 1_000_000
# Directories pruned from /files listings (never the repo's .silkcode, so
# instructions/memory stored there still reach the client).
FILES_IGNORED = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
                 "build", ".pytest_cache", ".mypy_cache", "target", ".next"}


def safe_extract(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in archive: {name}")
            if member.issym() or member.islnk():
                raise ValueError(f"links not allowed in archive: {name}")
        tar.extractall(target)  # members validated above


def _list_files(base: Path) -> list[str]:
    """Relative paths of every file under `base`, pruning build/vendor dirs."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in FILES_IGNORED)
        for name in sorted(filenames):
            files.append(str(Path(dirpath).relative_to(base) / name))
            if len(files) >= MAX_FILES_LISTED:
                return files
    return files


def _resolve_under(base: Path, rel: str) -> Path:
    """Resolve a relative path inside `base`, refusing escapes."""
    rel = rel.replace("\\", "/").lstrip("/")
    p = (base / rel).resolve()
    if p != base.resolve() and base.resolve() not in p.parents:
        raise ValueError(f"path escapes the workspace: {rel}")
    return p


def _askpass_script(directory: Path) -> Path:
    """A credential helper that reads the token from the environment.

    The token must not be embedded in the remote URL: git writes that URL
    verbatim into .git/config, where it survives the clone and is readable by
    anything that can see the workspace - `/read`, `/exec` running `cat
    .git/config` or `git remote -v`, and therefore the agent itself, which
    would carry the user's GitHub token into a third-party model's context.
    Passing it through GIT_ASKPASS keeps it in the process environment only,
    the same way the local client does it (see github.py).
    """
    directory.mkdir(parents=True, exist_ok=True)
    askpass = directory / "git-askpass.sh"
    if not askpass.exists():
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  Username*) echo x-access-token ;;\n"
            "  *) printenv SILKCODE_GIT_TOKEN ;;\n"
            "esac\n"
        )
        askpass.chmod(0o700)
    return askpass


def _git_env(token: str | None, helper_dir: Path) -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        env["GIT_ASKPASS"] = str(_askpass_script(helper_dir))
        env["SILKCODE_GIT_TOKEN"] = token
    return env


def _clone_repo(url: str, dest: Path, token: str | None = None) -> None:
    """Clone `url` into `dest` (or fetch when already cloned). The token is
    supplied through a credential helper, so it never lands in .git/config
    and later pushes still work."""
    dest.mkdir(parents=True, exist_ok=True)
    env = _git_env(token, dest.parent / ".silkcode-git")
    if (dest / ".git").is_dir():
        subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune"],
                       capture_output=True, text=True, timeout=600, env=env)
        return
    proc = subprocess.run(["git", "clone", url, str(dest)],
                          capture_output=True, text=True, timeout=600, env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "unknown git error"
        # never echo the token back to the client
        if token:
            detail = detail.replace(token, "***")
        raise ValueError(f"git clone failed: {detail.splitlines()[0][:300]}")


def _grep(base: Path, regex: "re.Pattern", rel_path: str, glob: str) -> list[str]:
    """Server-side grep over the workspace, mirroring the local tool."""
    base_path = _resolve_under(base, rel_path)
    if base_path.is_file():
        candidates = [base_path]
    else:
        # Path.glob supports ** (unlike fnmatch), same as the local tool
        candidates = sorted(p for p in base_path.glob(glob) if p.is_file())
    results: list[str] = []
    for f in candidates:
        if ".git" in f.parts:
            continue
        try:
            if f.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:1024]:
            continue
        text = raw.decode("utf-8", "replace")
        rel = str(f.relative_to(base))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{rel}:{lineno}:{line.strip()[:200]}")
                if len(results) >= MAX_GREP_RESULTS:
                    results.append("... [more matches truncated]")
                    return results
    return results


class SandboxState:
    def __init__(self, base_dir: Path, token: str):
        self.base_dir = base_dir
        self.token = token
        # Per-workspace git credentials, held in memory only. They used to
        # live in the clone's origin URL, which put them in .git/config for
        # anything reading the workspace to find; keeping them here means a
        # git command can still authenticate without the workspace carrying
        # the secret.
        self.git_tokens: dict[str, str] = {}

    def workspace_dir(self, workspace_id: str) -> Path:
        if not WORKSPACE_ID_PATTERN.match(workspace_id):
            raise ValueError("invalid workspace id")
        # resolve() canonicalizes symlinked prefixes (e.g. /var -> /private/var
        # on macOS) so listing/read/grep all see the same path.
        return (self.base_dir / workspace_id).resolve()


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
        path = urlparse(self.path).path
        if path == "/health":
            return self._json({"ok": True, "version": PROTOCOL_VERSION})
        files_match = re.fullmatch(r"/files/([a-f0-9]+)", path)
        read_match = re.fullmatch(r"/read/([a-f0-9]+)", path)
        try:
            if files_match:
                workdir = self.state.workspace_dir(files_match.group(1))
                if not workdir.is_dir():
                    return self._json({"error": "workspace not found"}, 404)
                return self._json({"files": _list_files(workdir)})
            if read_match:
                workdir = self.state.workspace_dir(read_match.group(1))
                if not workdir.is_dir():
                    return self._json({"error": "workspace not found"}, 404)
                query = parse_qs(urlparse(self.path).query)
                rel = (query.get("path") or [""])[0]
                p = _resolve_under(workdir, rel)
                if not p.is_file():
                    return self._json({"error": f"Not a file: {rel}"}, 404)
                if p.stat().st_size > MAX_GREP_FILE_BYTES:
                    return self._json({"content": "(file too large to display)"})
                return self._json({"content": p.read_text(errors="replace")})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
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
        clone_match = re.fullmatch(r"/clone/([a-f0-9]+)", self.path)
        write_match = re.fullmatch(r"/write/([a-f0-9]+)", self.path)
        grep_match = re.fullmatch(r"/grep/([a-f0-9]+)", self.path)
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
                # A git command gets the workspace's credentials; nothing else
                # does, so an ordinary command's environment stays clean.
                # (A command is only recognized as git by its leading word,
                # so this narrows incidental exposure - a repo scan, a `cat
                # .git/config`, a `git remote -v` - rather than defending
                # against a command deliberately written to read it back.)
                stored = self.state.git_tokens.get(exec_match.group(1))
                run_env = None
                if stored and command.strip().split(" ")[0] == "git":
                    run_env = _git_env(stored, workdir.parent / ".silkcode-git")
                try:
                    proc = subprocess.run(
                        command, shell=True, cwd=workdir,
                        capture_output=True, text=True, timeout=timeout,
                        env=run_env,
                    )
                    output = proc.stdout or ""
                    if proc.stderr:
                        output = output + ("\n" if output else "") + proc.stderr
                    return self._json({"exit_code": proc.returncode, "output": output})
                except subprocess.TimeoutExpired:
                    return self._json({"exit_code": -1,
                                       "output": f"command timed out after {timeout} seconds"})
            if clone_match:
                workdir = self.state.workspace_dir(clone_match.group(1))
                request = json.loads(body or b"{}")
                url = str(request.get("url", ""))
                if not url:
                    return self._json({"error": "empty clone url"}, 400)
                token = request.get("token")
                _clone_repo(url, workdir, token=token)
                if token:
                    # remembered so later pushes authenticate without the
                    # token being written into the workspace
                    self.state.git_tokens[clone_match.group(1)] = str(token)
                return self._json({"ok": True})
            if write_match:
                workdir = self.state.workspace_dir(write_match.group(1))
                if not workdir.is_dir():
                    return self._json({"error": "workspace not found"}, 404)
                request = json.loads(body or b"{}")
                rel = str(request.get("path", ""))
                content = request.get("content")
                if not rel or content is None:
                    return self._json({"error": "path and content are required"}, 400)
                p = _resolve_under(workdir, rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(str(content))
                return self._json({"ok": True})
            if grep_match:
                workdir = self.state.workspace_dir(grep_match.group(1))
                if not workdir.is_dir():
                    return self._json({"error": "workspace not found"}, 404)
                request = json.loads(body or b"{}")
                pattern = str(request.get("pattern", ""))
                if not pattern:
                    return self._json({"error": "empty pattern"}, 400)
                try:
                    regex = re.compile(pattern)
                except re.error as exc:
                    return self._json({"error": f"invalid regex: {exc}"}, 400)
                rel_path = str(request.get("path", "."))
                glob = str(request.get("glob", "**/*"))
                return self._json({"matches": _grep(workdir, regex, rel_path, glob)})
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
