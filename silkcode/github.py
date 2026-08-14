"""GitHub integration (SRS section 80, pulled forward from V0.3).

Authentication uses a personal access token from $GITHUB_TOKEN (override the
variable name with config `github.token_env`). The repository is detected
from the workspace's `origin` remote. GitHub Enterprise works by setting
config `github.api_url`.
"""

from __future__ import annotations

import os
import re
import subprocess

import httpx

from .workspace import ToolError, Workspace

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
AGENT_TASKS_API_VERSION = "2026-03-10"  # required by the /agents endpoints
REMOTE_PATTERN = re.compile(r"github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$")

# Test hook: replaced to inject a mock transport.
_make_client = lambda: httpx.Client(timeout=30.0)  # noqa: E731


def token_from_env(config_data: dict | None = None) -> str | None:
    """Token from the environment, falling back to one stored in the config
    (set via the GUI authorization page or 'silkcode connect github')."""
    github_cfg = (config_data or {}).get("github") or {}
    env = github_cfg.get("token_env", "GITHUB_TOKEN")
    return os.environ.get(env) or github_cfg.get("token")


def get_token(config) -> str | None:
    """Resolve the GitHub token, refreshing an expired device-flow token
    in place when a refresh token and client id are available."""
    import time as _time
    github_cfg = config.data.get("github") or {}
    env_token = os.environ.get(github_cfg.get("token_env", "GITHUB_TOKEN"))
    if env_token:
        return env_token
    token = github_cfg.get("token")
    if not token:
        return None
    expires_at = github_cfg.get("token_expires_at")
    if expires_at and _time.time() > expires_at:
        from .github_oauth import DeviceFlow, DeviceFlowError, client_id_from, store_token
        refresh_token = github_cfg.get("refresh_token")
        client_id = client_id_from(config.data)
        if refresh_token and client_id:
            try:
                data = DeviceFlow(client_id).refresh(refresh_token)
                store_token(config, data)
                return data["access_token"]
            except DeviceFlowError:
                return None
        return None
    return token


def git_credential_env() -> dict | None:
    """Environment that lets git push/pull authenticate to github.com over
    HTTPS with the configured token. Returns None when no token is set (git
    then uses the developer's own ambient credentials, e.g. SSH keys)."""
    from .config import Config, config_dir

    config = Config.load()
    token = get_token(config)
    if not token:
        return None
    askpass = config_dir() / "git-askpass.sh"
    if not askpass.exists():
        config_dir().mkdir(parents=True, exist_ok=True)
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  Username*) echo x-access-token ;;\n"
            "  *) printenv SILKCODE_GIT_TOKEN ;;\n"
            "esac\n"
        )
        askpass.chmod(0o755)
    return {
        "GIT_ASKPASS": str(askpass),
        "SILKCODE_GIT_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
    }


def detect_repo(ws: Workspace) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ws.root, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise ToolError(f"cannot detect GitHub repository: {exc}") from exc
    if proc.returncode != 0:
        raise ToolError("no 'origin' git remote found in this workspace")
    match = REMOTE_PATTERN.search(proc.stdout.strip())
    if not match:
        raise ToolError(f"origin remote is not a GitHub URL: {proc.stdout.strip()}")
    return match.group("owner"), match.group("repo")


