"""Reusable coding skills (SRS section 41).

Skills are markdown files in `~/.silkcode/skills/` (user) and
`<project>/.silkcode/skills/` (project; overrides user skills with the same
name). Optional frontmatter:

    ---
    name: react-expert
    description: Idiomatic React and hooks guidance
    ---
    ...instructions...
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import config_dir
from .workspace import ToolError, Workspace

MAX_SKILL_CHARS = 20_000


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def content(self) -> str:
        text = self.path.read_text(errors="replace")
        body = _split_frontmatter(text)[1]
        if len(body) > MAX_SKILL_CHARS:
            body = body[:MAX_SKILL_CHARS] + "\n... [skill truncated]"
        return body


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    meta: dict = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :]).lstrip("\n")
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip().lower()] = value.strip()
    return {}, text  # unterminated frontmatter: treat whole file as body


def _first_meaningful_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return ""


def skill_dirs(ws: Workspace) -> list[Path]:
    return [config_dir() / "skills", ws.root / ".silkcode" / "skills"]


def load_skills(ws: Workspace) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for directory in skill_dirs(ws):  # project dir last: overrides user skills
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(errors="replace")
            meta, body = _split_frontmatter(text)
            name = meta.get("name") or path.stem
            description = meta.get("description") or _first_meaningful_line(body)
            skills[name] = Skill(name=name, description=description, path=path)
    return skills


def use_skill(ws: Workspace, name: str) -> str:
    skills = load_skills(ws)
    skill = skills.get(name)
    if skill is None:
        known = ", ".join(sorted(skills)) or "(none installed)"
        raise ToolError(f"Unknown skill '{name}'. Available skills: {known}")
    return f"Skill '{skill.name}':\n{skill.content()}"
