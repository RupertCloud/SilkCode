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

import hashlib
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
    """Where a GitHub repo is cloned locally.

    The directory name must identify exactly one repository. Joining owner and
    repo with a "-" does not: "-" is legal inside both, so facebook/react-native
    and facebook-react/native produce the same name. That is not a cosmetic
    clash - clone_github_repo reuses any directory that already has a .git, so
    the second repo would silently hand back the first one's working tree under
    the second one's label, and a push would land in the wrong repository.
    A digest of the exact "owner/repo" keeps them apart.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", f"{owner}-{repo}")
    digest = hashlib.sha256(f"{owner}/{repo}".encode()).hexdigest()[:8]
    return projects_dir() / f"{safe}-{digest}"


def _legacy_path_for_github(owner: str, repo: str) -> Path:
    """Where clones lived before the name carried a digest (see
    local_path_for_github). Kept only to adopt an existing checkout."""
    return projects_dir() / re.sub(r"[^A-Za-z0-9._-]", "-", f"{owner}-{repo}")


def _origin_url(path: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _adopt_legacy_clone(owner: str, repo: str, dest: Path) -> None:
    """Move a pre-digest clone to its new home, so upgrading does not strand
    the checkout the user has been working in.

    Only when the old directory really is this repository: the old name is
    ambiguous by construction, so adopting it blindly is the very mix-up the
    digest exists to prevent. The origin URL settles it.
    """
    legacy = _legacy_path_for_github(owner, repo)
    if dest.exists() or not (legacy / ".git").is_dir():
        return
    url = _origin_url(legacy)
    tail = url.rstrip("/").removesuffix(".git").lower()
    if not tail.endswith(f"{owner}/{repo}".lower()):
        return  # some other repository happens to share the old name
    try:
        legacy.rename(dest)
    except OSError:
        pass  # keep the old checkout where it is and clone fresh


def clone_github_repo(owner: str, repo: str, token: str | None = None) -> Workspace:
    """Clone (or update) a GitHub repo into our managed projects dir.

    Credentials always come from the configured GitHub token, because the
    picker lists whatever the token can see - private repositories included -
    and a repo you were offered has to be a repo you can actually clone. The
    `token` argument is kept for callers that pass one, but it is not the
    source of the credential: git_credential_env reads the configured token
    and feeds it to git through an askpass helper, so it never appears in a
    URL or in .git/config.
    """
    from .github import git_credential_env

    dest = local_path_for_github(owner, repo)
    _adopt_legacy_clone(owner, repo, dest)
    env = dict(os.environ)
    cred = git_credential_env()
    if cred:
        env.update(cred)

    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{owner}/{repo}.git"
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
            # the same credentials: fetching a private repo needs them too
            subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune"],
                           capture_output=True, text=True, timeout=300, env=env)
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
