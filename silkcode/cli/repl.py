"""Interactive REPL (SRS sections 12-13, 45)."""

from __future__ import annotations

import json
import sys

from .. import __version__
from ..agent import Agent
from ..config import Config, ConfigError
from ..providers import ProviderError, build_provider
from ..context import build_context
from ..sessions import SessionStore, new_session
from ..permissions import PermissionManager
from ..tools.git import git_diff
from ..workspace import ToolError, Workspace

try:
    import readline  # noqa: F401  (line editing / history for input())
except ImportError:
    pass

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

HELP = """Commands:
  /help              show this help
  /model [spec]      show or switch the model (e.g. /model ollama/qwen2.5-coder)
  /models            list configured providers
  /mode [m]          show or set permission mode: ask | edit | agent
  /diff              show the current git diff
  /usage             show token usage for this session
  /revert            revert the files changed in the last turn (checkpoint restore)
  /skills            list installed skills
  /memory            show the project memory
  /mcp               list connected MCP servers and their tools
  /clear             clear the conversation (keeps the session file)
  /sessions          list saved sessions
  /exit              quit

Anything else is sent to the model."""


def _ask_user(prompt: str) -> str:
    print(f"\n{YELLOW}? {prompt}{RESET}")
    while True:
        try:
            answer = input("  [y]es / [n]o / [a]lways this session: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "no"
        if answer in ("y", "yes"):
            return "yes"
        if answer in ("n", "no", ""):
            return "no"
        if answer in ("a", "always"):
            return "always"


def _summarize_args(args: dict) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 140 else text[:140] + "..."


def _on_event(kind: str, data) -> None:
    if kind == "text":
        sys.stdout.write(data)
        sys.stdout.flush()
    elif kind == "tool_start":
        print(f"\n{DIM}⚙ {data['name']} {_summarize_args(data['args'])}{RESET}")
    elif kind == "tool_result":
        first = str(data["output"]).splitlines()[0] if str(data["output"]) else ""
        print(f"{DIM}  → {first[:160]}{RESET}")


def run_repl(path: str, model_spec: str | None, mode: str, resume: dict | None = None,
             prompt: str | None = None, grants: list[str] | None = None) -> int:
    try:
        workspace = Workspace(path)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    config = Config.load()
    store = SessionStore()

    spec = model_spec or (resume or {}).get("model") or config.default_model
    try:
        provider_name, provider_cfg, model = config.resolve_model(spec)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    api_key = config.api_key_for(provider_cfg)
    if provider_cfg.get("api_key_env") and not api_key:
        print(f"{YELLOW}warning: no API key found for '{provider_name}'. "
              f"Set ${provider_cfg['api_key_env']} or run: silkcode models add ...{RESET}")
    provider = build_provider(provider_name, provider_cfg, api_key=api_key)

    mcp = None
    mcp_servers = config.data.get("mcp_servers") or {}
    if mcp_servers:
        from ..mcp import McpManager
        mcp = McpManager(mcp_servers)
        for server_name, error in mcp.errors.items():
            print(f"{YELLOW}warning: MCP server '{server_name}' failed to start: {error}{RESET}")

    permissions = PermissionManager(mode=(resume or {}).get("mode", mode), asker=_ask_user,
                                    grants=grants)
    agent = Agent(provider, model, workspace, permissions, on_event=_on_event,
                  context=build_context(workspace), mcp=mcp)

    if resume:
        session = resume
        if session.get("messages"):
            agent.messages = session["messages"]
        agent.usage.prompt_tokens = session.get("usage", {}).get("prompt_tokens", 0)
        agent.usage.completion_tokens = session.get("usage", {}).get("completion_tokens", 0)
        print(f"Resumed session #{session['id']}: {session.get('title', '')}")
    else:
        session = new_session(store.new_id(), title="", model=spec, cwd=str(workspace.root), mode=permissions.mode)

    if prompt is not None:
        # one-shot mode: run a single turn and exit
        session["title"] = prompt[:60]
        try:
            agent.run_turn(prompt)
            print()
        except ProviderError as exc:
            print(f"\n{RED}provider error: {exc}{RESET}", file=sys.stderr)
            return 1
        session["messages"] = agent.messages
        session["usage"] = {
            "prompt_tokens": agent.usage.prompt_tokens,
            "completion_tokens": agent.usage.completion_tokens,
        }
        store.save(session)
        return 0

    print(f"{BOLD}Silk Code{RESET} v{__version__}  {DIM}|{RESET}  model: {CYAN}{provider_name}/{model}{RESET}  "
          f"{DIM}|{RESET}  mode: {permissions.mode}  {DIM}|{RESET}  {workspace.root}")
    print(f"{DIM}Type a request, or /help for commands.{RESET}")

    while True:
        try:
            line = input(f"\n{BOLD}silk>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            if _handle_slash(line, agent, config, session, store):
                break
            continue
        if not session["title"]:
            session["title"] = line[:60]
        try:
            print()
            agent.run_turn(line)
            print()
        except ProviderError as exc:
            print(f"\n{RED}provider error: {exc}{RESET}")
        except KeyboardInterrupt:
            agent.repair_dangling_tool_calls()
            print(f"\n{YELLOW}[interrupted]{RESET}")
        session["messages"] = agent.messages
        session["model"] = spec if "/" in spec else f"{provider_name}/{model}"
        session["usage"] = {
            "prompt_tokens": agent.usage.prompt_tokens,
            "completion_tokens": agent.usage.completion_tokens,
        }
        store.save(session)
    if len(agent.messages) > 1:  # don't persist empty sessions
        store.save(session | {"messages": agent.messages})
    if mcp is not None:
        mcp.close()
    return 0


def _handle_slash(line: str, agent: Agent, config: Config, session: dict, store: SessionStore) -> bool:
    """Handle a slash command. Returns True when the REPL should exit."""
    parts = line.split(maxsplit=1)
    cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

    if cmd in ("/exit", "/quit", "/q"):
        return True
    if cmd == "/help":
        print(HELP)
    elif cmd == "/model":
        if not arg:
            print(f"model: {agent.provider.name}/{agent.model}")
        else:
            try:
                provider_name, provider_cfg, model = config.resolve_model(arg)
                api_key = config.api_key_for(provider_cfg)
                agent.provider = build_provider(provider_name, provider_cfg, api_key=api_key)
                agent.model = model
                session["model"] = arg
                print(f"Switched to {provider_name}/{model}")
            except ConfigError as exc:
                print(f"{RED}{exc}{RESET}")
    elif cmd == "/models":
        from .main import _models_list
        _models_list()
    elif cmd == "/mode":
        if not arg:
            print(f"mode: {agent.permissions.mode}")
        elif arg in PermissionManager.MODES:
            agent.permissions.mode = arg
            session["mode"] = arg
            print(f"mode set to {arg}")
        else:
            print(f"{RED}unknown mode '{arg}'; expected ask, edit, or agent{RESET}")
    elif cmd == "/diff":
        print(git_diff(agent.workspace))
    elif cmd == "/usage":
        u = agent.usage
        print(f"tokens: {u.prompt_tokens} prompt + {u.completion_tokens} completion = {u.total_tokens} total")
    elif cmd == "/revert":
        restored = agent.checkpoints.revert_last()
        if restored:
            print("Reverted:\n  " + "\n  ".join(restored))
        else:
            print("Nothing to revert.")
    elif cmd == "/clear":
        agent.messages = agent.messages[:1]
        print("Conversation cleared.")
    elif cmd == "/skills":
        from ..skills import load_skills, skill_dirs
        skills = load_skills(agent.workspace)
        if skills:
            for s in skills.values():
                print(f"{s.name:<24} {s.description}")
        else:
            dirs = " or ".join(str(d) for d in skill_dirs(agent.workspace))
            print(f"No skills installed. Add markdown files to {dirs}")
    elif cmd == "/memory":
        from ..memory import load_memory, memory_path
        content = load_memory(agent.workspace)
        print(content if content else f"No project memory yet ({memory_path(agent.workspace)}). "
              "The agent adds notes with the remember tool.")
    elif cmd == "/mcp":
        if agent.mcp is None:
            print("No MCP servers configured. Add one with: silkcode mcp add <name> --command '...'")
        else:
            for server_name, server in agent.mcp.servers.items():
                print(f"{server_name}: {', '.join(t['name'] for t in server.tools) or '(no tools)'}")
            for server_name, error in agent.mcp.errors.items():
                print(f"{RED}{server_name}: failed - {error}{RESET}")
    elif cmd == "/sessions":
        for s in store.list():
            print(f"#{s['id']:<5} {s['model']:<28} {s['title']}")
    else:
        print(f"Unknown command {cmd}. Try /help")
    return False
