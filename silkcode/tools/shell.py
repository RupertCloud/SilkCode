"""Terminal execution tool (SRS section 29).

Execution is delegated to the workspace's execution backend: local
subprocesses by default, or a remote sandbox when one is attached
(`ws.exec_backend`, see silkcode.execbackend).
"""

from __future__ import annotations

from ..execbackend import LocalBackend, MAX_OUTPUT_CHARS  # noqa: F401  (re-exported)
from ..workspace import Workspace

_LOCAL = LocalBackend()


def run_command(ws: Workspace, command: str, timeout: int = 120) -> str:
    backend = getattr(ws, "exec_backend", None) or _LOCAL
    return backend.exec(ws, command, timeout=timeout)