class GitHubClient:
    def __init__(self, token: str, api_url: str = DEFAULT_API_URL):
        self.api_url = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        self._client = _make_client()

    def _request(self, method: str, path: str, api_version: str | None = None, **kwargs) -> dict | list:
        headers = dict(self._headers)
        if api_version:
            headers["X-GitHub-Api-Version"] = api_version
        try:
            resp = self._client.request(method, f"{self.api_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"GitHub request failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                pass
            raise ToolError(f"GitHub API error {resp.status_code}: {detail or resp.text[:200]}")
        return resp.json() if resp.content else {}

    def whoami(self) -> str:
        data = self._request("GET", "/user")
        return data.get("login", "?")

    def list_repositories(self) -> list[dict]:
        """Repositories the authenticated user can read, most recently pushed first.

        Complements the auto-detected 'origin' remote so the user can open a
        different project in a new session (SRS: new sessions ask for a project).
        """
        data = self._request(
            "GET", "/user/repos",
            params={"per_page": 100, "sort": "pushed", "visibility": "all"},
        )
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for r in data:
            full = r.get("full_name", "")
            if not full or full in seen:
                continue
            seen.add(full)
            out.append({"full_name": full, "description": r.get("description") or ""})
        return out

    def create_pull_request(self, owner: str, repo: str, title: str, head: str,
                            base: str, body: str = "", draft: bool = True) -> str:
        data = self._request("POST", f"/repos/{owner}/{repo}/pulls", json={
            "title": title, "head": head, "base": base, "body": body, "draft": draft,
        })
        return f"Created pull request #{data.get('number')}: {data.get('html_url')}"

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> str:
        data = self._request("GET", f"/repos/{owner}/{repo}/pulls",
                             params={"state": state, "per_page": 20})
        if not data:
            return f"No {state} pull requests."
        return "\n".join(f"#{p['number']} [{p['state']}] {p['title']} ({p['head']['ref']} -> {p['base']['ref']})"
                         for p in data)

    def list_issues(self, owner: str, repo: str, state: str = "open") -> str:
        data = self._request("GET", f"/repos/{owner}/{repo}/issues",
                             params={"state": state, "per_page": 20})
        issues = [i for i in data if "pull_request" not in i]
        if not issues:
            return f"No {state} issues."
        return "\n".join(f"#{i['number']} [{i['state']}] {i['title']}" for i in issues)

    def merge_pull_request(self, owner: str, repo: str, number: int,
                           method: str = "merge") -> str:
        if method not in ("merge", "squash", "rebase"):
            raise ToolError("merge method must be one of: merge, squash, rebase")
        data = self._request("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                             json={"merge_method": method})
        if not data.get("merged"):
            raise ToolError(f"merge failed: {data.get('message', 'unknown reason')}")
        return f"Merged pull request #{number} ({method}): {data.get('sha', '')[:10]}"

    # ---- Agent Tasks API (Copilot cloud agents, api version 2026-03-10) ----

    def start_agent_task(self, owner: str, repo: str, prompt: str, model: str | None = None,
                         base_ref: str | None = None, create_pull_request: bool = False) -> str:
        payload: dict = {"prompt": prompt, "create_pull_request": create_pull_request}
        if model:
            payload["model"] = model
        if base_ref:
            payload["base_ref"] = base_ref
        task = self._request("POST", f"/agents/repos/{owner}/{repo}/tasks",
                             api_version=AGENT_TASKS_API_VERSION, json=payload)
        return (f"Started agent task {task.get('id')} [{task.get('state', '?')}]: "
                f"{task.get('html_url', '')}")

    @staticmethod
    def _format_task(task: dict) -> str:
        return (f"{task.get('id')} [{task.get('state', '?')}] {task.get('name', '(unnamed)')} "
                f"(sessions: {task.get('session_count', 0)}, updated: {task.get('updated_at', '?')})")

    def list_agent_tasks(self, owner: str, repo: str, state: str | None = None) -> str:
        params: dict = {"per_page": 20}
        if state:
            params["state"] = state
        data = self._request("GET", f"/agents/repos/{owner}/{repo}/tasks",
                             api_version=AGENT_TASKS_API_VERSION, params=params)
        tasks = data.get("tasks") or []
        if not tasks:
            return "No agent tasks."
        lines = [self._format_task(t) for t in tasks]
        lines.append(f"(active: {data.get('total_active_count', '?')}, "
                     f"archived: {data.get('total_archived_count', '?')})")
        return "\n".join(lines)

    def get_agent_task(self, owner: str, repo: str, task_id: str) -> str:
        task = self._request("GET", f"/agents/repos/{owner}/{repo}/tasks/{task_id}",
                             api_version=AGENT_TASKS_API_VERSION)
        lines = [self._format_task(task), f"url: {task.get('html_url', '')}"]
        for session in task.get("sessions") or []:
            lines.append(f"  session {session.get('id')} [{session.get('state', '?')}] "
                         f"model: {session.get('model', '?')}")
            if session.get("error"):
                lines.append(f"    error: {session['error']}")
        return "\n".join(lines)

    def get_issue(self, owner: str, repo: str, number: int) -> str:
        issue = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        out = [f"#{issue['number']} [{issue['state']}] {issue['title']}",
               f"author: {issue.get('user', {}).get('login', '?')}", "",
               (issue.get("body") or "(no description)")[:4000]]
        comments = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}/comments",
                                 params={"per_page": 10})
        for c in comments:
            out.append("")
            out.append(f"--- comment by {c.get('user', {}).get('login', '?')} ---")
            out.append((c.get("body") or "")[:1500])
        return "\n".join(out)


