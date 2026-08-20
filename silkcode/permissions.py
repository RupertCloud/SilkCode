"""Command risk classification and permission modes (SRS sections 30-31)."""

from __future__ import annotations

import re
import shlex
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


# Where one command ends and the next begins.
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", "\n"}


def split_commands(command: str) -> list[list[str]] | None:
    """The individual commands in a shell line, each as its argv.

    None when the line cannot be read as shell at all (an unterminated
    quote). The caller must treat that as dangerous rather than guessing:
    something is going to run, and we do not know what.

    This exists because a permission decision made on the raw text is a
    decision about the wrong object. The shell sees `"git" push`, `g\\it push`
    and `gi""t push` as `git push`; a regex over the string sees three
    different things and matches none of them. Resolving quotes and escapes
    first means the gate classifies the command that will actually run.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SHELL_OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _runs_something_we_cannot_see(argv: list[str]) -> bool:
    """Whether the command word itself is produced at run time.

    `$CMD push`, `$(which git) push` and `` `echo git` push `` all decide what
    to execute after this gate has had its say. There is no reading of that
    which is safe to assume, so it is treated as outward-facing. Expansions in
    *arguments* are left alone — `git commit -m "$MSG"` is how everyone writes
    a commit, and gating it would make the prompt meaningless.
    """
    return bool(argv) and any(ch in argv[0] for ch in ("$", "`"))


def classify_command(command: str) -> Risk:
    # The raw text first: deliberately over-approximate, so a dangerous string
    # is caught wherever it appears, including inside quotes.
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, command):
            return Risk.HIGH

    segments = split_commands(command)
    if segments is None:
        return Risk.HIGH  # unreadable: fail closed, ask the human

    # Then the commands as the shell will actually see them. Matching a
    # normalized form can only find more than the raw pass did, never less,
    # so this closes the quoting bypass without loosening anything.
    for argv in segments:
        if _runs_something_we_cannot_see(argv):
            return Risk.HIGH
        normalized = " ".join(argv)
        for pattern in HIGH_RISK_PATTERNS:
            if re.search(pattern, normalized):
                return Risk.HIGH

    if not segments:
        return Risk.MEDIUM
    return max(_classify_argv(a) for a in segments)


def _classify_segment(segment: str) -> Risk:
    """Risk of one command written as text. Kept for callers that have a
    string; the argv form below is what the gate uses."""
    argv = (split_commands(segment) or [[]])
    return _classify_argv(argv[0]) if argv and argv[0] else Risk.MEDIUM


def _classify_argv(tokens: list[str]) -> Risk:
    if not tokens:
        return Risk.MEDIUM
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
    """Map a command to a grantable git operation, or None.

    Parsed rather than matched, for the same reason classification is: a grant
    is about what will run, and `"git" push` is a push. Only a line that is a
    single command can carry a grant — `ls && git push` is not a push the user
    pre-authorized, it is a push with something else attached.
    """
    segments = split_commands(command)
    if not segments or len(segments) != 1:
        return None
    argv = segments[0]
    if argv[:2] == ["github", "merge-pr"]:
        return "merge"
    if argv[0] == "git" and len(argv) > 1:
        return _GIT_OP_BY_SUBCOMMAND.get(argv[1])
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
