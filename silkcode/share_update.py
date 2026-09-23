"""Turn local Git history into reviewable, link-free release-update drafts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .workspace import ToolError, Workspace


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"could not inspect Git history: {exc}") from exc
    if proc.returncode:
        return ""
    return proc.stdout.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip(" ,.;:-") + "…"


def _privacy_warnings(text: str) -> list[str]:
    checks = [
        (r"(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", "Possible GitHub token"),
        (r"(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})", "Possible API key"),
        (r"(?:/Users/|/home/|[A-Z]:\\Users\\)", "Local file path"),
        (r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "Email address"),
        (r"\b(?:confidential|internal only|do not share)\b", "Private-language marker"),
    ]
    return [label for pattern, label in checks if re.search(pattern, text, re.I)]


def build_share_update(workspace: Workspace) -> dict:
    root = Path(workspace.root)
    if not (root / ".git").exists():
        raise ToolError("Share update needs a Git repository with at least one commit.")

    branch = _git(root, "branch", "--show-current") or "current branch"
    upstream = _git(root, "rev-parse", "--abbrev-ref", "@{upstream}")
    range_spec = f"{upstream}..HEAD" if upstream else "HEAD"
    log_args = ["log", "--format=%s", "-8"]
    if upstream:
        log_args.append(range_spec)
    subjects = [line.strip() for line in _git(root, *log_args).splitlines() if line.strip()]
    if not subjects and upstream:
        subjects = [line.strip() for line in _git(root, "log", "--format=%s", "-5").splitlines()
                    if line.strip()]
        range_spec = "latest commits"
    if not subjects:
        raise ToolError("Share update needs a Git repository with at least one commit.")

    status_lines = [line for line in _git(root, "status", "--porcelain").splitlines() if line]
    project = root.name
    lead = subjects[0].rstrip(".")
    extras = subjects[1:4]
    work_note = (f" There {'is' if len(status_lines) == 1 else 'are'} {len(status_lines)} "
                 f"uncommitted {'change' if len(status_lines) == 1 else 'changes'} still in review."
                 if status_lines else "")

    x_parts = [f"Building {project}: {lead}."]
    if extras:
        x_parts.append("Also shipped: " + "; ".join(s.rstrip(".") for s in extras) + ".")
    x_parts.append("#BuildInPublic #SoftwareDevelopment")
    x_draft = _truncate("\n\n".join(x_parts), 280)

    bullets = "\n".join(f"• {subject}" for subject in subjects[:6])
    linkedin = (f"A development update from {project}\n\n"
                f"We’ve been working on the {branch} branch. The latest improvement: {lead}.\n\n"
                f"What changed:\n{bullets}\n\n"
                "The work is being reviewed and tested before release."
                f"{work_note}\n\n#SoftwareDevelopment #BuildInPublic")
    changelog = "## Development update\n\n" + "\n".join(
        f"- {subject}" for subject in subjects[:8])
    if status_lines:
        changelog += f"\n\n_There are {len(status_lines)} uncommitted changes still under review._"

    drafts = {"x": x_draft, "linkedin": linkedin, "changelog": changelog}
    warnings = sorted({warning for draft in drafts.values() for warning in _privacy_warnings(draft)})
    return {
        "project": project, "branch": branch, "range": range_spec,
        "commit_count": len(subjects), "uncommitted_count": len(status_lines),
        "drafts": drafts, "warnings": warnings,
    }
