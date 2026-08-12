"""Session persistence shared by GUI and CLI (SRS sections 44 and 47)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import config_dir


class SessionStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or (config_dir() / "sessions")
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: int) -> Path:
        return self.dir / f"{session_id}.json"

    def new_id(self) -> int:
        existing = [int(p.stem) for p in self.dir.glob("*.json") if p.stem.isdigit()]
        return max(existing, default=0) + 1

    def save(self, data: dict) -> None:
        data["updated"] = time.time()
        self._path(data["id"]).write_text(json.dumps(data, indent=2))

    def load(self, session_id: int) -> dict:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"No session #{session_id} in {self.dir}")
        return json.loads(path.read_text())

    def list(self) -> list[dict]:
        sessions = []
        for path in self.dir.glob("*.json"):
            if not path.stem.isdigit():
                continue
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            sessions.append({
                "id": data.get("id"),
                "title": data.get("title", ""),
                "model": data.get("model", ""),
                "cwd": data.get("cwd", ""),
                "updated": data.get("updated", 0),
            })
        return sorted(sessions, key=lambda s: s["updated"], reverse=True)


def new_session(session_id: int, title: str, model: str, cwd: str, mode: str) -> dict:
    return {
        "id": session_id,
        "title": title[:60],
        "model": model,
        "cwd": cwd,
        "mode": mode,
        "messages": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "created": time.time(),
        "updated": time.time(),
    }
