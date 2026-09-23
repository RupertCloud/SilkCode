"""Lightweight symbol index (SRS section 23; tree-sitter comes later)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ..repomap import IGNORED_DIRS
from ..workspace import Workspace

MAX_FILES = 200
MAX_LINES = 400

JS_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:(?:async\s+)?function\s+(?P<func>\w+)"
    r"|class\s+(?P<cls>\w+)"
    r"|(?:const|let|var)\s+(?P<const>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)"
)
GO_PATTERN = re.compile(r"^\s*(?:func\s+(?:\([^)]*\)\s*)?(?P<func>\w+)|type\s+(?P<cls>\w+))")
RUST_PATTERN = re.compile(r"^\s*(?:pub\s+)?(?:fn\s+(?P<func>\w+)|struct\s+(?P<cls>\w+)|enum\s+(?P<enum>\w+)|trait\s+(?P<trait>\w+))")


# What each named capture group in the language patterns above is called in
# the output. Unknown group names fall back to "symbol" rather than raising,
# so adding a group to a pattern cannot break extraction for a whole file.
GROUP_LABELS = {"func": "function", "cls": "class", "const": "function",
                "enum": "enum", "trait": "trait"}


def _python_symbols(text: str, rel: str, out: list[str]) -> None:
    """Classes and functions from Python source.

    Takes text, not a path: a remote workspace's files are fetched from the
    sandbox and never exist locally. One implementation for both, so a fix
    here cannot apply to only half the callers.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.append(f"{rel}:{node.lineno}: class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            out.append(f"{rel}:{node.lineno}: def {node.name}({args})")


def _regex_symbols(text: str, rel: str, pattern: re.Pattern, out: list[str]) -> None:
    """Definitions from a non-Python language, matched line by line."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = pattern.match(line)
        if m:
            for kind, name in m.groupdict().items():
                if name:
                    label = GROUP_LABELS.get(kind, "symbol")
                    out.append(f"{rel}:{lineno}: {label} {name}")


def _extract(text: str, rel: str, out: list[str]) -> None:
    """Dispatch to the right extractor for this file's language."""
    if rel.endswith(".py"):
        _python_symbols(text, rel, out)
    elif rel.endswith((".js", ".jsx", ".ts", ".tsx")):
        _regex_symbols(text, rel, JS_PATTERN, out)
    elif rel.endswith(".go"):
        _regex_symbols(text, rel, GO_PATTERN, out)
    elif rel.endswith(".rs"):
        _regex_symbols(text, rel, RUST_PATTERN, out)


SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs")


def _collect_source_files(base: Path) -> list[Path]:
    """Bounded walk: prunes ignored/hidden dirs and stops at MAX_FILES, so
    huge directories can't hang the tool."""
    import os
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(SOURCE_SUFFIXES):
                files.append(Path(dirpath) / name)
                if len(files) >= MAX_FILES:
                    return files
    return files


def symbols(ws: Workspace, path: str = ".") -> str:
    from ..remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        # remote: source files are fetched one at a time from the sandbox
        if path != "." and ws.is_file(path.lstrip("/")):
            files = [path.lstrip("/")]
        else:
            files = [f for f in ws.list_files()
                     if f.endswith(SOURCE_SUFFIXES)
                     and not any(part in IGNORED_DIRS or part.startswith(".")
                                 for part in Path(f).parts)][:MAX_FILES]
        out: list[str] = []
        for rel in files:
            try:
                text = ws.read_text(rel)
            except Exception:
                continue
            _extract(text, rel, out)
            if len(out) >= MAX_LINES:
                out = out[:MAX_LINES]
                out.append("... [symbol list truncated]")
                break
        return "\n".join(out) if out else "No symbols found."
    base = ws.resolve(path)
    if base.is_file():
        files = [base]
    else:
        files = _collect_source_files(base)
    out: list[str] = []
    for f in files:
        rel = ws.relative(f)
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue  # unreadable file: skip it rather than lose the rest
        _extract(text, rel, out)
        if len(out) >= MAX_LINES:
            out = out[:MAX_LINES]
            out.append("... [symbol list truncated]")
            break
    return "\n".join(out) if out else "No symbols found."
