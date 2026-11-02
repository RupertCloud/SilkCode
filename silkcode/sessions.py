"""Session persistence shared by GUI and CLI (SRS sections 44 and 47).

Session ids are allocated atomically across processes (a counter file guarded
by an advisory file lock), so several Silk Code GUI daemons can run on one
machine - on different addresses and in different projects - without ever
handing out the same id or clobbering each other's session files. Each session
also records the instance (host:port of the daemon) that created it, so
`silkcode sessions` can tell sessions from different instances apart.

Several writers (a GUI daemon, the CLI, a swarm child) may save the same
session: each save goes to its own temp file and is serialized by a
per-session advisory lock, so an interleaved save can never corrupt the JSON.
Two writers that each keep a whole conversation in memory can still overwrite
one another's new turns; preventing that needs an open-claim/release protocol
in every writer, which this store deliberately does not impose.
"""

from __future__ import annotations

import json
import os
import tempfile
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


def _fsync_dir(directory: Path) -> None:
    """Best-effort durability for the rename itself (a crash right after
    os.replace must not lose the new session file on power failure). Windows
    cannot open a directory for fsync, so there it is skipped."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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

    def _lock_session(self, session_id: int) -> int | None:
        """Exclusive advisory lock for one session's writers, or None when the
        store cannot create the lock file (read-only directory).

        A GUI daemon and the CLI (or two daemons) can save the same session at
        the same time. The lock serializes their replaces; the unique temp name
        in _write keeps their file contents from ever mixing. On Windows the
        flock calls are no-ops, but each replace is still atomic.
        """
        try:
            fd = os.open(self.dir / f"{session_id}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return None
        try:
            _lock_exclusive(fd)
        except OSError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def _write_atomic(path: Path, data: dict) -> None:
        # Unique temp file per writer: GUI and CLI used to share <id>.tmp, so
        # two saves interleaved their bytes into one file and the loser's
        # os.replace installed the mixture as if it were an intact session.
        body = json.dumps(data, indent=2).encode()
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-",
                                    suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _fsync_dir(path.parent)
        finally:
            # the replace consumed the temp file; unlink only if it did not run
            tmp.unlink(missing_ok=True)

    def save(self, data: dict) -> None:
        """Atomic: write beside, then replace. A crash mid-save must leave the
        previous version of the conversation intact, never a half-written
        JSON file - sessions are the state users would miss most.

        Writers of one session are serialized with a per-session lock file so
        a GUI save and a CLI save cannot interleave. That keeps the file whole
        and complete; it does not decide which writer owns a session open in
        two places at once (a whole-conversation last-writer-wins is still
        possible there, and closing that needs an open-claim protocol in every
        writer, not just in this store)."""
        data["updated"] = time.time()
        path = self._path(data["id"])
        lock_fd = self._lock_session(data["id"])
        try:
            self._write_atomic(path, data)
        finally:
            if lock_fd is not None:
                try:
                    _unlock(lock_fd)
                finally:
                    os.close(lock_fd)

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


def _safe_cut(messages: list[dict]) -> list[dict]:
    """Trim a message prefix back to a coherent conversation.

    A cut can land inside a tool exchange: trailing tool results whose
    assistant request was kept, or an assistant message whose tool calls
    lost their results. Providers reject both, so strip back to the last
    complete turn - first any trailing tool results, then the assistant
    message left waiting for them.
    """
    end = len(messages)
    tools = 0
    while end and messages[end - 1].get("role") == "tool":
        end -= 1
        tools += 1
    last = messages[end - 1] if end else None
    calls = last.get("tool_calls") if last and last.get("role") == "assistant" else None
    if calls and len(calls) == tools:
        return messages[:end + tools]  # a complete exchange ends the cut fine
    if calls:
        return messages[:end - 1]  # requests missing (some of) their results
    return messages[:end]  # stray results with no request above them


def fork_session(parent: dict, new_id: int, at_message: int | None = None,
                 instance: str | None = None) -> dict:
    """A new session continuing `parent`'s conversation - unreal-agent's fork,
    on our store: the child copies the parent's history up to a turn boundary
    and records where it came from, and the two diverge freely from there.

    `at_message` caps how much history is copied (the whole conversation when
    None); the cut is pulled back to a complete turn either way. The copy is
    deep: forking must never leave two live sessions sharing message dicts.
    """
    import copy

    messages = parent.get("messages") or []
    if at_message is not None:
        messages = messages[:max(0, at_message)]
    messages = copy.deepcopy(_safe_cut(list(messages)))
    title = parent.get("title") or f"session #{parent.get('id')}"
    data = new_session(new_id, title=f"⑂ {title}"[:60],
                       model=parent.get("model", ""), cwd=parent.get("cwd", ""),
                       mode=parent.get("mode", ""), instance=instance)
    data["messages"] = messages
    data["forked_from"] = {
        "version": 1,
        "id": parent.get("id"),
        "at_message": len(messages),
    }
    return data
