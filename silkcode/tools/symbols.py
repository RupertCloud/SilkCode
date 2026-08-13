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


def _python_symbols(path: Path, rel: str, out: list[str]) -> None:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.append(f"{rel}:{node.lineno}: class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            out.append(f"{rel}:{node.lineno}: def {node.name}({args})")


def _regex_symbols(path: Path, rel: str, pattern: re.Pattern, out: list[str]) -> None:
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        m = pattern.match(line)
        if m:
            for kind, name in m.groupdict().items():
                if name:
                    label = {"func": "function", "cls": "class", "const": "function",
                             "enum": "enum", "trait": "trait"}[kind]
                    out.append(f"{rel}:{lineno}: {label} {name}")


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
    base = ws.resolve(path)
    if base.is_file():
        files = [base]
    else:
        files = _collect_source_files(base)
    out: list[str] = []
    for f in files:
        rel = ws.relative(f)
        if f.suffix == ".py":
            _python_symbols(f, rel, out)
        elif f.suffix in (".js", ".jsx", ".ts", ".tsx"):
            _regex_symbols(f, rel, JS_PATTERN, out)
        elif f.suffix == ".go":
            _regex_symbols(f, rel, GO_PATTERN, out)
        elif f.suffix == ".rs":
            _regex_symbols(f, rel, RUST_PATTERN, out)
        if len(out) >= MAX_LINES:
            out = out[:MAX_LINES]
            out.append("... [symbol list truncated]")
            break
    return "\n".join(out) if out else "No symbols found."
