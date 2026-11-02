"""File tools: read, write, edit (SRS section 27).

Optimistic concurrency: read_file records a fingerprint (sha256) of each file
it returns; write_file/edit_file refuse to touch a file whose on-disk content
no longer matches the recorded fingerprint. So when two sessions/agents in the
same process work on the same file, the second one gets a clean
"re-read and retry" error instead of silently clobbering the first one's
changes.

Each caller owns its own fingerprint registry (the agent loop passes the
Agent's registry via `_registry`), so one session's reads and writes never
mask another session's stale base. Callers that don't pass a registry (tests,
direct calls) share a module-level anonymous one. Files that were never read
are written without a check (best-effort); races across processes/machines
are caught by git at merge time.

Writes are also subject to the advisory per-workspace lock (see silkcode/lock.py):
an agent that carries an `_owner` (a GUI or CLI session) refuses to write while
another owner holds a fresh lock, and refreshes the lock on each successful
write.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from ..lock import acquire, is_stale, read_lock
from ..workspace import ToolError, Workspace

MAX_READ_CHARS = 50_000

# Anonymous registry for callers that don't pass their own (tests, direct use).
# Per workspace root -> {absolute path -> sha256 of last-known content}.
_ANON_REGISTRY: dict[str, dict[str, str]] = {}
_fp_lock = threading.Lock()


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _read_text(path: Path, errors: str = "replace") -> str:
    """Read text without universal-newline translation.

    Path.read_text() translates CRLF/CR to LF, so a read-then-write cycle
    silently rewrote a CRLF file as LF. newline="" keeps the file's own line
    endings; the file tools preserve them instead of normalising them.
    """
    with open(path, "r", errors=errors, newline="") as f:
        return f.read()


def _newline_style(text: str) -> str:
    """The file's newline style: CRLF only when every LF is part of a CRLF."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf == text.count("\n") else "\n"


