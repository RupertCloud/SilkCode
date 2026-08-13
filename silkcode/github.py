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
REMOTE_PATTERN = re.compile(r"github\.com[:/](?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$")

# Test hook: replaced to inject a mock transport.
_make_client = lambda: httpx.Client(timeout=30.0)  # noqa: E731


def token_from_env(config_data: dict | None = None) -> str | None:
    env = ((config_data or {}).get("github") or {}).get("token_env", "GITHUB_TOKEN")
    return os.environ.get(env)


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
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = _make_client()

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            resp = self._client.request(method, f"{self.api_url}{path}", headers=self._headers, **kwargs)
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
    token = token_from_env(config.data)
    if not token:
        env = (config.data.get("github") or {}).get("token_env", "GITHUB_TOKEN")
        raise ToolError(f"no GitHub token: set ${env} (a personal access token) and retry, "
                        "or run 'silkcode connect github' to check your setup")
    api_url = (config.data.get("github") or {}).get("api_url", DEFAULT_API_URL)
    owner, repo = detect_repo(ws)
    return GitHubClient(token, api_url), owner, repo


# ---- agent tools -----------------------------------------------------------

def github_create_pr(ws: Workspace, title: str, body: str = "", base: str = "main",
                     head: str | None = None, draft: bool = True) -> str:
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
