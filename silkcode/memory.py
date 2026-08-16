"""Project memory (SRS section 43): a plain, inspectable markdown file."""

from __future__ import annotations

import datetime
from pathlib import Path

from .workspace import Workspace

MEMORY_RELPATH = ".silkcode/memory.md"
MAX_MEMORY_CHARS = 8_000


def memory_path(ws: Workspace) -> Path:
    return ws.root / MEMORY_RELPATH


def load_memory(ws: Workspace) -> str:
    from .remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        # the repo's memory lives in the sandbox clone, not on this machine
        try:
            if not ws.is_file(MEMORY_RELPATH):
                return ""
            text = ws.read_text(MEMORY_RELPATH).strip()
        except Exception:
            return ""
        if len(text) > MAX_MEMORY_CHARS:
            text = text[-MAX_MEMORY_CHARS:]
            text = "... [older memory truncated]\n" + text
        return text
    path = memory_path(ws)
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace").strip()
    if len(text) > MAX_MEMORY_CHARS:
        text = text[-MAX_MEMORY_CHARS:]
        text = "... [older memory truncated]\n" + text
    return text


def remember(ws: Workspace, text: str) -> str:
    from .remotews import RemoteWorkspace

    text = text.strip()
    if not text:
        return "Nothing to remember (empty text)."
    stamp = datetime.date.today().isoformat()
    entry = f"- [{stamp}] {text}\n"

    if isinstance(ws, RemoteWorkspace):
        # A remote workspace's local root is a scratch directory that stays
        # empty on purpose - the repo lives in the sandbox. Appending there
        # would write the note somewhere load_memory never looks and the
        # session throws away, so the agent would be told it remembered
        # something that was gone immediately. Write it where it is read.
        try:
            existing = ws.read_text(MEMORY_RELPATH) if ws.is_file(MEMORY_RELPATH) else ""
        except Exception:
            existing = ""
        ws.write_text(MEMORY_RELPATH, existing + entry)
        ws.refresh()  # the file may be new; let the listing pick it up
        return f"Remembered in {MEMORY_RELPATH}: {text}"

    path = memory_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(entry)
    return f"Remembered in {MEMORY_RELPATH}: {text}"
