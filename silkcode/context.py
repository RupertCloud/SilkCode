"""Session context assembly: repo map, project instructions, memory, skills.

Everything assembled here comes out of the repository, and a repository is
something you cloned. `SILKCODE.md`, `.silkcode/memory.md` and the skill
descriptions all reach the model without a tool call ever being made, which
makes them the one path into the conversation that `provenance.py` could not
see: it watches what tools return, and these arrive before the first tool runs.

So they are read the same way tool output is. A file that reads as ordinary
project instructions — which is very nearly all of them — is used exactly as
before. A file carrying text written to steer an agent is kept out of the
system prompt and reported to the person running Silk Code, and the turn is
marked as having consumed it, so an action that leaves the machine asks first
even under a standing grant.

The detection is deliberately imprecise and documented as such in
provenance.py. This raises the cost of hiding instructions in a repository; it
does not make it impossible. The control remains that consequential actions
stop and ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .memory import load_memory
from .repomap import repo_map
from .skills import load_skills
from .workspace import Workspace

INSTRUCTIONS_FILE = "SILKCODE.md"
MEMORY_LABEL = ".silkcode/memory.md"
SKILLS_LABEL = "project skills"
MAX_INSTRUCTIONS_CHARS = 8_000


def load_project_instructions(ws: Workspace) -> str:
    from .remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        try:
            if not ws.is_file(INSTRUCTIONS_FILE):
                return ""
            text = ws.read_text(INSTRUCTIONS_FILE).strip()
        except Exception:
            return ""
        if len(text) > MAX_INSTRUCTIONS_CHARS:
            text = text[:MAX_INSTRUCTIONS_CHARS] + "\n... [truncated]"
        return text
    path = ws.root / INSTRUCTIONS_FILE
    if not path.is_file():
        return ""
    text = path.read_text(errors="replace").strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        text = text[:MAX_INSTRUCTIONS_CHARS] + "\n... [truncated]"
    return text


def project_sources(ws: Workspace) -> list[tuple[str, str]]:
    """`(label, text)` for everything the repository puts in front of the
    model without a tool call.

    Skill *descriptions* only: a skill's body arrives through the `use_skill`
    tool, and tool results are already recorded as sources by the agent loop.
    """
    sources: list[tuple[str, str]] = []
    instructions = load_project_instructions(ws)
    if instructions:
        sources.append((INSTRUCTIONS_FILE, instructions))
    memory = load_memory(ws)
    if memory:
        sources.append((MEMORY_LABEL, memory))
    try:
        skills = load_skills(ws)
    except Exception:
        skills = {}
    if skills:
        described = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
        sources.append((SKILLS_LABEL, described))
    return sources


@dataclass
class ProjectContext:
    """The system-prompt context, plus anything the human should know about
    how it was assembled."""

    text: str
    warnings: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)


def assemble(ws: Workspace) -> ProjectContext:
    from .provenance import scan

    parts = [repo_map(ws)]
    warnings: list[str] = []
    withheld: list[str] = []

    for label, text in project_sources(ws):
        sightings = scan(text, label)
        if sightings:
            # Not loaded. A file in the repository does not get to give the
            # agent orders through the system prompt just because it is named
            # SILKCODE.md - that is the one place in this program where text
            # is treated as coming from the user.
            withheld.append(label)
            warnings.append(
                f"⚠ {label} was not loaded into the agent's instructions: "
                f"{sightings[0].reason} — “{sightings[0].excerpt}”. "
                "Instructions in a repository file cannot authorize an action. "
                "Read it yourself, and if it is legitimate, say so in your request."
            )
            continue
        if label == INSTRUCTIONS_FILE:
            parts.append(f"Project instructions ({INSTRUCTIONS_FILE}) - follow these:\n{text}")
        elif label == MEMORY_LABEL:
            parts.append(f"Project memory ({MEMORY_LABEL}):\n{text}")
        elif label == SKILLS_LABEL:
            parts.append(
                "Available skills (load one with the use_skill tool when relevant):\n" + text
            )
    return ProjectContext(text="\n\n".join(parts), warnings=warnings, withheld=withheld)


def build_context(ws: Workspace) -> str:
    """The context string for the system prompt. Callers that can show the
    user a notice should use `assemble` instead and surface its warnings."""
    return assemble(ws).text
