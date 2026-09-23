"""The per-workspace .silkcode/ directory, and keeping it out of the repo.

Silk Code writes state into the project it is working on: the advisory
workspace lock, project memory, skills. That directory is ours, not the
user's, and it must not show up in their work.

Left untracked it is a nuisance - `git status` is permanently dirty. But the
real problem is the agent: `git add -A && git commit` is an ordinary thing
for it to do, and it would commit a lock file naming a pid, plus whatever
notes memory holds, into the user's repository and push them. The directory
therefore ignores itself from the moment it is created.
"""

from __future__ import annotations

from pathlib import Path

STATE_DIRNAME = ".silkcode"

# "*" covers the .gitignore itself, so the whole directory disappears from
# git's view without touching the project's own .gitignore - nothing of the
# user's is edited to accommodate us.
GITIGNORE = (
    "# Silk Code's per-workspace state (lock, memory, skills).\n"
    "# Not part of your project; this file keeps it out of git.\n"
    "*\n"
)


def state_dir(root: Path) -> Path:
    """Create (if needed) `root/.silkcode`, self-ignoring, and return it."""
    directory = Path(root) / STATE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    ensure_ignored(directory)
    return directory


def ensure_ignored(directory: Path) -> None:
    """Write the self-ignoring .gitignore if it is not already there.

    Never overwrites: a user who deliberately edited it gets to keep their
    version. Failures are silent - a read-only checkout still has to work,
    and this is hygiene rather than correctness.
    """
    marker = Path(directory) / ".gitignore"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(GITIGNORE)
    except OSError:
        pass
