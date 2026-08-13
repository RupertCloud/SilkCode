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
    path = memory_path(ws)
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace").strip()
    if len(text) > MAX_MEMORY_CHARS:
        text = text[-MAX_MEMORY_CHARS:]
        text = "... [older memory truncated]\n" + text
    return text


def remember(ws: Workspace, text: str) -> str:
    text = text.strip()
    if not text:
        return "Nothing to remember (empty text)."
    path = memory_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    with path.open("a") as f:
        f.write(f"- [{stamp}] {text}\n")
    return f"Remembered in {MEMORY_RELPATH}: {text}"
