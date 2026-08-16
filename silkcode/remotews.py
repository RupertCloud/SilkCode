"""Remote workspace: a GitHub repo that lives entirely inside a sandbox.

The normal flow mirrors a local workspace up to a sandbox before each
command. In remote-workspace mode the ownership is flipped: the sandbox
clones the repo into its own store and the local machine never holds a
single repo file. `RemoteWorkspace` is a `Workspace` whose local root is an
empty scratch directory; every file operation (list/read/write/grep), git
operation and shell command is proxied to the sandbox over the Silk Sandbox
Protocol v1 (`/clone`, `/files`, `/read`, `/write`, `/grep`, `/exec`).

The existing tools keep working because they are routed through this class:
  * tools/files  -> ws.read_text / ws.write_text
  * tools/git    -> ws.git(...)
  * tools/search -> ws.list_files / backend grep
  * tools/shell  -> ws.exec_backend (a no-sync exec adapter)
  * repomap      -> ws.list_files (no local disk access at all)
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
from pathlib import Path

from .workspace import ToolError, Workspace


def glob_matches(rel: str, pattern: str) -> bool:
    """Glob match with pathlib's `**` semantics (zero or more directories),
    which fnmatch does not provide. Used for the remote workspace where there
    is no local filesystem to run Path.glob over."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i)
            if j == -1:
                parts.append(re.escape(pattern[i]))
                i += 1
            else:
                parts.append(pattern[i : j + 1])
                i = j + 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(parts), rel) is not None


def workspace_id_for_repo(url: str) -> str:
    """Stable sandbox workspace id for a repo URL (same id on every machine,
    so a repo cloned once in the sandbox is reused)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


class RemoteExecBackend:
    """Execution backend for a RemoteWorkspace: runs commands in the sandbox's
    clone of the repo. Unlike the classic RemoteBackend it never syncs a local
    tree up (there is no local tree - the sandbox owns the files)."""

    name = "remote-workspace"

    def __init__(self, backend, workspace_id: str):
        self._backend = backend
        self._workspace_id = workspace_id

    def exec(self, ws: Workspace, command: str, timeout: int = 120) -> str:
        return self._backend.exec_raw(self._workspace_id, command, timeout=timeout)


class RemoteWorkspace(Workspace):
    """A workspace whose files live in the sandbox, keyed by the repo URL."""

    def __init__(self, backend, repo_url: str, token: str | None = None):
        # The local root is a scratch dir that stays empty - proof (and the
        # guarantee) that the repo is never on this machine. It only needs to
        # exist so Path-based code that checks is_dir() keeps working.
        root = Path(tempfile.mkdtemp(prefix="silk-remote-"))
        super().__init__(root)
        self.backend = backend
        self.repo_url = repo_url
        self.token = token
        self.workspace_id = workspace_id_for_repo(repo_url)
        self.exec_backend = RemoteExecBackend(backend, self.workspace_id)
        self._files_cache: set[str] | None = None
        backend.clone(self.workspace_id, repo_url, token)

    # ---- file API (proxied to the sandbox) ---------------------------------

    def list_files(self) -> list[str]:
        if self._files_cache is None:
            self._files_cache = set(self.backend.files(self.workspace_id))
        return sorted(self._files_cache)

    def refresh(self) -> None:
        self._files_cache = None

    def exists(self, rel: str) -> bool:
        return rel in self._files() or self._is_dir(rel)

    def is_file(self, rel: str) -> bool:
        return rel in self._files()

    def _files(self) -> set[str]:
        if self._files_cache is None:
            self._files_cache = set(self.backend.files(self.workspace_id))
        return self._files_cache

    def _is_dir(self, rel: str) -> bool:
        prefix = rel.rstrip("/") + "/"
        return any(f.startswith(prefix) for f in self._files())

    def read_text(self, rel: str) -> str:
        rel = rel.lstrip("/")
        if not self.is_file(rel):
            raise ToolError(f"Not a file: {rel}")
        return self.backend.read(self.workspace_id, rel)

    def write_text(self, rel: str, content: str) -> None:
        rel = rel.lstrip("/")
        self.backend.write(self.workspace_id, rel, content)
        self._files_cache = None  # the listing changed

    def stat_size(self, rel: str) -> int:
        return len(self.read_text(rel).encode("utf-8"))

    # ---- git (runs inside the sandbox's clone) -----------------------------

    def git(self, *args: str, env: dict | None = None, timeout: int = 60) -> str:
        """Run a git command in the sandbox clone. `env` is accepted for
        signature compatibility with the local tools but ignored: credentials
        live in the sandbox's origin URL (set at clone time), not on this
        machine."""
        cmd = "git " + shlex.join(str(a) for a in args)
        data = self.backend.exec_json(self.workspace_id, cmd, timeout=timeout)
        output = str(data.get("output", ""))
        if data.get("exit_code") != 0:
            detail = output.strip().splitlines()
            first = detail[0] if detail else "unknown error"
            return f"git error (exit {data.get('exit_code')}): {first[:300]}"
        return output

    def exec(self, command: str, timeout: int = 120) -> str:
        return self.exec_backend.exec(self, command, timeout=timeout)
