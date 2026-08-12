"""Workspace: the repository root all tools operate inside."""

from __future__ import annotations

from pathlib import Path


class ToolError(RuntimeError):
    """A tool-level failure that should be reported back to the model."""


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ToolError(f"Workspace root is not a directory: {self.root}")

    def resolve(self, path: str | Path) -> Path:
        p = Path(path)
        resolved = (p if p.is_absolute() else self.root / p).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError(f"Path '{path}' is outside the workspace root {self.root}")
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)
