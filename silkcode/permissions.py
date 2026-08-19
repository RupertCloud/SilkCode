"""Command risk classification and permission modes (SRS sections 30-31)."""

from __future__ import annotations

import re
import weakref
from enum import IntEnum
from typing import Callable


class Risk(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2


HIGH_RISK_PATTERNS = [
    r"\bgithub merge-pr\b",  # merging a PR is as outward-facing as a push
    r"\brm\s+-[a-zA-Z-]*[rf]",
    r"\bsudo\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+checkout\s+(--(\s|$)|\.(\s|$)|.*\s-f\b)",
    r"\bgit\s+restore\b",
    r"\bgit\s+clean\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"curl[^|]*\|\s*(ba|z)?sh",
    r"wget[^|]*\|\s*(ba|z)?sh",
    # Piping anything into an interpreter runs unreviewed content, whatever
    # produced it: 'curl x | sh' is the famous case, but 'cat payload | sh'
    # and 'fetch | python' are the same action with the download hidden a
    # step earlier.
    r"\|\s*(ba|z|k|da)?sh\b",
    r"\|\s*(python\d?(\.\d+)?|perl|ruby|node|php)\b",
    # eval executes text assembled at runtime - the shell's own back door
    r"\beval\b",
]

LOW_RISK_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "which", "whoami",
    "date", "grep", "rg", "find", "du", "df", "file", "stat", "tree",
    "uname", "pytest",
}

GIT_READ_SUBCOMMANDS = {"status", "log", "diff", "show", "blame", "shortlog", "describe", "branch", "remote"}


def classify_command(command: str) -> Risk:
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command):
            return Risk.HIGH
    segments = [s.strip() for s in re.split(r"&&|\|\||;|\|", command) if s.strip()]
    if not segments:
        return Risk.MEDIUM
    return max(_classify_segment(s) for s in segments)


def _classify_segment(segment: str) -> Risk:
    tokens = segment.split()
    head = tokens[0]
    if head == "git":
        if len(tokens) > 1 and tokens[1] in GIT_READ_SUBCOMMANDS:
            return Risk.LOW
        return Risk.MEDIUM
    # test runners are low risk per SRS section 30
    if head in ("npm", "cargo", "go", "flutter", "yarn") and tokens[1:2] == ["test"]:
        return Risk.LOW
    if head in LOW_RISK_COMMANDS:
        if "-delete" in tokens or "-exec" in tokens:
            return Risk.MEDIUM
        return Risk.LOW
    return Risk.MEDIUM


# Asker callback: takes a human-readable prompt, returns "yes", "no", or "always".
Asker = Callable[[str], str]

# Operations a user can pre-authorize (SRS section 31, Custom policies).
GRANTABLE = ("pull", "push", "commit", "merge")

_GIT_OP_BY_SUBCOMMAND = {
    "pull": "pull", "fetch": "pull",
    "push": "push",
    "commit": "commit",
    "merge": "merge",
}


def git_operation(command: str) -> str | None:
    """Map a command to a grantable git operation, or None."""
    if re.match(r"\s*github merge-pr\b", command):
        return "merge"
    match = re.match(r"\s*git\s+(\w+)", command)
    if match:
        return _GIT_OP_BY_SUBCOMMAND.get(match.group(1))
    return None


class PermissionManager:
    """Permission modes per SRS section 31.

    - ask:   prompt for file writes and all non-LOW commands.
    - edit:  file writes allowed; prompt for non-LOW commands.
    - agent: file writes and MEDIUM commands allowed; HIGH commands prompt.

    HIGH-risk commands always prompt, in every mode, and cannot be
    permanently allowed for the session - unless the user explicitly picks
    "yes to all" (see allow_all), which approves every request for the rest
    of the session.
    """

    MODES = ("ask", "edit", "agent")

    def __init__(self, mode: str = "ask", asker: Asker | None = None,
                 grants: set[str] | list[str] | None = None,
                 provenance=None):
        if mode not in self.MODES:
            raise ValueError(f"Unknown permission mode '{mode}'; expected one of {self.MODES}")
        self.mode = mode
        self.asker: Asker = asker or (lambda prompt: "no")
        self.grants: set[str] = {g for g in (grants or []) if g in GRANTABLE}
        # Where this turn's instructions came from. Only the human's request
        # can authorize a consequential action; anything the agent read is
        # data. See silkcode/provenance.py.
        self._watched: "weakref.WeakSet" = weakref.WeakSet()
        self._provenance = None
        self.provenance = provenance
        self._always_write = False
        self._always_commands: set[str] = set()
        self._always_all = False

    def allow_all(self) -> None:
        """Approve every permission request for the rest of the session.

        Set by the GUI's "Yes to all" button. Overrides the per-command and
        per-write caches and even HIGH-risk prompts - the user asked for no
        further prompts this session."""
        self._always_all = True

    def check_write(self, path: str) -> bool:
        if self._always_all or self.mode in ("edit", "agent") or self._always_write:
            return True
        decision = self.asker(f"Allow modifying file: {path}")
        if decision == "always":
            self._always_write = True
            return True
        return decision == "yes"

    @property
    def provenance(self):
        """The most recently registered turn record, kept for callers that
        read this attribute directly. The gate itself uses every watched
        record, not just this one."""
        return self._provenance

    @provenance.setter
    def provenance(self, value) -> None:
        self._provenance = value
        self.watch(value)

    def watch(self, provenance) -> None:
        """Follow an agent's turn record. A swarm gives several agents one
        permission manager, and any of them can be the one that read a
        poisoned file, so this tracks all of them rather than only the first.
        The reference is weak and counts only while a turn is running: a
        worker that finished an hour ago must not still be gating pushes."""
        if provenance is not None:
            self._watched.add(provenance)
            if self._provenance is None:
                self._provenance = provenance

    def _live(self) -> list:
        """Turn records for turns that are actually running right now."""
        return [p for p in list(self._watched) if getattr(p, "active", False)]

    def _tainted(self) -> bool:
        """Whether this turn has read content written to steer the agent."""
        return any(p.tainted for p in self._live())

    def _context(self) -> str:
        """Provenance to show with a prompt, as a trailing block or ""."""
        parts = [p.explain() for p in self._live()]
        joined = "\n".join(p for p in parts if p)
        return f"\n\n{joined}" if joined else ""

    def check_command(self, command: str) -> bool:
        risk = classify_command(command)
        # An action that reaches outside this machine is the one case where a
        # standing approval is not enough: if this turn read something that
        # tried to give the agent orders, the human sees it before it lands.
        outward = risk == Risk.HIGH
        if self._always_all and not (outward and self._tainted()):
            return True
        if risk == Risk.LOW:
            return True
        operation = git_operation(command)
        if operation is not None and operation in self.grants \
                and not (outward and self._tainted()):
            return True
        if risk == Risk.MEDIUM:
            if self.mode == "agent":
                return True
            head = command.strip().split()[0] if command.strip() else command
            if head in self._always_commands:
                return True
            decision = self.asker(f"Run command ({risk.name} risk): {command}"
                                  + self._context())
            if decision == "always":
                self._always_commands.add(head)
                return True
            return decision == "yes"
        return self.asker(f"Run HIGH-RISK command: {command}"
                          + self._context()) == "yes"

    def check_mcp(self, qualified_name: str) -> bool:
        """External MCP tools are treated like medium-risk commands."""
        if self._always_all or self.mode == "agent" or qualified_name in self._always_commands:
            return True
        decision = self.asker(f"Call MCP tool: {qualified_name}")
        if decision == "always":
            self._always_commands.add(qualified_name)
            return True
        return decision == "yes"
