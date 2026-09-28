"""Durable records of commands in flight, and honest answers when one dies.

Today, if Silk Code's process dies while a command is running - the laptop
sleeps mid-`pytest`, the daemon is killed during a build - the next session
knows nothing: the run simply never happened, as far as anyone can tell.

unreal-agent treats this as a state machine with named states, and the
taxonomy is worth adopting even without its actor runtime: "the process
never started" and "the process started but no exit status was recorded"
are different answers, and the user deserves whichever one is true, plus
whatever output was captured before the lights went out.

So the local execution backend writes a small record into
`.silkcode/inflight/` before starting a command, points the command's output
at files beside it, and deletes everything on completion. A record that is
still there later - with no live process behind it - is an orphan, and the
next session in that workspace reports it as a notice: the command, when it
started, which of the two fates it met, and the tail of its captured output.
Records are versioned so the format can change without breaking old state.

A live process keeps its record: several sessions can share a workspace, so
"a record exists" only becomes "something died" once the pid is gone.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .statedir import state_dir

RECORD_VERSION = 1
DIRNAME = "inflight"
OUTPUT_TAIL_CHARS = 1500


class Record:
    """One command's durable state: a JSON file plus its output files."""

    def __init__(self, path: Path):
        self.path = path
        self.out_path = path.with_suffix(".out")
        self.err_path = path.with_suffix(".err")

    def _write(self, data: dict) -> None:
        # atomic: an interrupted write must never leave a half-record that a
        # later session would misread as a real orphan
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)


def _inflight_dir(root: Path, create: bool = False) -> Path:
    if create:
        return state_dir(root) / DIRNAME
    return Path(root) / ".silkcode" / DIRNAME


def begin(root: Path, command: str) -> Record | None:
    """Record that `command` is about to start. Never raises: on a read-only
    filesystem commands still run, just without crash accounting."""
    try:
        directory = _inflight_dir(Path(root), create=True)
        directory.mkdir(parents=True, exist_ok=True)
        record = Record(directory / f"{uuid.uuid4().hex[:12]}.json")
        record._write({
            "version": RECORD_VERSION,
            "command": str(command),
            "started_at": time.time(),
        })
        return record
    except OSError:
        return None


def started(record: Record | None, pid: int) -> None:
    """The process exists now; remember its pid so liveness can be checked."""
    if record is None:
        return
    try:
        data = json.loads(record.path.read_text())
        data["pid"] = int(pid)
        record._write(data)
    except (OSError, ValueError):
        pass


def finish(record: Record | None) -> None:
    """The command reached an outcome the caller is reporting normally
    (an exit status, or a timeout the backend already explains), so the
    crash accounting has nothing left to say: remove the record."""
    if record is None:
        return
    for path in (record.path, record.out_path, record.err_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True  # exists but not ours
    return True


def _output_tail(record: Record) -> str:
    parts = []
    for path in (record.out_path, record.err_path):
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    combined = "\n".join(parts)
    if len(combined) > OUTPUT_TAIL_CHARS:
        combined = "…" + combined[-OUTPUT_TAIL_CHARS:]
    return combined


def report(root: Path) -> list[str]:
    """Notices for commands that were in flight when a previous session died.

    Consuming: each orphan is reported once, then its files are removed - the
    notice carries everything worth keeping. Records whose process is still
    alive are left alone (another session may legitimately be running it).
    """
    directory = _inflight_dir(Path(root))
    try:
        record_paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    notices = []
    for path in record_paths:
        record = Record(path)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            finish(record)  # a half-record says nothing reliable; clear it
            continue
        pid = data.get("pid")
        if pid is not None and _alive(int(pid)):
            continue
        command = data.get("command", "?")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("started_at", 0)))
        if pid is None:
            fate = ("its start was never recorded, so it may not have run at all; "
                    "check before re-running anything destructive")
        else:
            fate = "it was interrupted before an exit status was recorded"
        notice = (f"⚠ A command was in flight when a previous session ended "
                  f"(started {when}): `{command}` — {fate}.")
        tail = _output_tail(record)
        if tail:
            notice += f"\nCaptured output before the interruption:\n{tail}"
        notices.append(notice)
        finish(record)
    return notices
