"""File tools: read, write, edit (SRS section 27)."""

from __future__ import annotations

from ..workspace import ToolError, Workspace

MAX_READ_CHARS = 50_000


def read_file(ws: Workspace, path: str, offset: int = 1, limit: int = 1000) -> str:
    p = ws.resolve(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    lines = p.read_text(errors="replace").splitlines()
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


def write_file(ws: Workspace, path: str, content: str) -> str:
    p = ws.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} characters to {ws.relative(p)}"


def edit_file(ws: Workspace, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = ws.resolve(path)
    if not p.is_file():
        raise ToolError(f"File not found: {path}")
    text = p.read_text()
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
    p.write_text(text)
    return f"Replaced {count if replace_all else 1} occurrence(s) in {ws.relative(p)}"
