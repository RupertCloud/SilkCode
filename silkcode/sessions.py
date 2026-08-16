"""Session persistence shared by GUI and CLI (SRS sections 44 and 47).

Session ids are allocated atomically across processes (a counter file guarded
by an advisory file lock), so several Silk Code GUI daemons can run on one
machine - on different addresses and in different projects - without ever
handing out the same id or clobbering each other's session files. Each session
also records the instance (host:port of the daemon) that created it, so
`silkcode sessions` can tell sessions from different instances apart.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .config import config_dir

try:
    import fcntl

    def _lock_exclusive(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows has no advisory file locks
    def _lock_exclusive(fd: int) -> None:
        pass

    def _unlock(fd: int) -> None:
        pass


_counter_lock = threading.Lock()  # serialize allocations within this process


class SessionStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or (config_dir() / "sessions")
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: int) -> Path:
        return self.dir / f"{session_id}.json"

    def _existing_ids(self) -> list[int]:
        return [int(p.stem) for p in self.dir.glob("*.json") if p.stem.isdigit()]

    def new_id(self) -> int:
        """Allocate a session id unique across every process sharing this store
        (GUI daemons on different ports/projects, the CLI, the swarm). The id
        comes from a counter file updated under an exclusive file lock, so two
        daemons can never hand out the same id and then save to - and clobber -
        the same session file. A store that predates the counter keeps the ids
        it already has on disk."""
        counter = self.dir / ".counter"
        try:
            fd = os.open(counter, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            # Cannot create the counter file (e.g. read-only store): fall back
            # to scanning the directory; ids may collide across processes, but
            # saving to such a store would fail anyway.
            return max(self._existing_ids(), default=0) + 1
        with _counter_lock:
            try:
                _lock_exclusive(fd)
                try:
                    raw = os.read(fd, 64)
                except OSError:
                    raw = b""
                try:
                    last = int(raw.strip() or 0)
                except ValueError:
                    last = 0
                if last == 0:
                    last = max(self._existing_ids(), default=0)
                session_id = last + 1
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, str(session_id).encode())
                os.truncate(fd, len(str(session_id)))
                return session_id
            finally:
                _unlock(fd)
                os.close(fd)

    def save(self, data: dict) -> None:
        data["updated"] = time.time()
        self._path(data["id"]).write_text(json.dumps(data, indent=2))

    def load(self, session_id: int) -> dict:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"No session #{session_id} in {self.dir}")
        return json.loads(path.read_text())

    def list(self) -> list[dict]:
        sessions = []
        for path in self.dir.glob("*.json"):
            if not path.stem.isdigit():
                continue
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            sessions.append({
                "id": data.get("id"),
                "title": data.get("title", ""),
                "model": data.get("model", ""),
                "cwd": data.get("cwd", ""),
                "instance": data.get("instance"),
                "updated": data.get("updated", 0),
                # token spend, so usage can be summarized without loading
                # every conversation (see silkcode/environment.py)
                "usage": data.get("usage") or {},
            })
        return sorted(sessions, key=lambda s: s["updated"], reverse=True)

    # ---- active-session marker (per daemon instance) ------------------------

    def _active_path(self, instance: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in instance)
        return self.dir / f"active.{safe}.json"

    def active_session(self, instance: str | None) -> int | None:
        """The session id that was active in `instance` (a daemon's host:port)
        before it stopped/restarted, or None. The GUI reopens this session on
        startup so a self-update restart does not collapse the view to a fresh
        empty conversation."""
        if not instance:
            return None
        try:
            return int(json.loads(self._active_path(instance).read_text())["session_id"])
        except (OSError, ValueError, KeyError):
            return None

    def set_active(self, instance: str | None, session_id: int) -> None:
        if not instance:
            return
        try:
            self._active_path(instance).write_text(json.dumps({"session_id": session_id}))
        except OSError:
            pass  # best-effort; a read-only store just skips session restore


def new_session(session_id: int, title: str, model: str, cwd: str, mode: str,
                instance: str | None = None) -> dict:
    return {
        "id": session_id,
        "title": title[:60],
        "model": model,
        "cwd": cwd,
        "mode": mode,
        "instance": instance,
        "messages": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "created": time.time(),
        "updated": time.time(),
    }
