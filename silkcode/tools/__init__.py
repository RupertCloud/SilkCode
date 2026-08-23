"""Tool registry exposed to the model (SRS sections 26-27)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import files, git, images, search, shell, symbols, testing
from ..liveserver import live_server
from ..github import (
    github_agent_task_get,
    github_agent_task_start,
    github_agent_tasks,
    github_create_pr,
    github_get_issue,
    github_list_issues,
    github_list_prs,
    github_merge_pr,
)
from ..memory import MEMORY_RELPATH, remember
from ..skills import use_skill


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable
    kind: str  # "read" | "write" | "command"
    # For kind == "command": resolves the effective shell command used for
    # risk classification and permission checks. Defaults to args["command"].
    command_of: Callable | None = None
    # For kind == "write": resolves the path being modified for permission
    # checks and checkpoints. Defaults to args["path"].
    path_of: Callable | None = None
    # True when the tool keeps per-owner file fingerprints (the file tools):
    # the agent loop passes the Agent's registry so concurrent sessions on the
    # same workspace detect each other's changes instead of clobbering them.
    owner_aware: bool = False


def _params(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> None:
    TOOLS[tool.name] = tool


_register(Tool(
    name="read_file",
    description="Read a file from the workspace. Returns numbered lines.",
    parameters=_params({
        "path": {"type": "string", "description": "Path relative to the workspace root"},
        "offset": {"type": "integer", "description": "1-based first line to read (default 1)"},
        "limit": {"type": "integer", "description": "Maximum number of lines to read (default 1000)"},
    }, ["path"]),
    func=files.read_file,
    kind="read",
    owner_aware=True,
))

_register(Tool(
    name="write_file",
    description="Create or overwrite a file with the given content. Parent directories are created automatically.",
    parameters=_params({
        "path": {"type": "string", "description": "Path relative to the workspace root"},
        "content": {"type": "string", "description": "Full file content"},
    }, ["path", "content"]),
    func=files.write_file,
    kind="write",
    owner_aware=True,
))

_register(Tool(
    name="edit_file",
    description="Replace an exact string in a file. old_string must match exactly and be unique unless replace_all is true.",
    parameters=_params({
        "path": {"type": "string", "description": "Path relative to the workspace root"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
    }, ["path", "old_string", "new_string"]),
    func=files.edit_file,
    kind="write",
    owner_aware=True,
))

_register(Tool(
    name="glob",
    description="List workspace files matching a glob pattern, e.g. 'src/**/*.py'.",
    parameters=_params({
        "pattern": {"type": "string", "description": "Glob pattern relative to the workspace root"},
    }, ["pattern"]),
    func=search.glob_files,
    kind="read",
))

_register(Tool(
    name="grep",
    description="Search file contents with a regular expression. Returns 'path:line:text' matches.",
    parameters=_params({
        "pattern": {"type": "string", "description": "Regular expression to search for"},
        "path": {"type": "string", "description": "File or directory to search (default '.')"},
        "glob": {"type": "string", "description": "Glob filter for files when searching a directory (default '**/*')"},
    }, ["pattern"]),
    func=search.grep,
    kind="read",
))

_register(Tool(
    name="run_command",
    description="Run a shell command in the workspace root and return its exit code and output. Use for builds, tests, and inspection.",
    parameters=_params({
        "command": {"type": "string", "description": "Shell command to execute"},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)"},
    }, ["command"]),
    func=shell.run_command,
    kind="command",
))

_register(Tool(
    name="capture_screenshot",
    description=("Capture the machine's visible desktop and show the PNG inline. Launch or focus "
                 "the app first, then use delay to give it time to appear. Local workspaces only."),
    parameters=_params({
        "path": {"type": "string", "description": "Optional .png path inside the workspace"},
        "delay": {"type": "integer", "description": "Seconds to wait before capture (default 1, max 10)"},
    }, []),
    func=images.capture_screenshot,
    kind="command",
    command_of=lambda args, ws: "screencapture",
))

_register(Tool(
    name="show_image",
    description="Show an existing PNG, JPEG, GIF or WebP file from the workspace inline in the GUI.",
    parameters=_params({
        "path": {"type": "string", "description": "Image path relative to the workspace root"},
    }, ["path"]),
    func=images.show_image,
    kind="read",
))

_register(Tool(
    name="review_url",
    description=("Open an HTTP(S) link in a headless Chromium browser, inspect its rendered title "
                 "and visible text, and show a full-page screenshot inline. Use mobile=true to "
                 "review the phone layout."),
    parameters=_params({
        "url": {"type": "string", "description": "HTTP(S) link to review"},
        "path": {"type": "string", "description": "Optional screenshot .png path in the workspace"},
        "mobile": {"type": "boolean", "description": "Use a 390x844 phone viewport"},
        "wait_ms": {"type": "integer", "description": "Extra rendering wait, up to 10000 ms"},
    }, ["url"]),
    func=images.review_url,
    kind="command",
    command_of=lambda args, ws: "headless-browser " + str(args.get("url", "")),
))

_register(Tool(
    name="live_server",
    description=("Start (or stop/check) a live preview server for the workspace. It serves the "
                 "project over HTTP and reloads the open page automatically whenever a file "
                 "changes — handy while building web pages. No external dependency needed."),
    parameters=_params({
        "action": {"type": "string", "description": "start, stop or status (default start)"},
        "port": {"type": "integer", "description": "Port to listen on (0 picks a free port)"},
    }, []),
    func=live_server,
    kind="command",
))

_register(Tool(
    name="run_tests",
    description="Run the project's test suite. Detects the test framework (pytest, npm test, cargo test, go test, flutter test) when no command is given.",
    parameters=_params({
        "command": {"type": "string", "description": "Explicit test command (optional; auto-detected when omitted)"},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 300)"},
    }, []),
    func=testing.run_tests,
    kind="command",
    command_of=lambda args, ws: args.get("command") or testing.detect_test_command(ws) or "",
))

_register(Tool(
    name="git_commit",
    description="Stage all changes and create a git commit with the given message.",
    parameters=_params({
        "message": {"type": "string", "description": "Commit message"},
        "add_all": {"type": "boolean", "description": "Stage all changes first (default true)"},
    }, ["message"]),
    func=git.git_commit,
    kind="command",
    command_of=lambda args, ws: "git commit",
))

_register(Tool(
    name="symbols",
    description="List classes and functions (with file:line) in a file or directory. Useful for getting oriented in unfamiliar code.",
    parameters=_params({
        "path": {"type": "string", "description": "File or directory to index (default '.')"},
    }, []),
    func=symbols.symbols,
    kind="read",
))

_register(Tool(
    name="use_skill",
    description="Load the full instructions of an installed skill by name. The available skills are listed in your context.",
    parameters=_params({
        "name": {"type": "string", "description": "Skill name"},
    }, ["name"]),
    func=use_skill,
    kind="read",
))

_register(Tool(
    name="remember",
    description="Append a note to the project memory (.silkcode/memory.md): architecture decisions, conventions, important commands, known limitations.",
    parameters=_params({
        "text": {"type": "string", "description": "The note to remember"},
    }, ["text"]),
    func=remember,
    kind="write",
    path_of=lambda args, ws: MEMORY_RELPATH,
))

_register(Tool(
    name="git_status",
    description="Show git status (short format) for the workspace.",
    parameters=_params({}, []),
    func=git.git_status,
    kind="read",
))

_register(Tool(
    name="git_diff",
    description="Show the git diff of unstaged (or staged) changes.",
    parameters=_params({
        "staged": {"type": "boolean", "description": "Show staged changes instead of unstaged (default false)"},
    }, []),
    func=git.git_diff,
    kind="read",
))

_register(Tool(
    name="git_push",
    description="Push the current (or given) branch to a remote. Uses the configured GitHub token for HTTPS remotes, or the developer's own git credentials.",
    parameters=_params({
        "remote": {"type": "string", "description": "Remote name (default 'origin')"},
        "branch": {"type": "string", "description": "Branch to push (default: current branch)"},
        "set_upstream": {"type": "boolean", "description": "Set upstream tracking (default true)"},
    }, []),
    func=git.git_push,
    kind="command",
    command_of=lambda args, ws: "git push",
))

_register(Tool(
    name="git_pull",
    description="Pull from a remote into the current branch.",
    parameters=_params({
        "remote": {"type": "string", "description": "Remote name (default 'origin')"},
        "branch": {"type": "string", "description": "Branch to pull (default: tracked branch)"},
    }, []),
    func=git.git_pull,
    kind="command",
    command_of=lambda args, ws: "git pull",
))

def _git_sync(ws, apply: bool = False, restart: bool = False) -> str:
    from ..sync import git_sync
    return git_sync(ws, apply=apply, restart=restart)


_register(Tool(
    name="git_sync",
    description=(
        "Check whether this branch has fallen behind because someone else "
        "pushed, the base branch moved, or a pull request was merged — and "
        "optionally reconcile it. Read-only unless 'apply' is true. Use this "
        "before committing or pushing after a long session, and whenever a "
        "push is rejected as non-fast-forward. It never discards work: "
        "uncommitted changes stop it, and it reports conflicts rather than "
        "guessing."
    ),
    parameters=_params({
        "apply": {"type": "boolean",
                  "description": "Perform the suggested action (fast-forward or "
                                 "merge) instead of only reporting it."},
        "restart": {"type": "boolean",
                    "description": "Allow moving the branch onto the base when its "
                                   "work is already merged there as a squash. Only "
                                   "meaningful with apply."},
    }, []),
    func=_git_sync,
    # A plain check touches nothing; applying one runs a pull, which is the
    # grantable 'pull' operation, so it is classified as such.
    kind="command",
    command_of=lambda args, ws: ("git pull" if args.get("apply") else "git status"),
))

_register(Tool(
    name="git_log",
    description="Show recent commits (oneline format).",
    parameters=_params({
        "limit": {"type": "integer", "description": "Number of commits to show (default 10)"},
    }, []),
    func=git.git_log,
    kind="read",
))


_register(Tool(
    name="github_create_pr",
    description="Create a GitHub pull request (draft by default) from the current or given branch. Requires $GITHUB_TOKEN and an 'origin' remote on github.com.",
    parameters=_params({
        "title": {"type": "string", "description": "Pull request title"},
        "body": {"type": "string", "description": "Pull request description (markdown)"},
        "base": {"type": "string", "description": "Base branch (default 'main')"},
        "head": {"type": "string", "description": "Head branch (default: current branch)"},
        "draft": {"type": "boolean", "description": "Create as draft (default true)"},
    }, ["title"]),
    func=github_create_pr,
    kind="command",
    command_of=lambda args, ws: "github create-pr",  # outward-facing: approval-gated
))

_register(Tool(
    name="github_list_prs",
    description="List pull requests in the repository's GitHub project.",
    parameters=_params({
        "state": {"type": "string", "description": "open, closed, or all (default open)"},
    }, []),
    func=github_list_prs,
    kind="read",
))

_register(Tool(
    name="github_list_issues",
    description="List issues in the repository's GitHub project.",
    parameters=_params({
        "state": {"type": "string", "description": "open, closed, or all (default open)"},
    }, []),
    func=github_list_issues,
    kind="read",
))

_register(Tool(
    name="github_get_issue",
    description="Read a GitHub issue with its recent comments.",
    parameters=_params({
        "number": {"type": "integer", "description": "Issue number"},
    }, ["number"]),
    func=github_get_issue,
    kind="read",
))


_register(Tool(
    name="github_merge_pr",
    description="Merge a GitHub pull request (merge, squash, or rebase).",
    parameters=_params({
        "number": {"type": "integer", "description": "Pull request number"},
        "method": {"type": "string", "description": "merge, squash, or rebase (default merge)"},
    }, ["number"]),
    func=github_merge_pr,
    kind="command",
    command_of=lambda args, ws: "github merge-pr",  # high-risk: gated unless 'merge' is granted
))

_register(Tool(
    name="github_agent_task_start",
    description="Start a GitHub Copilot cloud agent task on this repository (requires Copilot subscription). The agent works remotely; check progress with github_agent_task_get.",
    parameters=_params({
        "prompt": {"type": "string", "description": "Task prompt for the cloud agent"},
        "model": {"type": "string", "description": "Model to use (optional)"},
        "base_ref": {"type": "string", "description": "Base branch (optional)"},
        "create_pull_request": {"type": "boolean", "description": "Open a PR with the result (default false)"},
    }, ["prompt"]),
    func=github_agent_task_start,
    kind="command",
    command_of=lambda args, ws: "github agent-task start",
))

_register(Tool(
    name="github_agent_tasks",
    description="List GitHub Copilot cloud agent tasks for this repository.",
    parameters=_params({
        "state": {"type": "string", "description": "Filter by state (optional)"},
    }, []),
    func=github_agent_tasks,
    kind="read",
))

_register(Tool(
    name="github_agent_task_get",
    description="Get a GitHub Copilot cloud agent task and its sessions by id.",
    parameters=_params({
        "task_id": {"type": "string", "description": "Task id"},
    }, ["task_id"]),
    func=github_agent_task_get,
    kind="read",
))


def openai_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS.values()
    ]
