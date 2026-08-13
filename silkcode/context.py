"""Session context assembly: repo map, project instructions, memory, skills."""

from __future__ import annotations

from .memory import load_memory
from .repomap import repo_map
from .skills import load_skills
from .workspace import Workspace

INSTRUCTIONS_FILE = "SILKCODE.md"
MAX_INSTRUCTIONS_CHARS = 8_000


def load_project_instructions(ws: Workspace) -> str:
    path = ws.root / INSTRUCTIONS_FILE
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace").strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        text = text[:MAX_INSTRUCTIONS_CHARS] + "\n... [truncated]"
    return text


def build_context(ws: Workspace) -> str:
    parts = [repo_map(ws)]
    instructions = load_project_instructions(ws)
    if instructions:
        parts.append(f"Project instructions ({INSTRUCTIONS_FILE}) - follow these:\n{instructions}")
    memory = load_memory(ws)
    if memory:
        parts.append(f"Project memory (.silkcode/memory.md):\n{memory}")
    skills = load_skills(ws)
    if skills:
        lines = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
        parts.append(
            "Available skills (load one with the use_skill tool when relevant):\n" + lines
        )
    return "\n\n".join(parts)
