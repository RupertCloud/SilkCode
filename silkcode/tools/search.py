"""Repository search tools: glob and grep (SRS section 27)."""

from __future__ import annotations

import re

from ..workspace import ToolError, Workspace

MAX_RESULTS = 200
MAX_FILE_BYTES = 1_000_000


def glob_files(ws: Workspace, pattern: str) -> str:
    try:
        matches = sorted(
            ws.relative(p)
            for p in ws.root.glob(pattern)
            if ".git" not in p.parts
        )
    except (ValueError, NotImplementedError) as exc:
        raise ToolError(f"Invalid glob pattern '{pattern}': {exc}") from exc
    if not matches:
        return "No files matched."
    truncated = len(matches) > MAX_RESULTS
    body = "\n".join(matches[:MAX_RESULTS])
    if truncated:
        body += f"\n... ({len(matches) - MAX_RESULTS} more matches)"
    return body


def grep(ws: Workspace, pattern: str, path: str = ".", glob: str = "**/*") -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"Invalid regex '{pattern}': {exc}") from exc
    base = ws.resolve(path)
    if base.is_file():
        candidates = [base]
    else:
        candidates = sorted(p for p in base.glob(glob) if p.is_file())
    results: list[str] = []
    for f in candidates:
        if ".git" in f.parts:
            continue
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:1024]:
            continue
        text = raw.decode("utf-8", "replace")
        rel = ws.relative(f)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{rel}:{lineno}:{line.strip()[:200]}")
                if len(results) >= MAX_RESULTS:
                    return "\n".join(results) + "\n... [more matches truncated]"
    return "\n".join(results) if results else "No matches found."
