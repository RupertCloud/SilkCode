"""A plan the agent writes down and then works through.

Without this, a plan lives in one assistant message and dies with it: the
next turn paraphrases it, compaction drops it, and nothing ever records which
steps actually happened. The fix is the same one that works for people - the
plan is a file, and execution means checking items off.

The file is `.silkcode/plan.md`: plain markdown with checkbox lines, in the
self-ignoring state directory, editable by the person as easily as by the
agent. Markdown is the store, not a rendering of one - there is nothing here
that needs more structure than a checkbox.

    - [ ] not started      - [>] in progress
    - [x] done             - [-] skipped, with the reason appended

It pairs with the `plan` permission mode (permissions.py): a read-only mode
in which the agent can investigate and propose but not modify the project.
The plan file itself lives in state, not the project, so proposing is
possible in the very mode that forbids everything else. Approving is a human
act: the person reads the proposal and switches to edit or agent mode, and
the agent works through the checklist with `update_plan`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .workspace import Workspace

PLAN_RELPATH = ".silkcode/plan.md"

MARKERS = {"pending": " ", "in_progress": ">", "done": "x", "skipped": "-"}
STATUSES = {marker: status for status, marker in MARKERS.items()}
MAX_STEPS = 50

_STEP_LINE = re.compile(r"^- \[(.)\] (.*)$")


def plan_path(ws: Workspace) -> Path:
    return ws.root / PLAN_RELPATH


def _steps(text: str) -> list[tuple[str, str]]:
    """(marker, text) for each checkbox line, in order."""
    found = []
    for line in text.splitlines():
        match = _STEP_LINE.match(line.strip())
        if match and match.group(1) in STATUSES:
            found.append((match.group(1), match.group(2)))
    return found


def propose_plan(ws: Workspace, title: str, steps: str, verify: str = "") -> str:
    """Write a fresh plan: a title, one step per line, optional acceptance.

    A step may carry its own acceptance criterion after " => " - how anyone
    will know that step is done, which is the half a checklist usually
    forgets. `verify` is the end-to-end check for the whole plan.

    Replaces any previous plan - a new proposal supersedes the old one, and
    the old one is in git-less state, not anyone's work.
    """
    from .statedir import state_dir

    title = " ".join(title.split())
    raw = [" ".join(s.split()) for s in steps.splitlines()]
    raw = [s.lstrip("-").strip().removeprefix("[ ]").strip() for s in raw if s.strip()]
    if not title:
        return "A plan needs a title."
    if not raw:
        return "A plan needs at least one step (one per line)."
    if len(raw) > MAX_STEPS:
        return (f"That is {len(raw)} steps; a plan over {MAX_STEPS} is a "
                "task list nobody reads. Propose the phases instead.")

    lines = []
    for step in raw:
        text, _, acceptance = step.partition(" => ")
        lines.append(f"- [ ] {text.strip()}")
        if acceptance.strip():
            lines.append(f"      (done when: {acceptance.strip()})")
    body = f"# Plan: {title}\n\n" + "\n".join(lines) + "\n"
    verify = " ".join(verify.split())
    if verify:
        body += f"\nVerify: {verify}\n"
    state_dir(ws.root)
    plan_path(ws).write_text(body)
    return (f"Plan written to {PLAN_RELPATH} ({len(raw)} steps).\n\n{body}\n"
            "When the user approves it, work through the steps and mark each "
            "with update_plan as you go.")


def read_plan(ws: Workspace) -> str:
    path = plan_path(ws)
    if not path.is_file():
        return ("No plan. Propose one with propose_plan when the work has "
                "enough steps to lose track of.")
    return path.read_text(errors="replace")


def update_plan(ws: Workspace, step: int, status: str, note: str = "") -> str:
    """Mark step `step` (1-based) as pending/in_progress/done/skipped."""
    if status not in MARKERS:
        return f"Unknown status '{status}'; expected one of {', '.join(MARKERS)}."
    path = plan_path(ws)
    if not path.is_file():
        return "No plan to update; propose one first with propose_plan."

    lines = path.read_text(errors="replace").splitlines()
    index = 0
    for i, line in enumerate(lines):
        match = _STEP_LINE.match(line.strip())
        if match and match.group(1) in STATUSES:
            index += 1
            if index == step:
                text = match.group(2)
                if note.strip():
                    # a skipped step without a reason is a mystery in a week
                    text = f"{text} — {' '.join(note.split())}"
                lines[i] = f"- [{MARKERS[status]}] {text}"
                path.write_text("\n".join(lines) + "\n")
                message = f"Step {step} marked {status}."
                acceptance = _acceptance_after(lines, i)
                if status == "done" and acceptance:
                    message += (f"\nIts acceptance criterion: {acceptance} - "
                                "confirm this actually holds.")
                trailer = _verify_line(lines)
                if status == "done" and trailer and _all_done(path):
                    message += f"\nEvery step is done. End-to-end check: {trailer}"
                return message + "\n\n" + progress(ws)
    if index == 0:
        return "The plan has no steps; propose a fresh one with propose_plan."
    return f"No step {step}; the plan has {index}."


def _acceptance_after(lines: list[str], index: int) -> str:
    """The "(done when: ...)" annotation for the step at `lines[index]`."""
    if index + 1 < len(lines):
        match = re.match(r"^\s*\(done when: (.*)\)\s*$", lines[index + 1])
        if match:
            return match.group(1)
    return ""


def _verify_line(lines: list[str]) -> str:
    for line in lines:
        if line.strip().startswith("Verify: "):
            return line.strip().removeprefix("Verify: ")
    return ""


def _all_done(path: Path) -> bool:
    steps = _steps(path.read_text(errors="replace"))
    return bool(steps) and all(marker in ("x", "-") for marker, _ in steps)


def progress(ws: Workspace) -> str:
    """One line a person can read in a status bar."""
    path = plan_path(ws)
    if not path.is_file():
        return "No plan."
    steps = _steps(path.read_text(errors="replace"))
    if not steps:
        return "The plan has no steps."
    done = sum(1 for m, _ in steps if m == "x")
    skipped = sum(1 for m, _ in steps if m == "-")
    active = next((text for m, text in steps if m == ">"), None)
    line = f"{done}/{len(steps)} steps done" + (f", {skipped} skipped" if skipped else "")
    if active:
        line += f"; in progress: {active}"
    return line
