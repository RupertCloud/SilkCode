"""Command risk classification and permission modes (SRS sections 30-31)."""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Callable


class Risk(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2


HIGH_RISK_PATTERNS = [
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
    if head == "npm" and tokens[1:2] == ["test"]:
        return Risk.LOW
    if head in LOW_RISK_COMMANDS:
        if "-delete" in tokens or "-exec" in tokens:
            return Risk.MEDIUM
        return Risk.LOW
    return Risk.MEDIUM


# Asker callback: takes a human-readable prompt, returns "yes", "no", or "always".
Asker = Callable[[str], str]


class PermissionManager:
    """Permission modes per SRS section 31.

    - ask:   prompt for file writes and all non-LOW commands.
    - edit:  file writes allowed; prompt for non-LOW commands.
    - agent: file writes and MEDIUM commands allowed; HIGH commands prompt.

    HIGH-risk commands always prompt, in every mode, and cannot be
    permanently allowed for the session.
    """

    MODES = ("ask", "edit", "agent")

    def __init__(self, mode: str = "ask", asker: Asker | None = None):
        if mode not in self.MODES:
            raise ValueError(f"Unknown permission mode '{mode}'; expected one of {self.MODES}")
        self.mode = mode
        self.asker: Asker = asker or (lambda prompt: "no")
        self._always_write = False
        self._always_commands: set[str] = set()

    def check_write(self, path: str) -> bool:
        if self.mode in ("edit", "agent") or self._always_write:
            return True
        decision = self.asker(f"Allow modifying file: {path}")
        if decision == "always":
            self._always_write = True
            return True
        return decision == "yes"

    def check_command(self, command: str) -> bool:
        risk = classify_command(command)
        if risk == Risk.LOW:
            return True
        if risk == Risk.MEDIUM:
            if self.mode == "agent":
                return True
            head = command.strip().split()[0] if command.strip() else command
            if head in self._always_commands:
                return True
            decision = self.asker(f"Run command ({risk.name} risk): {command}")
            if decision == "always":
                self._always_commands.add(head)
                return True
            return decision == "yes"
        return self.asker(f"Run HIGH-RISK command: {command}") == "yes"
