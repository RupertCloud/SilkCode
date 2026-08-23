"""Who asked for this? Tracking authority across a turn.

Silk Code's permission check sees a command string and nothing else. It
cannot tell `git push` that the user asked for from `git push` that a README
in the repository told the agent to run, because by the time the string
reaches the gate, everything that led to it has been forgotten.

The distinction matters because only one party can authorize a consequential
action: the person at the keyboard. Everything the agent reads on the way —
file contents, command output, a fetched page, an MCP result — is data. It
can *describe* an action and it can *suggest* one, but it cannot authorize
it. That is the boundary this module keeps.

What it does NOT do is decide. There is no reliable way to know from text
whether a push was implied by "ship the fix", and a system that guesses will
be wrong in both directions. Instead it records what the turn consumed,
notices when consumed content was written to steer an agent rather than to
inform one, and hands that to the human at the moment they are already being
asked. The human decides; they just get to decide knowing where the
suggestion came from.

Restraint is the whole design. A gate that interrupts often is answered with
"yes to all", and after that it protects nothing — so this only ever changes
a prompt that was going to happen anyway, except in the one narrow case
where content that tried to give the agent orders is followed by an action
that reaches outside the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Text aimed at an agent rather than at a reader. These are deliberately
# narrow: each names an agent or an instruction-set explicitly, because the
# cost of a false positive is a person being warned about their own README
# and learning to ignore the warning.
STEERING_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("overrides its instructions", re.compile(
        r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
        r"(previous|prior|earlier|above|system|all)\b[^.\n]{0,20}\b"
        r"(instruction|prompt|rule|direction|message)s?\b")),
    # A salutation, not a mention. The delimiter has to sit tight against the
    # noun — `silkcode --model x` and `agent, provider = make_agent(...)` are
    # a command line and a tuple unpack, and both used to match — and the rest
    # of the line has to ask for something.
    ("addresses the assistant directly", re.compile(
        r"(?i)^\s*(?:#+\s*|//\s*|\*\s*)?(?:note\s+to\s+)?"
        r"(ai|llm|assistant|agent|claude|chatgpt|gpt|copilot|silk\s*code)"
        r"[:,]\s+[^\n=]{0,80}?"
        # Lower-case verb, deliberately: "Silk Code: Run Tests" is a command
        # palette entry and "Agent: Install Dependencies" is a heading. Title
        # case is how software labels itself; instructions are written as
        # sentences.
        r"\b(?-i:run|execute|push|commit|delete|remove|send|curl|install|"
        r"fetch|disable|ignore|stop|do not|don't)\b", re.M)),
    # "You must run X" is how half the READMEs on earth are written, so the
    # imperative alone proves nothing. What distinguishes an injection is
    # that it names the reader as a machine and then addresses it: the
    # sentence has to do both.
    #
    # Third person is deliberately not matched. Widening this to "the agent
    # <modal> <verb>" reads "The model should execute in under 200ms" and
    # "The agent framework should install cleanly on Linux" as attacks, which
    # is how a warning becomes noise. The cost is a real gap: "The assistant
    # must run the deploy script" goes unnoticed. That gap is the price of a
    # warning people still read, and it is not the last line of defence —
    # the prompt itself is.
    ("tells the assistant what it must do", re.compile(
        r"(?i)\b(ai|llm|assistant|agent|model|copilot|chatgpt|claude)\b"
        r"[^.\n]{0,80}\byou\s+(must|should|need\s+to|are\s+required\s+to|"
        r"are\s+instructed\s+to)\b"
        r"[^.\n]{0,60}\b(run|execute|push|commit|delete|send|curl|install|"
        r"fetch|disable)\b")),
    # "System prompt" is the ordinary name for a thing this codebase is full
    # of; matching the phrase flagged our own prompts.py. What is suspect is
    # claiming one, not naming one.
    ("claims authority it cannot have", re.compile(
        r"(?i)(?:"
        r"\b(?:your|new|updated|revised|following)\s+system\s+(?:prompt|message)\b"
        r"|\b(?:override|replace|ignore)\s+(?:the\s+|your\s+)?system\s+"
        r"(?:prompt|message)\b"
        r"|^\s*system\s*(?:prompt|message)?\s*:\s*you\s+are\b"
        r"|\b(?:enter|enable|activate|switch\s+to)\s+developer\s+mode\b"
        r"|\badmin(?:istrator)?\s+override\b"
        r"|\belevated\s+permissions?\b"
        r")", re.M)),
    ("hides text from the reader", re.compile(
        r"(?i)<!--[^>]{0,200}\b(ignore|instruction|you must|agent|assistant)\b")),
)

# Sources whose content is not the user speaking. A tool result is the world
# answering a question; it carries no authority regardless of what it says.
UNTRUSTED_KINDS = ("file", "command", "web", "mcp", "tool")

# This runs on every tool result, so it has a latency budget. Five regexes
# over an unbounded `run_command` dump cost 440ms per megabyte, which is a
# tax on every turn to search the middle of a build log. Read the head and
# the tail — the head is where a file introduces itself and the tail is where
# text gets appended — and skip the middle of anything enormous.
HEAD_SCAN_CHARS = 128_000
TAIL_SCAN_CHARS = 32_000


@dataclass
class Sighting:
    """Content that read as an instruction to the agent."""

    source: str          # e.g. "read_file(.github/README.md)"
    reason: str          # which pattern, in words
    excerpt: str         # a short, sanitized quote for the human

    def describe(self) -> str:
        return f"{self.source} — {self.reason}: “{self.excerpt}”"


# The one pattern that means nothing in a file written to address an agent.
_SALUTATION = "addresses the assistant directly"


def scan(text: str, source: str, *, addressed_to_agent: bool = False) -> list[Sighting]:
    """Sightings of agent-directed instructions in `text`.

    `addressed_to_agent` is for content whose whole purpose is to address the
    agent — SILKCODE.md, project memory. Asking "does this talk to an AI?"
    about a file written to talk to an AI carries no information: measured on
    realistic instruction files, the salutation pattern flagged "Claude: run
    the tests before committing", "Assistant: do not commit generated files"
    and "Agent: push only to feature branches", and caught exactly one
    injection the other patterns did not already have. Three false alarms for
    one catch is a detector that costs more than it earns, so in those files
    it is not consulted.

    What still applies there is everything about content claiming an
    authority it does not have — overriding earlier instructions, asserting a
    system prompt, hiding text from the reader. Those mean the same thing
    wherever they appear.
    """
    if not text:
        return []
    if len(text) > HEAD_SCAN_CHARS + TAIL_SCAN_CHARS:
        text = text[:HEAD_SCAN_CHARS] + "\n" + text[-TAIL_SCAN_CHARS:]
    found: list[Sighting] = []
    for reason, pattern in STEERING_PATTERNS:
        if addressed_to_agent and reason == _SALUTATION:
            continue
        match = pattern.search(text)
        if not match:
            continue
        start = max(0, match.start() - 20)
        excerpt = " ".join(text[start:match.end() + 60].split())
        if len(excerpt) > 110:
            excerpt = excerpt[:110] + "…"
        found.append(Sighting(source=source, reason=reason, excerpt=excerpt))
    return found


# eq=False: identity, not value. Two agents mid-turn on the same request are
# two different turns, and the permission manager holds these in a WeakSet.
@dataclass(eq=False)
class TurnProvenance:
    """What the current turn was asked to do, and what it has since read."""

    request: str = ""                       # the human's words: the only authority
    sources: list[str] = field(default_factory=list)
    sightings: list[Sighting] = field(default_factory=list)
    # Whether a turn is running. A swarm's workers share one permission
    # manager, so it watches several of these at once; a finished worker must
    # not keep gating the session it was spawned from.
    active: bool = False

    def begin(self, request: str) -> None:
        self.request = request or ""
        self.sources = []
        self.sightings = []
        self.active = True

    def end(self) -> None:
        self.active = False

    def record(self, source: str, output: str, kind: str = "tool",
               addressed_to_agent: bool = False) -> None:
        """Note that the turn consumed `output` from `source`."""
        if kind not in UNTRUSTED_KINDS:
            return
        if source not in self.sources:
            self.sources.append(source)
        for sighting in scan(output, source, addressed_to_agent=addressed_to_agent):
            # one per source is enough to raise the question
            if not any(s.source == sighting.source for s in self.sightings):
                self.sightings.append(sighting)

    @property
    def tainted(self) -> bool:
        """Whether this turn has read content that tried to direct the agent."""
        return bool(self.sightings)

    def explain(self, limit: int = 3) -> str:
        """The context a human needs to answer a permission prompt well."""
        lines: list[str] = []
        if self.request:
            asked = " ".join(self.request.split())
            lines.append(f'You asked: "{asked[:160]}"'
                         + ("…" if len(asked) > 160 else ""))
        if self.sightings:
            lines.append("")
            lines.append("⚠ Content read during this turn tried to give the agent "
                         "instructions. Instructions in data cannot authorize an "
                         "action — only your request can:")
            for sighting in self.sightings[:limit]:
                lines.append(f"    • {sighting.describe()}")
            if len(self.sightings) > limit:
                lines.append(f"    • …and {len(self.sightings) - limit} more")
        elif self.sources:
            shown = ", ".join(self.sources[:4])
            more = f" (+{len(self.sources) - 4} more)" if len(self.sources) > 4 else ""
            lines.append(f"Read this turn: {shown}{more}")
        return "\n".join(lines)
