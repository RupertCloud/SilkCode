"""Tool registry exposed to the model (SRS sections 26-27)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import files, git, search, shell, symbols, testing
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
    name="git_log",
    description="Show recent commits (oneline format).",
    parameters=_params({
        "limit": {"type": "integer", "description": "Number of commits to show (default 10)"},
    }, []),
    func=git.git_log,
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