def _write_text(path: Path, text: str) -> None:
    """Write beside the target, then swap it into place.

    A crash, a full disk (ENOSPC) or a killed process must leave the previous
    version of the file intact, never a truncated one. The text goes to a temp
    file in the same directory first, is fsynced, and only then replaces the
    target atomically. newline="" writes the text's own line endings instead
    of translating them to the platform's.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # durable before the swap, not after it
        try:
            mode = path.stat().st_mode & 0o7777
        except OSError:
            mode = None
        if mode is not None:
            try:
                os.chmod(tmp, mode)  # keep an executable script executable
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _record(registry: dict, ws: Workspace, path: Path, content: str) -> None:
    with _fp_lock:
        registry.setdefault(str(ws.root), {})[str(path)] = _digest(content)


def _check_stale(registry: dict, ws: Workspace, path: Path) -> None:
    """Raise ToolError if `path` changed on disk since this owner read it.

    A missing file also counts as changed (it existed when we read it). Files
    with no recorded fingerprint are not checked.
    """
    try:
        current = _digest(_read_text(path))
    except OSError:
        current = None  # file vanished since it was read
    with _fp_lock:
        recorded = registry.get(str(ws.root), {}).get(str(path))
    if recorded is not None and recorded != current:
        raise ToolError(
            f"{ws.relative(path)} changed on disk since it was last read "
            "(another agent or editor modified it). Re-read it and retry."
        )


def _check_locked(ws: Workspace, owner: str | None) -> None:
    """Refuse writes when another owner holds a fresh workspace lock.

    Advisory: no lock file, or a stale one, means the workspace is free.
    The owner's own writes pass (and refresh the lock afterwards).
    """
    lock = read_lock(ws.root)
    if lock is None or is_stale(lock):
        return
    if lock.get("owner") != owner:
        raise ToolError(
            f"workspace locked by {lock['owner']} (pid {lock.get('pid')}) — "
            "another session is editing this project; edits are refused until "
            "it closes or the lock goes stale."
        )


def read_file(ws: Workspace, path: str, offset: int = 1, limit: int = 1000,
              _registry: dict | None = None, _owner: str | None = None) -> str:
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        rel = path.lstrip("/")
        if not ws.is_file(rel):
            raise ToolError(f"File not found: {path}")
        text = ws.read_text(rel)
        lines = text.splitlines()
        if not lines:
            return "(empty file)"
        offset = max(1, int(offset))
        limit = max(1, int(limit))
        chunk = lines[offset - 1 : offset - 1 + limit]
        if not chunk:
            return f"(no lines at offset {offset}; file has {len(lines)} lines)"
        body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start=offset))
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS] + "\n... [truncated]"
        remaining = len(lines) - (offset - 1 + len(chunk))
        if remaining > 0:
            body += f"\n... ({remaining} more lines)"
        return body
    registry = _registry if _registry is not None else _ANON_REGISTRY
    p = ws.resolve(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    text = _read_text(p)
    _record(registry, ws, p, text)  # fingerprint the exact bytes the agent now knows
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not chunk:
        return f"(no lines at offset {offset}; file has {len(lines)} lines)"
    body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start=offset))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n... [truncated]"
    remaining = len(lines) - (offset - 1 + len(chunk))
    if remaining > 0:
        body += f"\n... ({remaining} more lines)"
    return body


def write_file(ws: Workspace, path: str, content: str,
               _registry: dict | None = None, _owner: str | None = None) -> str:
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        rel = path.lstrip("/")
        ws.write_text(rel, content)
        return f"Wrote {len(content)} characters to {rel}"
    registry = _registry if _registry is not None else _ANON_REGISTRY
    p = ws.resolve(path)
    _check_stale(registry, ws, p)
    _check_locked(ws, _owner)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_text(p, content)
    _record(registry, ws, p, content)
    if _owner:
        acquire(ws.root, _owner)  # refresh the lock: this session is active
    return f"Wrote {len(content)} characters to {ws.relative(p)}"


def edit_file(ws: Workspace, path: str, old_string: str, new_string: str,
              replace_all: bool = False, _registry: dict | None = None,
              _owner: str | None = None) -> str:
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        rel = path.lstrip("/")
        if not ws.is_file(rel):
            raise ToolError(f"File not found: {path}")
        text = ws.read_text(rel)
        count = text.count(old_string)
        if count == 0:
            raise ToolError(f"old_string not found in {path}")
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {path}; make it unique or set replace_all=true"
            )
        if replace_all:
            text = text.replace(old_string, new_string)
        else:
            text = text.replace(old_string, new_string, 1)
        ws.write_text(rel, text)
        return f"Replaced {count if replace_all else 1} occurrence(s) in {rel}"
    registry = _registry if _registry is not None else _ANON_REGISTRY
    p = ws.resolve(path)
    _check_stale(registry, ws, p)  # before the existence check: a read-then-deleted file is "changed"
    _check_locked(ws, _owner)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    text = _read_text(p, errors="strict")
    newline = _newline_style(text)
    count = text.count(old_string)
    if count == 0:
        # read_file shows lines without their endings, so callers (and the
        # model) naturally pass LF-separated old/new strings even for a CRLF
        # file. Match, and insert, in the file's own style so it stays CRLF.
        converted = old_string.replace("\r\n", "\n").replace("\n", newline)
        if converted != old_string and text.count(converted):
            old_string = converted
            new_string = new_string.replace("\r\n", "\n").replace("\n", newline)
            count = text.count(old_string)
    if count == 0:
        raise ToolError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times in {path}; make it unique or set replace_all=true"
        )
    if replace_all:
        text = text.replace(old_string, new_string)
    else:
        text = text.replace(old_string, new_string, 1)
    _write_text(p, text)
    _record(registry, ws, p, text)
    if _owner:
        acquire(ws.root, _owner)  # refresh the lock: this session is active
    return f"Replaced {count if replace_all else 1} occurrence(s) in {ws.relative(p)}"
