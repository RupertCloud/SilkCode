import json
import subprocess

import httpx
import pytest

import silkcode.github as gh
from silkcode.github import GitHubClient, detect_repo, github_create_pr, github_list_issues
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def repo_ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-1"], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=root, check=True)
    return Workspace(root)


def mock_github(handler, monkeypatch):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(gh, "_make_client", lambda: httpx.Client(transport=transport))


def test_detect_repo_ssh_and_https(repo_ws):
    assert detect_repo(repo_ws) == ("acme", "widgets")
    subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/acme/widgets.git"],
                   cwd=repo_ws.root, check=True)
    assert detect_repo(repo_ws) == ("acme", "widgets")


def test_detect_repo_non_github(repo_ws):
    subprocess.run(["git", "remote", "set-url", "origin", "https://gitlab.com/a/b.git"],
                   cwd=repo_ws.root, check=True)
    with pytest.raises(ToolError, match="not a GitHub URL"):
        detect_repo(repo_ws)


def test_client_whoami_and_errors(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    client = GitHubClient("tok")
    assert client.whoami() == "octocat"
    with pytest.raises(ToolError, match="404"):
        client._request("GET", "/nope")


def test_create_pr_tool_uses_current_branch(repo_ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    captured = {}

    def handler(request):
        if request.url.path == "/repos/acme/widgets/pulls" and request.method == "POST":
            captured.update(json.loads(request.content))
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/acme/widgets/pull/7"})
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    out = github_create_pr(repo_ws, title="Add widgets", body="desc", base="main")
    assert "pull request #7" in out
    assert captured == {"title": "Add widgets", "head": "feature-1", "base": "main",
                        "body": "desc", "draft": True}


def test_create_pr_gets_attribution_footer(repo_ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    from silkcode.tools.git import clear_attribution, set_attribution
    captured = {}

    def handler(request):
        if request.url.path == "/repos/acme/widgets/pulls" and request.method == "POST":
            captured.update(json.loads(request.content))
            return httpx.Response(201, json={"number": 8, "html_url": "u"})
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    set_attribution(model="deepseek/deepseek-chat", session=3)
    try:
        github_create_pr(repo_ws, title="T", body="The change.", base="main")
    finally:
        clear_attribution()
    assert "Co-authored with [Silk Code]" in captured["body"]
    assert "deepseek/deepseek-chat" in captured["body"]
    assert captured["body"].startswith("The change.")


def test_tools_require_token(repo_ws, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ToolError, match="not connected to GitHub"):
        github_list_issues(repo_ws)


def test_merge_pr(repo_ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def handler(request):
        if request.url.path == "/repos/acme/widgets/pulls/7/merge" and request.method == "PUT":
            assert json.loads(request.content) == {"merge_method": "squash"}
            return httpx.Response(200, json={"merged": True, "sha": "abc1234567890"})
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    from silkcode.github import github_merge_pr
    out = github_merge_pr(repo_ws, 7, method="squash")
    assert "Merged pull request #7" in out


def test_agent_tasks_use_new_api_version(repo_ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen = {}

    def handler(request):
        seen[request.url.path] = request.headers.get("X-GitHub-Api-Version")
        if request.url.path == "/agents/repos/acme/widgets/tasks" and request.method == "POST":
            assert json.loads(request.content) == {
                "prompt": "Fix the login bug", "create_pull_request": True, "base_ref": "main",
            }
            return httpx.Response(201, json={"id": "t1", "state": "queued",
                                             "html_url": "https://github.com/acme/widgets/agents/t1"})
        if request.url.path == "/agents/repos/acme/widgets/tasks" and request.method == "GET":
            return httpx.Response(200, json={
                "tasks": [{"id": "t1", "state": "completed", "name": "Fix login",
                           "session_count": 1, "updated_at": "2026-08-13T00:00:00Z"}],
                "total_active_count": 0, "total_archived_count": 1,
            })
        if request.url.path == "/agents/repos/acme/widgets/tasks/t1":
            return httpx.Response(200, json={
                "id": "t1", "state": "completed", "name": "Fix login", "session_count": 1,
                "updated_at": "2026-08-13T00:00:00Z", "html_url": "u",
                "sessions": [{"id": "s1", "state": "completed", "model": "claude-sonnet-4.6"}],
            })
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    from silkcode.github import github_agent_task_get, github_agent_task_start, github_agent_tasks

    out = github_agent_task_start(repo_ws, "Fix the login bug", base_ref="main",
                                  create_pull_request=True)
    assert "Started agent task t1" in out
    out = github_agent_tasks(repo_ws)
    assert "t1 [completed] Fix login" in out
    out = github_agent_task_get(repo_ws, "t1")
    assert "session s1 [completed] model: claude-sonnet-4.6" in out
    assert all(v == "2026-03-10" for v in seen.values())


def test_list_issues_filters_prs(repo_ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def handler(request):
        if request.url.path == "/repos/acme/widgets/issues":
            return httpx.Response(200, json=[
                {"number": 1, "state": "open", "title": "Real issue"},
                {"number": 2, "state": "open", "title": "A PR", "pull_request": {}},
            ])
        return httpx.Response(404, json={"message": "Not Found"})

    mock_github(handler, monkeypatch)
    out = github_list_issues(repo_ws)
    assert "Real issue" in out
    assert "A PR" not in out
