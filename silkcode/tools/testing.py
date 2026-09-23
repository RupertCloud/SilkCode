"""Test execution with framework detection (SRS section 34)."""

from __future__ import annotations

import json
import shlex
import shutil
import sys

from ..workspace import ToolError, Workspace
from . import shell


def _pytest_command() -> str:
    """Run pytest via the current interpreter when the binary is not on PATH
    (e.g. inside a virtualenv that only has the module installed)."""
    if shutil.which("pytest"):
        return "pytest"
    return f"{shlex.quote(sys.executable)} -m pytest"


def detect_test_command(ws: Workspace) -> str | None:
    from ..remotews import RemoteWorkspace
    remote = isinstance(ws, RemoteWorkspace)
    files = ws.list_files() if remote else None

    def exists(rel: str) -> bool:
        if remote:
            return rel in files
        return (ws.root / rel).is_file()

    def read(rel: str) -> str:
        return ws.read_text(rel) if remote else (ws.root / rel).read_text(errors="replace")

    def has_test_files(directory: str) -> bool:
        """Test files directly in `directory`; "" means the workspace root."""
        if remote:
            prefix = directory + "/" if directory else ""
            names = [f[len(prefix):] for f in files if f.startswith(prefix)]
            return any("/" not in n and (n.startswith("test_") or n.endswith("_test.py"))
                       for n in names)
        d = ws.root / directory if directory else ws.root
        if not d.is_dir():
            return False
        return bool(list(d.glob("test_*.py")) or list(d.glob("*_test.py")))

    if exists("package.json"):
        try:
            scripts = json.loads(read("package.json")).get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except ValueError:
            pass
    if exists("Cargo.toml"):
        return "cargo test"
    if exists("go.mod"):
        return "go test ./..."
    if exists("pubspec.yaml"):
        return "flutter test"
    if exists("pytest.ini") or exists("conftest.py"):
        return _pytest_command()
    if exists("pyproject.toml") and "pytest" in read("pyproject.toml"):
        return _pytest_command()
    # "" is the workspace root: a flat project keeps test_foo.py next to
    # foo.py, with no tests/ directory and nothing to declare it.
    for directory in ("tests", "test", ""):
        if has_test_files(directory):
            return _pytest_command()
    return None


def run_tests(ws: Workspace, command: str | None = None, timeout: int = 300) -> str:
    command = command or detect_test_command(ws)
    if not command:
        raise ToolError(
            "No test framework detected (looked for package.json scripts.test, "
            "Cargo.toml, go.mod, pubspec.yaml, pytest configuration). "
            "Pass an explicit command."
        )
    return f"$ {command}\n" + shell.run_command(ws, command, timeout=timeout)
