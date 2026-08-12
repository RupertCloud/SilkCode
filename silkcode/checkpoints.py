"""Basic checkpoints (SRS sections 28 and 72).

Snapshots each file before its first automated modification in a turn, so a
turn's changes can be reverted as a unit.
"""

from __future__ import annotations

from pathlib import Path


class Checkpoints:
    def __init__(self):
        self._generations: list[list[tuple[Path, str | None]]] = []
        self._current: list[tuple[Path, str | None]] | None = None

    def begin(self) -> None:
        self._current = []
        self._generations.append(self._current)

    def snapshot(self, path: Path) -> None:
        if self._current is None:
            self.begin()
        if any(existing == path for existing, _ in self._current):
            return
        content = path.read_text(errors="replace") if path.exists() else None
        self._current.append((path, content))

    def revert_last(self) -> list[str]:
        """Undo the most recent non-empty generation. Returns restored paths."""
        while self._generations and not self._generations[-1]:
            self._generations.pop()
        if not self._generations:
            return []
        generation = self._generations.pop()
        if self._current is generation:
            self._current = None
        restored = []
        for path, content in reversed(generation):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            restored.append(str(path))
        return restored
