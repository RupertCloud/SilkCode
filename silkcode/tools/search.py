"""Repository search tools: glob and grep (SRS section 27)."""

from __future__ import annotations

import re

from ..repomap import IGNORED_DIRS
from ..workspace import ToolError, Workspace

MAX_RESULTS = 200
MAX_FILE_BYTES = 1_000_000


def _asked_for_vendored(*specs: str) -> bool:
    """Whether the caller named a dependency directory outright.

    Skipping node_modules is a default, not a rule: someone who greps inside
    it, or globs node_modules/**/*.js, means it.
    """
    return any(part in IGNORED_DIRS
               for spec in specs
               for part in re.split(r"[/\\]", spec or ""))


def _is_vendored(path, base) -> bool:
    """True for a path inside a dependency or build directory.

    Searching those is worse than useless by default: results are sorted, so
    node_modules/... sorts ahead of src/..., and MAX_RESULTS is spent on
    vendored copies before the project's own code is ever reached - the one
    match the caller wanted ends up invisible.
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    return bool(set(parts) & IGNORED_DIRS)


def glob_files(ws: Workspace, pattern: str) -> str:
    from ..remotews import RemoteWorkspace, glob_matches
    if isinstance(ws, RemoteWorkspace):
        matches = sorted(f for f in ws.list_files() if glob_matches(f, pattern))
        if not matches:
            return "No files matched."
        truncated = len(matches) > MAX_RESULTS
        body = "\n".join(matches[:MAX_RESULTS])
        if truncated:
            body += f"\n... ({len(matches) - MAX_RESULTS} more matches)"
        return body
    try:
        keep_all = _asked_for_vendored(pattern)
        matches = sorted(
            ws.relative(p)
            for p in ws.root.glob(pattern)
            if keep_all or not _is_vendored(p, ws.root)
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
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"Invalid regex '{pattern}': {exc}") from exc
        results = ws.backend.grep(ws.workspace_id, pattern, path=path, glob=glob)
        return "\n".join(results) if results else "No matches found."
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"Invalid regex '{pattern}': {exc}") from exc
    base = ws.resolve(path)
    if base.is_file():
        candidates = [base]
    else:
        candidates = sorted(p for p in base.glob(glob) if p.is_file())
    keep_all = _asked_for_vendored(path, glob)
    results: list[str] = []
    for f in candidates:
        if not keep_all and _is_vendored(f, ws.root):
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
