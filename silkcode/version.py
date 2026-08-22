"""What am I actually running?

The release number alone cannot answer that here. `silkcode update` pulls
whatever is on a git remote into the installed checkout, and the GUI daemon
re-execs itself when new code lands — so everyone tracking `main` runs
different code while `__version__` says "0.1.0" for all of them. A bug report
that says "0.1.0" from one of those installs names a hundred commits.

So a build identity is the release plus, when the install is a git checkout,
the commit it is sitting on:

    0.1.0                     a wheel, exactly what was tagged
    0.1.0+gd4e5f6a            a checkout at that commit
    0.1.0+gd4e5f6a.dirty      ...with uncommitted changes

The suffix is PEP 440 local-version syntax, which pip understands and
comparison rules ignore — it identifies a build without pretending to order it.

None of this may fail loudly. A read-only install, a machine with no git, a
tarball with no `.git` — all of those are ordinary, and every one of them still
has to start. When the commit cannot be determined the release number stands on
its own, which is exactly right for a wheel.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__

#: The declared release. Single-sourced: pyproject.toml reads this attribute
#: (``[tool.setuptools.dynamic] version = {attr = "silkcode.__version__"}``),
#: so the literal lives in exactly one place.
RELEASE = __version__


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _git(*args: str) -> str | None:
    """Run git inside the installed package, or None if it cannot be run."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_package_dir()), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None          # no git on the machine, or it hung
    if proc.returncode != 0:
        return None          # not a work tree (a wheel install), or no commits
    return proc.stdout.strip()


@lru_cache(maxsize=1)
def commit() -> str | None:
    """The short commit this install sits on, or None for a wheel.

    Cached for the life of the process. That is not a staleness bug: after
    `silkcode update` the GUI daemon re-execs, and the CLI is a fresh process
    every time, so nothing survives a change in the underlying code.
    """
    return _git("rev-parse", "--short", "HEAD") or None


@lru_cache(maxsize=1)
def is_dirty() -> bool:
    """Whether the checkout has uncommitted changes (False when not a checkout)."""
    if commit() is None:
        return False
    return bool(_git("status", "--porcelain"))


@lru_cache(maxsize=1)
def build_id() -> str:
    """The string that identifies this build. Safe in a bug report."""
    sha = commit()
    if sha is None:
        return RELEASE
    return f"{RELEASE}+g{sha}" + (".dirty" if is_dirty() else "")


def branch() -> str | None:
    """The checked-out branch, or None (wheel install, or detached HEAD)."""
    name = _git("rev-parse", "--abbrev-ref", "HEAD")
    return None if name in (None, "HEAD") else name


def info() -> dict:
    """Everything worth putting in a bug report, as data."""
    import platform
    import sys
    return {
        "version": RELEASE,
        "build": build_id(),
        "commit": commit(),
        "branch": branch(),
        "dirty": is_dirty(),
        "install": str(_package_dir()),
        "source": "git checkout" if commit() else "package install",
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
    }


def report() -> str:
    """The human-readable version report behind `silkcode version`."""
    d = info()
    lines = [f"Silk Code {d['build']}"]
    if d["commit"]:
        where = f"commit {d['commit']}"
        if d["branch"]:
            where += f" on {d['branch']}"
        if d["dirty"]:
            where += " (uncommitted changes)"
        lines.append(f"  source    {where}")
        lines.append("  update    silkcode update")
    else:
        lines.append("  source    installed package (no git metadata)")
        # `silkcode update` handles this install too: it reinstalls from the
        # source pip recorded. This line used to read `pip install -U
        # silkcode`, sending the one person actively diagnosing an update
        # problem to a package that does not exist on PyPI.
        lines.append("  update    silkcode update")
    lines.append(f"  install   {d['install']}")
    lines.append(f"  python    {d['python']}  ({d['executable']})")
    lines.append(f"  platform  {d['platform']}")
    return "\n".join(lines)
