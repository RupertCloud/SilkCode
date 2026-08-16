"""The GitHub client and the repository it decides to act on.

These calls are outward-facing: create_pull_request and merge_pull_request
change something on github.com. Acting on the wrong repository is worse than
failing, and it is silent, so the tests pin down which owner/repo the client
derives from a remote before they check anything else.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from silkcode.github import (
    REMOTE_PATTERN,
    GitHubClient,
    detect_repo,
    token_from_env,
)
from silkcode.workspace import ToolError, Workspace


class StubGitHub:
    """A local stand-in for the GitHub REST API."""

    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                api.requests.append({
                    "method": self.command,
                    "path": path,
                    "query": self.path.split("?")[1] if "?" in self.path else "",
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                })
                status, payload = api.routes.get((self.command, path), (404, {"message": "Not Found"}))
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def route(self, method, path, payload, status=200):
        self.routes[(method, path)] = (status, payload)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api():
    stub = StubGitHub()
    yield stub
    stub.close()


@pytest.fixture
def client(api):
    return GitHubClient("ghp_testtoken", api.url)


# --------------------------------------------------------------------------
# which repository are we acting on?
# --------------------------------------------------------------------------

def _repo_with_remote(tmp_path, url):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", url], check=True)
    return Workspace(root)


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/owner/repo.git", ("owner", "repo")),
    ("https://github.com/owner/repo", ("owner", "repo")),
    ("git@github.com:owner/repo.git", ("owner", "repo")),
    ("ssh://git@github.com/owner/repo.git", ("owner", "repo")),
    ("https://x-access-token:tok@github.com/owner/repo.git", ("owner", "repo")),
    ("https://github.com/owner/repo.with.dots", ("owner", "repo.with.dots")),
])
def test_detects_the_repository_from_a_github_remote(tmp_path, url, expected):
    assert detect_repo(_repo_with_remote(tmp_path, url)) == expected


@pytest.mark.parametrize("url", [
    "https://evilgithub.com/attacker/repo.git",
    "https://notgithub.com/someone/else",
    "https://git.mygithub.com/internal/thing.git",
])
def test_a_host_that_merely_ends_in_github_com_is_not_github(tmp_path, url):
    """Matching "github.com" anywhere treats these as real GitHub repos, and
    every caller then acts against api.github.com — opening or merging a pull
    request on a github.com repository that is not this remote at all."""
    with pytest.raises(ToolError, match="not a GitHub URL"):
        detect_repo(_repo_with_remote(tmp_path, url))


def test_a_non_github_remote_is_rejected(tmp_path):
    with pytest.raises(ToolError, match="not a GitHub URL"):
        detect_repo(_repo_with_remote(tmp_path, "https://gitlab.com/o/r.git"))


def test_a_workspace_with_no_origin_says_so(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    with pytest.raises(ToolError, match="no 'origin' git remote"):
        detect_repo(Workspace(root))


# --------------------------------------------------------------------------
# request plumbing
# --------------------------------------------------------------------------

def test_sends_the_token_and_api_version(client, api):
    api.route("GET", "/user", {"login": "octocat"})
    assert client.whoami() == "octocat"
    headers = api.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer ghp_testtoken"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_an_api_error_surfaces_githubs_message(client, api):
    api.route("GET", "/user", {"message": "Bad credentials"}, status=401)
    with pytest.raises(ToolError, match="Bad credentials"):
        client.whoami()


def test_an_api_error_without_json_still_reports(client, api):
    api.route("GET", "/user", b"<html>gateway timeout</html>", status=502)
    with pytest.raises(ToolError, match="502"):
        client.whoami()


def test_an_unreachable_api_is_reported_not_raised_raw():
    client = GitHubClient("tok", "http://127.0.0.1:1")
    with pytest.raises(ToolError, match="GitHub request failed"):
        client.whoami()


def test_an_error_never_echoes_the_token(client, api):
    api.route("GET", "/user", {"message": "Bad credentials"}, status=401)
    with pytest.raises(ToolError) as excinfo:
        client.whoami()
    assert "ghp_testtoken" not in str(excinfo.value)


# --------------------------------------------------------------------------
# repositories
# --------------------------------------------------------------------------

def test_lists_repositories_and_drops_duplicates(client, api):
    api.route("GET", "/user/repos", [
        {"full_name": "acme/one", "description": "first"},
        {"full_name": "acme/one", "description": "first again"},
        {"full_name": "acme/two", "description": None},
        {"description": "nameless"},
    ])
    repos = client.list_repositories()
    assert [r["full_name"] for r in repos] == ["acme/one", "acme/two"]
    assert repos[1]["description"] == ""


def test_lists_repositories_asks_for_private_ones_too(client, api):
    api.route("GET", "/user/repos", [])
    client.list_repositories()
    assert "visibility=all" in api.requests[0]["query"]


def test_an_unexpected_repositories_payload_is_not_fatal(client, api):
    api.route("GET", "/user/repos", {"message": "something odd"})
    assert client.list_repositories() == []


# --------------------------------------------------------------------------
# pull requests
# --------------------------------------------------------------------------

def test_creates_a_draft_pull_request_by_default(client, api):
    api.route("POST", "/repos/acme/widget/pulls",
              {"number": 7, "html_url": "https://github.com/acme/widget/pull/7"})
    out = client.create_pull_request("acme", "widget", "Title", "feature", "main",
                                     body="Body")
    assert "#7" in out and "pull/7" in out
    sent = api.requests[0]["body"]
    assert sent == {"title": "Title", "head": "feature", "base": "main",
                    "body": "Body", "draft": True}


def test_creates_a_ready_pull_request_when_asked(client, api):
    api.route("POST", "/repos/acme/widget/pulls", {"number": 8, "html_url": "u"})
    client.create_pull_request("acme", "widget", "T", "f", "main", draft=False)
    assert api.requests[0]["body"]["draft"] is False


def test_lists_pull_requests(client, api):
    api.route("GET", "/repos/acme/widget/pulls", [
        {"number": 1, "state": "open", "title": "First",
         "head": {"ref": "f1"}, "base": {"ref": "main"}},
    ])
    out = client.list_pull_requests("acme", "widget")
    assert "#1 [open] First (f1 -> main)" == out


def test_reports_when_there_are_no_pull_requests(client, api):
    api.route("GET", "/repos/acme/widget/pulls", [])
    assert client.list_pull_requests("acme", "widget") == "No open pull requests."


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------

def test_lists_issues_excluding_pull_requests(client, api):
    """GitHub returns PRs from the issues endpoint; they are not issues."""
    api.route("GET", "/repos/acme/widget/issues", [
        {"number": 3, "state": "open", "title": "A real issue"},
        {"number": 4, "state": "open", "title": "Actually a PR",
         "pull_request": {"url": "..."}},
    ])
    out = client.list_issues("acme", "widget")
    assert "A real issue" in out
    assert "Actually a PR" not in out


def test_reports_when_there_are_no_issues(client, api):
    api.route("GET", "/repos/acme/widget/issues", [])
    assert client.list_issues("acme", "widget") == "No open issues."


# --------------------------------------------------------------------------
# merging  (outward-facing and irreversible)
# --------------------------------------------------------------------------

def test_merges_with_the_requested_method(client, api):
    api.route("PUT", "/repos/acme/widget/pulls/9/merge",
              {"merged": True, "sha": "abcdef1234567890"})
    out = client.merge_pull_request("acme", "widget", 9, method="squash")
    assert "#9" in out and "squash" in out
    assert api.requests[0]["body"] == {"merge_method": "squash"}


def test_rejects_an_unknown_merge_method(client, api):
    with pytest.raises(ToolError, match="merge method"):
        client.merge_pull_request("acme", "widget", 9, method="fast-forward")
    assert api.requests == [], "must not call the API with a bad method"


def test_a_refused_merge_is_an_error_not_a_success(client, api):
    api.route("PUT", "/repos/acme/widget/pulls/9/merge",
              {"merged": False, "message": "Pull Request is not mergeable"})
    with pytest.raises(ToolError, match="not mergeable"):
        client.merge_pull_request("acme", "widget", 9)


# --------------------------------------------------------------------------
# token resolution
# --------------------------------------------------------------------------

def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert token_from_env({}) == "from-env"


def test_token_can_come_from_a_named_variable(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MY_TOKEN", "named")
    assert token_from_env({"github": {"token_env": "MY_TOKEN"}}) == "named"


def test_token_falls_back_to_the_config(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert token_from_env({"github": {"token": "stored"}}) == "stored"


def test_the_environment_wins_over_the_config(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert token_from_env({"github": {"token": "stored"}}) == "from-env"


def test_no_token_anywhere(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert token_from_env({}) is None
