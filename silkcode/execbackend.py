"""Execution backends: where run_command/run_tests actually execute.

LocalBackend runs subprocesses on this machine (the default). RemoteBackend
speaks the Silk Sandbox Protocol v1 over HTTPS to a sandbox — a self-hosted
`silkcode sandbox serve` process or the reference Cloudflare Worker — syncing
the workspace up before each command (SRS V0.3, remote sandboxes).

Silk Sandbox Protocol v1 (all requests carry `Authorization: Bearer <token>`):
    GET  /health                 -> {"ok": true, "version": 1}
    POST /sync/<workspace-id>    body: tar.gz of the workspace -> {"ok": true}
    POST /exec/<workspace-id>    {"command": str, "timeout": int}
                                 -> {"exit_code": int, "output": str}
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile

import httpx

from .repomap import IGNORED_DIRS
from .workspace import ToolError, Workspace

MAX_OUTPUT_CHARS = 20_000
MAX_SYNC_BYTES = 100_000_000  # 100 MB workspace cap for remote sync
SYNC_IGNORED = IGNORED_DIRS - {".git"}  # keep .git so git commands work remotely


class LocalBackend:
    name = "local"

    def exec(self, ws: Workspace, command: str, timeout: int = 120) -> str:
        timeout = min(max(int(timeout), 1), 600)
        try:
            proc = subprocess.run(
                command, shell=True, cwd=ws.root,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout} seconds: {command}"
        out = proc.stdout or ""
        if proc.stderr:
            out = out + ("\n" if out else "") + proc.stderr
        out = out.strip() or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        return f"exit code: {proc.returncode}\n{out}"


def workspace_tar(ws: Workspace) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(ws.root.rglob("*")):
            if set(path.parts) & SYNC_IGNORED:
                continue
            if path.is_file():
                tar.add(path, arcname=ws.relative(path))
    data = buffer.getvalue()
    if len(data) > MAX_SYNC_BYTES:
        raise ToolError(
            f"workspace is too large for remote sync ({len(data)} bytes compressed; "
            f"limit {MAX_SYNC_BYTES})"
        )
    return data


class RemoteBackend:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.name = f"remote({self.url})"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=630.0)
        self._last_manifest: tuple | None = None

    def _workspace_id(self, ws: Workspace) -> str:
        return hashlib.sha256(str(ws.root).encode()).hexdigest()[:16]

    def _manifest(self, ws: Workspace) -> tuple:
        entries = []
        for path in sorted(ws.root.rglob("*")):
            if set(path.parts) & SYNC_IGNORED or not path.is_file():
                continue
            stat = path.stat()
            entries.append((ws.relative(path), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def health(self) -> dict:
        # A health check is the one call whose whole job is to report an
        # unreachable sandbox, so a connection error is an expected answer
        # here, not a crash: wrap it like every other request does.
        try:
            resp = self._client.get(f"{self.url}/health", headers=self._headers)
        except httpx.HTTPError as exc:
            raise ToolError(f"sandbox unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise ToolError(f"sandbox health check failed: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolError(
                f"sandbox at {self.url} did not return JSON; is it a Silk Code sandbox?"
            ) from exc

    # ---- remote-workspace protocol (the repo lives in the sandbox) ---------

    def _ws_request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._client.request(method, f"{self.url}{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"sandbox request failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:200]
            try:
                detail = resp.json().get("error", detail)
            except ValueError:
                pass
            raise ToolError(f"sandbox {method} {path} failed: HTTP {resp.status_code}: {detail}")
        return resp.json()

    def clone(self, workspace_id: str, url: str, token: str | None = None) -> None:
        """Tell the sandbox to clone a repo into its own store (or fetch it
        when already present). The repo never touches this machine."""
        self._ws_request("POST", f"/clone/{workspace_id}",
                         json={"url": url, "token": token})

    def files(self, workspace_id: str) -> list[str]:
        return self._ws_request("GET", f"/files/{workspace_id}").get("files", [])

    def read(self, workspace_id: str, path: str) -> str:
        import urllib.parse
        return self._ws_request(
            "GET", f"/read/{workspace_id}?path={urllib.parse.quote(path)}"
        ).get("content", "")

    def write(self, workspace_id: str, path: str, content: str) -> None:
        self._ws_request("POST", f"/write/{workspace_id}",
                         json={"path": path, "content": content})

    def grep(self, workspace_id: str, pattern: str, path: str = ".",
             glob: str = "**/*") -> list[str]:
        return self._ws_request(
            "POST", f"/grep/{workspace_id}",
            json={"pattern": pattern, "path": path, "glob": glob},
        ).get("matches", [])

    def exec_json(self, workspace_id: str, command: str, timeout: int = 120) -> dict:
        """Run `command` in the sandbox workspace without syncing anything up
        (the sandbox owns the files). Returns the raw {"exit_code", "output"}."""
        return self._ws_request(
            "POST", f"/exec/{workspace_id}",
            json={"command": command, "timeout": min(max(int(timeout), 1), 600)},
        )

    def exec_raw(self, workspace_id: str, command: str, timeout: int = 120) -> str:
        data = self.exec_json(workspace_id, command, timeout=timeout)
        output = str(data.get("output", "")).strip() or "(no output)"
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        return f"[sandbox] exit code: {data.get('exit_code', '?')}\n{output}"

    # ---- classic sync-mode exec (local workspace mirrored up) --------------

    def _sync(self, ws: Workspace) -> None:
        manifest = self._manifest(ws)
        if manifest == self._last_manifest:
            return
        data = workspace_tar(ws)
        resp = self._client.post(
            f"{self.url}/sync/{self._workspace_id(ws)}",
            content=data,
            headers={**self._headers, "Content-Type": "application/gzip"},
        )
        if resp.status_code >= 400:
            raise ToolError(f"sandbox sync failed: HTTP {resp.status_code}: {resp.text[:200]}")
        self._last_manifest = manifest

    def exec(self, ws: Workspace, command: str, timeout: int = 120) -> str:
        timeout = min(max(int(timeout), 1), 600)
        try:
            self._sync(ws)
            resp = self._client.post(
                f"{self.url}/exec/{self._workspace_id(ws)}",
                json={"command": command, "timeout": timeout},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"sandbox request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ToolError(f"sandbox exec failed: HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        output = str(data.get("output", "")).strip() or "(no output)"
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        return f"[sandbox] exit code: {data.get('exit_code', '?')}\n{output}"


def remote_backend_from_config(config_data: dict) -> RemoteBackend | None:
    """Build the configured RemoteBackend, or None when no sandbox is set up."""
    import os
    sandbox = config_data.get("sandbox") or {}
    url = sandbox.get("url")
    if not url:
        return None
    token = sandbox.get("token") or os.environ.get(sandbox.get("token_env", "SILKCODE_SANDBOX_TOKEN"), "")
    if not token:
        raise ToolError(
            "sandbox is configured but no token found: set "
            f"${sandbox.get('token_env', 'SILKCODE_SANDBOX_TOKEN')} or run "
            "'silkcode sandbox connect <url> --token ...'"
        )
    return RemoteBackend(url, token)
