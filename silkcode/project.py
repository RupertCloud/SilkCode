"""Project selection for new sessions (CLI and GUI).

A "session" in Silk Code is tied to one workspace (a local directory).
The initial workspace comes from the launch path. A new session can point
at a different project — a local directory the user types in, or a GitHub
repository the user picks and we clone locally.

This module centralizes that choice so the CLI (a /project command) and the
GUI (a New-session modal) share the same behavior:
    * list GitHub repositories (recently used first)
    * clone a chosen GitHub repo into ~/.silkcode/projects/<owner>-<repo>
    * resolve an arbitrary local path into a Workspace
    * remember recently opened projects for quick re-selection
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir
from .workspace import ToolError, Workspace

# Directory (under SILKCODE_HOME) where GitHub repo clones are kept.
PROJECTS_SUBDIR = "projects"

# "github:owner/repo" or a plain local path.
GITHUB_SPEC = re.compile(r"^github[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")


@dataclass
class github_clone_spec:
    owner: str
    repo: str


@dataclass
class ProjectChoice:
    """What the user chose, plus the local workspace it resolves to."""

    label: str
    workspace: Workspace
    kind: str  # "github" | "local"
    github: github_clone_spec | None = None


def projects_dir() -> Path:
    d = config_dir() / PROJECTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_github_spec(spec: str) -> bool:
    return bool(GITHUB_SPEC.match(spec.strip()))


def github_repo_to_label(owner: str, repo: str) -> str:
    return f"github/{owner}/{repo}"


def parse_github_spec(spec: str) -> github_clone_spec:
    m = GITHUB_SPEC.match(spec.strip())
    if not m:
        raise ValueError(f"not a github spec: {spec}")
    return github_clone_spec(owner=m.group("owner"), repo=m.group("repo"))


def local_path_for_github(owner: str, repo: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", f"{owner}-{repo}")
    return projects_dir() / safe


def clone_github_repo(owner: str, repo: str, token: str | None = None) -> Workspace:
    """Clone (or update) a GitHub repo into our managed projects dir."""
    dest = local_path_for_github(owner, repo)
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").is_dir():
        url = f"https://github.com/{owner}/{repo}.git"
        env = dict(os.environ)
        if token:
            from .github import git_credential_env
            cred = git_credential_env()
            if cred:
                env.update(cred)
        try:
            proc = subprocess.run(
                ["git", "clone", url, str(dest)],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise ToolError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or "unknown git error"
            raise ToolError(f"git clone {owner}/{repo} failed: {detail.splitlines()[0]}")
    else:
        try:
            subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune"],
                           capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # offline is fine; use what we have
    return Workspace(str(dest))


def resolve_project(spec: str) -> ProjectChoice:
    """Turn a user-supplied string ('github:owner/repo' or a local path)
    into a concrete project we can point a session at."""
    spec = spec.strip()
    if not spec:
        raise ToolError("empty project spec")
    if is_github_spec(spec):
        choice = parse_github_spec(spec)
        ws = clone_github_repo(choice.owner, choice.repo)
        return ProjectChoice(
            label=github_repo_to_label(choice.owner, choice.repo),
            workspace=ws, kind="github", github=choice,
        )
    try:
        ws = Workspace(spec)
    except ToolError as exc:
        raise exc
    return ProjectChoice(label=str(ws.root), workspace=ws, kind="local")


# ---- recent project history (shared by CLI & GUI) ---------------------------

def recent_projects_path() -> Path:
    return config_dir() / "recent_projects.json"


def recent_projects(limit: int = 8) -> list[dict]:
    path = recent_projects_path()
    if not path.exists():
        return []
    import json
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return []
    items = data if isinstance(data, list) else []
    items = [i for i in items if i and {"kind", "spec", "label"}.issubset(i.keys())]
    return items[:limit]


def record_recent_project(kind: str, spec: str, label: str, limit: int = 8) -> None:
    """Remember a recently opened project, most recent first."""
    import json
    entry = {"kind": kind, "spec": spec, "label": label}
    earlier = [i for i in recent_projects() if i.get("spec") != spec]
    earlier.insert(0, entry)
    path = recent_projects_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(earlier[:limit], indent=2) + "\n")
    except OSError:
        pass


def available_projects() -> list[dict]:
    """Candidate projects the picker offers: GitHub repos + recent locals."""
    from .github import list_github_repos
    items: list[dict] = []
    for r in list_github_repos():
        items.append({
            "kind": "github",
            "spec": "github:" + r["full_name"],
            "label": "github/" + r["full_name"],
            "github_owner_repo": r["full_name"],
        })
    seen_specs = {i["spec"] for i in items}
    for recent in recent_projects():
        if recent["spec"] in seen_specs:
            continue
        seen_specs.add(recent["spec"])
        items.append(recent)
    return items


def prompt_for_project(asker=None) -> ProjectChoice:
    """Interactive project selection (CLI /project). Prompts to pick a GitHub
    repo or type a local path; returns the resolved ProjectChoice."""
    from .github import list_github_repos
    from .workspace import ToolError

    def _ask(prompt: str) -> str:
        # (asker is a stdin reader; default to plain input)
        return (asker(prompt) if asker else input(prompt)).strip()

    candidates = list_github_repos()
    print("\nOpen a project for the new session / switch to.")
    if candidates:
        print("\nGitHub repositories:")
        for i, r in enumerate(candidates[:15], 1):
            desc = r["description"]
            suffix = f"  — {desc[:60]}" if desc else ""
            print(f"  {i:>2}. github/{r['full_name']}{suffix}")
    recent = recent_projects(limit=5)
    if recent:
        print("\nRecent projects:")
        for r in recent:
            print(f"     {r['label']}")
    print("\n  type a directory path to open a local project\n  q. cancel")

    while True:
        try:
            choice = _ask("\nPick a project (#, path, or q): ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise ToolError("cancelled")
        if choice in ("q", "quit", "cancel"):
            raise ToolError("cancelled")
        if not choice:
            continue
        # a bare number picks a GitHub repo
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(candidates):
                full = candidates[idx]["full_name"]
                owner, sep, repo = full.partition("/")
                if not sep:
                    print("  (bad repo name, try again)")
                    continue
                try:
                    ws = clone_github_repo(owner, repo)
                except ToolError as exc:
                    print(f"  {exc}")
                    continue
                return ProjectChoice(
                    label=f"github/{full}", workspace=ws, kind="github",
                    github=github_clone_spec(owner=owner, repo=repo))
            print("  (no such number, try again)")
            continue
        try:
            return resolve_project(choice)
        except ToolError as exc:
            print(f"  {exc}")