def _client_for(ws: Workspace) -> tuple[GitHubClient, str, str]:
    from .config import Config
    config = Config.load()
    token = get_token(config)
    if not token:
        raise ToolError("not connected to GitHub: run 'silkcode connect github' "
                        "(sign in with GitHub) or set $GITHUB_TOKEN")
    api_url = (config.data.get("github") or {}).get("api_url", DEFAULT_API_URL)
    owner, repo = detect_repo(ws)
    return GitHubClient(token, api_url), owner, repo


def cli_client_for_repos() -> GitHubClient:
    """Build a GitHub client from config alone (no workspace needed), used by
    the project picker to list repositories."""
    from .config import Config
    config = Config.load()
    token = get_token(config)
    if not token:
        raise ToolError("not connected to GitHub: run 'silkcode connect github' "
                        "(sign in with GitHub) or set $GITHUB_TOKEN")
    api_url = (config.data.get("github") or {}).get("api_url", DEFAULT_API_URL)
    return GitHubClient(token, api_url)


def list_github_repos() -> list[dict]:
    """Repositories the user can open in a new session, or [] if not connected."""
    try:
        return cli_client_for_repos().list_repositories()
    except ToolError:
        return []


# ---- agent tools -----------------------------------------------------------

def github_create_pr(ws: Workspace, title: str, body: str = "", base: str = "main",
                     head: str | None = None, draft: bool = True) -> str:
    from .tools.git import get_attribution
    info = get_attribution()
    if info:
        body = (body.rstrip() + "\n\n---\n"
                f"_Co-authored with [Silk Code](https://github.com/RupertCloud/SilkCode) "
                f"({info['model']})_").lstrip("\n")
    client, owner, repo = _client_for(ws)
    if not head:
        # --show-current also works on a branch with no commits yet
        proc = subprocess.run(["git", "branch", "--show-current"],
                              cwd=ws.root, capture_output=True, text=True)
        head = proc.stdout.strip()
        if proc.returncode != 0 or not head:
            raise ToolError("cannot determine the current branch; pass 'head' explicitly")
    return client.create_pull_request(owner, repo, title, head, base, body, draft)


def github_list_prs(ws: Workspace, state: str = "open") -> str:
    client, owner, repo = _client_for(ws)
    return client.list_pull_requests(owner, repo, state)


def github_list_issues(ws: Workspace, state: str = "open") -> str:
    client, owner, repo = _client_for(ws)
    return client.list_issues(owner, repo, state)


def github_get_issue(ws: Workspace, number: int) -> str:
    client, owner, repo = _client_for(ws)
    return client.get_issue(owner, repo, int(number))


def github_merge_pr(ws: Workspace, number: int, method: str = "merge") -> str:
    client, owner, repo = _client_for(ws)
    return client.merge_pull_request(owner, repo, int(number), method)


def github_agent_task_start(ws: Workspace, prompt: str, model: str | None = None,
                            base_ref: str | None = None, create_pull_request: bool = False) -> str:
    client, owner, repo = _client_for(ws)
    return client.start_agent_task(owner, repo, prompt, model, base_ref, create_pull_request)


def github_agent_tasks(ws: Workspace, state: str | None = None) -> str:
    client, owner, repo = _client_for(ws)
    return client.list_agent_tasks(owner, repo, state)


def github_agent_task_get(ws: Workspace, task_id: str) -> str:
    client, owner, repo = _client_for(ws)
    return client.get_agent_task(owner, repo, str(task_id))
