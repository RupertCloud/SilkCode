"""Test execution with framework detection (SRS section 34)."""

from __future__ import annotations

import json

from ..workspace import ToolError, Workspace
from . import shell


def detect_test_command(ws: Workspace) -> str | None:
    root = ws.root
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text()).get("scripts", {})
            if "test" in scripts:
                return "npm test"
        except ValueError:
            pass
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "pubspec.yaml").is_file():
        return "flutter test"
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return "pytest"
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "pytest" in pyproject.read_text(errors="replace"):
        return "pytest"
    for directory in ("tests", "test"):
        d = root / directory
        if d.is_dir() and (list(d.glob("test_*.py")) or list(d.glob("*_test.py"))):
            return "pytest"
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
