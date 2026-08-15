"""Advisory per-workspace lock (one writer at a time).

A GUI session acquires a lock on its project when it opens, so a second
session on the same project gets a clear "already in use" instead of silently
overlapping. The lock is advisory: writes through the file tools are refused
while another owner holds a fresh lock; reads and everything else proceed.

Locks go stale after LOCK_STALE_SECONDS without activity and are taken over
automatically (daemon restart, crashed session, or an owner that stopped
writing). An owner's own writes refresh the timestamp, so an actively working
session keeps its lock. A lock whose owner process is dead is stale
immediately — a dead process can never write again, so there is no reason to
wait out the timestamp window.

The lock file lives under the workspace's .silkcode/ dir (git-ignored), so it
never pollutes the project. Races across processes are still caught by git at
merge time; this makes same-process/same-machine overlap explicit instead.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LOCK_RELPATH = ".silkcode/workspace.lock"
LOCK_STALE_SECONDS = 30 * 60  # 30 minutes without activity

_lock = threading.Lock()


def _pid_alive(pid: int | None) -> bool:
    """Best-effort liveness probe for a lock owner's process.

    A dead pid means the owner can never write again, so its lock is stale
    right away instead of blocking everyone for LOCK_STALE_SECONDS. On
    platforms where we cannot probe safely (Windows: os.kill(pid, 0) is not a
    harmless existence check there) we fall back to the timestamp rule.
    """
    if pid is None:
        return True  # unknown owner; fall back to the timestamp rule
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but owned by someone else, or probe failed
    return True


class LockError(RuntimeError):
    """The workspace is locked by another owner."""


def _lock_path(root: Path) -> Path:
    return root / LOCK_RELPATH


def read_lock(root: Path) -> dict | None:
    try:
        return json.loads(_lock_path(root).read_text())
    except (OSError, ValueError):
        return None


def is_stale(lock: dict | None) -> bool:
    if not lock:
        return True
    try:
        if not _pid_alive(lock.get("pid")):
            return True  # the owner process is gone; it can never write again
        return time.time() - float(lock.get("acquired_at", 0)) > LOCK_STALE_SECONDS
    except (TypeError, ValueError):
        return True


def lock_state(root: Path) -> dict:
    """Describe the current lock for the GUI: who holds it, how old it is, and
    whether it is stale (owner process dead, or the timestamp ran out)."""
    lock = read_lock(root)
    if not lock:
        return {"held": False}
    try:
        age = time.time() - float(lock.get("acquired_at", 0))
    except (TypeError, ValueError):
        age = None
    return {
        "held": True,
        "owner": lock.get("owner"),
        "pid": lock.get("pid"),
        "age": age,
        "alive": _pid_alive(lock.get("pid")),
        "stale": is_stale(lock),
    }


def acquire(root: Path, owner: str) -> dict:
    """Acquire the workspace lock for `owner`.

    Refreshes the timestamp when the caller already owns it, and takes over a
    stale lock. Raises LockError when another owner holds a fresh lock.
    """
    with _lock:
        existing = read_lock(root)
        if existing and existing.get("owner") != owner and not is_stale(existing):
            raise LockError(
                f"workspace is locked by {existing['owner']} "
                f"(pid {existing.get('pid')})"
            )
        lock = {"owner": owner, "pid": os.getpid(), "acquired_at": time.time()}
        p = _lock_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(lock))
        return lock


def release(root: Path, owner: str) -> bool:
    """Release the lock if `owner` holds it. Returns True when released."""
    with _lock:
        existing = read_lock(root)
        if existing and existing.get("owner") == owner:
            _lock_path(root).unlink(missing_ok=True)
            return True
        return False


def owner_of(root: Path) -> str | None:
    """The current non-stale lock owner, or None when the workspace is free."""
    lock = read_lock(root)
    if lock and not is_stale(lock):
        return lock.get("owner")
    return None
