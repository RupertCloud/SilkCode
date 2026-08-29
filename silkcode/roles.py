"""Swarm roles as files, not constants.

The swarm's roles - tester, critic, worker, and the product team - are
prompts wired into prompts.py. That made them Silk Code's opinion of what a
critic is. This module makes them the *default* opinion: a markdown file in
`~/.silkcode/agents/` (user) or `<project>/.silkcode/agents/` (project,
which wins) redefines a role, and a file with a new name adds a read-only
specialist to the team's discovery phase.

    ---
    name: critic
    description: Reviews with our house style in mind
    model: deepseek            # optional: pin this role to a model
    ---
    You are the CRITIC. Weigh maintainability above all...

The same frontmatter convention as skills, on purpose - one format to learn.

Two deliberate limits. A definition can make a *built-in writing role* write
because the built-in already writes; a custom-named role is always read-only,
because a repository file must not be able to introduce a new writer into
the swarm. And every definition body is scanned like any other repository
text (provenance.py): a definition that reads as prompt injection is not
loaded, and the person is told - these files write system prompts, which is
exactly the authority the trust boundary exists to guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import config_dir
from .workspace import Workspace

MAX_ROLE_CHARS = 8_000

# Roles the swarm runs today. A definition with one of these names replaces
# the built-in prompt; whether the role writes is the built-in's decision.
BUILTIN_ROLES = ("tester", "critic", "worker",
                 "business", "user", "designer", "head", "developer")


@dataclass
class RoleDefinition:
    name: str
    description: str
    prompt: str
    model: str | None      # provider spec, e.g. "deepseek" - None = caller's
    path: Path
    custom: bool           # not one of BUILTIN_ROLES: read-only specialist


def role_dirs(ws: Workspace) -> list[Path]:
    return [config_dir() / "agents", ws.root / ".silkcode" / "agents"]


def load_roles(ws: Workspace) -> dict[str, RoleDefinition]:
    """Definitions by name, project over user; poisoned ones dropped."""
    from .provenance import scan
    from .skills import _first_meaningful_line, _split_frontmatter

    roles: dict[str, RoleDefinition] = {}
    for directory in role_dirs(ws):  # project dir last: overrides user roles
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(errors="replace")
            meta, body = _split_frontmatter(text)
            body = body.strip()[:MAX_ROLE_CHARS]
            if not body:
                continue
            if scan(body, str(path), addressed_to_agent=True):
                # Reported by callers via `withheld` below; never loaded.
                continue
            name = (meta.get("name") or path.stem).strip().lower()
            roles[name] = RoleDefinition(
                name=name,
                description=meta.get("description") or _first_meaningful_line(body),
                prompt=body,
                model=(meta.get("model") or "").strip() or None,
                path=path,
                custom=name not in BUILTIN_ROLES,
            )
    return roles


def withheld(ws: Workspace) -> list[str]:
    """Definition files that were not loaded, with the reason - for showing
    the person, the same way context.py surfaces a withheld SILKCODE.md."""
    from .provenance import scan
    from .skills import _split_frontmatter

    warnings = []
    for directory in role_dirs(ws):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            body = _split_frontmatter(path.read_text(errors="replace"))[1].strip()
            sightings = scan(body[:MAX_ROLE_CHARS], str(path), addressed_to_agent=True)
            if sightings:
                warnings.append(
                    f"⚠ agent definition {path} was not loaded: "
                    f"{sightings[0].reason} — “{sightings[0].excerpt}”")
    return warnings


def role_prompt(roles: dict[str, RoleDefinition], name: str, default: str) -> str:
    definition = roles.get(name)
    return definition.prompt if definition else default


def role_model(roles: dict[str, RoleDefinition], name: str, default: str) -> str:
    definition = roles.get(name)
    return definition.model if definition and definition.model else default


def custom_specialists(roles: dict[str, RoleDefinition]) -> list[RoleDefinition]:
    """User-defined roles, which join the team's read-only discovery phase."""
    return [d for d in roles.values() if d.custom]
