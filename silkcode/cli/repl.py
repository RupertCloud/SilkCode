"""Interactive REPL (SRS sections 12-13, 45)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..version import build_id
from ..agent import Agent
from ..config import Config, ConfigError
from ..providers import ProviderError, build_provider
from ..context import assemble, build_context
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
  /mode [m]          show or set permission mode: plan | ask | edit | agent
  /new [name] [tpl]  create a new project from a template and switch to it
                     (e.g. /new todo-cli python-cli; no arguments prompts)
  /project [spec]    open another project (GitHub repo or local path) for this session
  /reload            re-read the config and rebuild the provider + MCP (e.g. a new
                     'timeout' for a slow provider) without losing this conversation
  /diff              show the current git diff
  /push              push the current branch to the remote
  /autopush [on|off] automatically push unpushed commits after each turn
  /usage             show token usage for this session
  /revert            revert the files changed in the last turn (checkpoint restore)
  /skills            list installed skills
  /memory            show the project memory
  /plan              show the current plan and its progress
  /agents            list swarm role definitions (built-in overrides and custom specialists)
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
             prompt: str | None = None, grants: list[str] | None = None,
             use_sandbox: bool = False, auto_push: bool = False,
             remote: str | None = None, trace_path: str | None = None,
             final_answer_path: str | None = None,
             check_command: str | None = None) -> int:
    config = Config.load()
    if remote:
        from ..remotews import RemoteWorkspace
        from ..execbackend import remote_backend_from_config
        try:
            backend = remote_backend_from_config(config.data)
            if backend is None:
                print("error: no sandbox configured; run 'silkcode sandbox connect <url>'", file=sys.stderr)
                return 1
            backend.health()
            workspace = RemoteWorkspace(backend, remote)
            print(f"{CYAN}remote workspace{RESET} {remote}  {DIM}(repo lives in sandbox {backend.url}){RESET}")
        except ToolError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            workspace = Workspace(path)
        except ToolError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if use_sandbox:
            from ..execbackend import remote_backend_from_config
            try:
                backend = remote_backend_from_config(config.data)
                if backend is None:
                    print("error: no sandbox configured; run 'silkcode sandbox connect <url>'", file=sys.stderr)
                    return 1
                backend.health()
                workspace.exec_backend = backend
                print(f"{DIM}commands run in sandbox: {backend.url}{RESET}")
            except ToolError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
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

    if auto_push:
        grants = list(grants or []) + ["push"]
    permissions = PermissionManager(mode=(resume or {}).get("mode", mode), asker=_ask_user,
                                    grants=grants)
    autopush_state = {"on": auto_push}
    from ..agent.loop import DEFAULT_CONTEXT_TOKENS
    from ..project import remember_workspace
    remember_workspace(workspace.root)   # the project you launched on counts
    project = assemble(workspace)
    # stderr, and before anything else: `silkcode -p` output gets piped into
    # scripts, and a security notice must not land in the middle of it - nor
    # be skipped just because this run is non-interactive.
    for warning in project.warnings:
        print(f"{YELLOW}{warning}{RESET}\n", file=sys.stderr)
    from ..lightmodel import checkpoint_summarizer
    agent = Agent(provider, model, workspace, permissions, on_event=_on_event,
                  context=project.text, mcp=mcp,
                  max_context_tokens=provider_cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS,
                  session_id=(resume or {}).get("id"),
                  attribution=config.data.get("attribution", True),
                  summarizer=checkpoint_summarizer(config))

    if resume:
        session = resume
        if session.get("messages"):
            agent.messages = session["messages"]
        agent.usage.prompt_tokens = session.get("usage", {}).get("prompt_tokens", 0)
        agent.usage.completion_tokens = session.get("usage", {}).get("completion_tokens", 0)
        print(f"Resumed session #{session['id']}: {session.get('title', '')}")
    else:
        session = new_session(store.new_id(), title="", model=spec, cwd=str(workspace.root), mode=permissions.mode)
        agent.session_id = session["id"]

    if prompt is not None:
        # One-shot mode: run a single turn and exit. With --trace,
        # --final-answer or --check this is adapter mode - a harness is
        # driving, so the exit code is a contract: 0 the run completed (and
        # the check passed), 1 the check failed, 2 the harness's fault
        # domain (provider down, bad config). Without them the plain
        # behavior is unchanged.
        adapter = bool(trace_path or final_answer_path or check_command)
        trace = None
        if trace_path:
            from ..trace import TraceWriter
            trace = TraceWriter(trace_path)
            inner = agent.on_event
            agent.on_event = lambda kind, data: (trace.event(kind, data), inner(kind, data))[-1]
        session["title"] = prompt[:60]
        try:
            answer = agent.run_turn(prompt)
            print()
        except ProviderError as exc:
            print(f"\n{RED}provider error: {exc}{RESET}", file=sys.stderr)
            if trace:
                trace.done(status="harness_error", detail=str(exc),
                           prompt_tokens=agent.usage.prompt_tokens,
                           completion_tokens=agent.usage.completion_tokens)
            return 2 if adapter else 1
        if final_answer_path:
            Path(final_answer_path).parent.mkdir(parents=True, exist_ok=True)
            Path(final_answer_path).write_text(answer or "")
        check_out = ""
        check_passed = True
        if check_command:
            from ..tools.shell import run_command
            check_out = run_command(agent.workspace, check_command, timeout=600)
            check_passed = check_out.startswith("exit code: 0")
            first = check_out.splitlines()[0] if check_out else ""
            print(f"{DIM}check: {check_command} -> {first}{RESET}", file=sys.stderr)
        if trace:
            trace.done(status="success" if check_passed else "task_failure",
                       detail="" if check_passed else check_out[:2000],
                       prompt_tokens=agent.usage.prompt_tokens,
                       completion_tokens=agent.usage.completion_tokens)
        if autopush_state["on"]:
            # --auto-push means "after each turn", and a one-shot run is a
            # turn. It is also the case that most needs it: nobody is at a
            # prompt to type /push afterwards.
            from ..tools.git import push_if_needed
            pushed = push_if_needed(agent.workspace)
            if pushed:
                print(f"{DIM}auto-push: {pushed.splitlines()[0]}{RESET}")
        session["messages"] = agent.messages
        session["usage"] = {
            "prompt_tokens": agent.usage.prompt_tokens,
            "completion_tokens": agent.usage.completion_tokens,
        }
        store.save(session)
        return 0 if check_passed else 1

    print(f"{BOLD}Silk Code{RESET} v{build_id()}  {DIM}|{RESET}  model: {CYAN}{provider_name}/{model}{RESET}  "
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
            exit_now, mcp = _handle_slash(line, agent, config, session, store,
                                          autopush_state, mcp)
            if exit_now:
                break
            continue
        if not session["title"]:
            session["title"] = line[:60]
        try:
            print()
            agent.run_turn(line)
            print()
            if autopush_state["on"]:
                from ..tools.git import push_if_needed
                pushed = push_if_needed(agent.workspace)
                if pushed:
                    print(f"{DIM}auto-push: {pushed.splitlines()[0]}{RESET}")
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


def _handle_slash(line: str, agent: Agent, config: Config, session: dict, store: SessionStore,
                  autopush_state: dict | None = None, mcp=None) -> tuple[bool, object]:
    """Handle a slash command. Returns (exit, mcp) — exit is True when the
    REPL should quit; mcp may be replaced (e.g. by /reload)."""
    parts = line.split(maxsplit=1)
    cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

    if cmd in ("/exit", "/quit", "/q"):
        return True, mcp
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
            print(f"{RED}unknown mode '{arg}'; expected plan, ask, edit, or agent{RESET}")
    elif cmd == "/diff":
        print(git_diff(agent.workspace))
    elif cmd == "/push":
        from ..tools.git import git_push
        print(git_push(agent.workspace))
    elif cmd == "/autopush":
        state = autopush_state if autopush_state is not None else {"on": False}
        if arg in ("on", "off"):
            state["on"] = arg == "on"
            if state["on"]:
                agent.permissions.grants.add("push")
            print(f"auto-push {arg}")
        else:
            print(f"auto-push is {'on' if state['on'] else 'off'}; use /autopush on|off")
    elif cmd == "/usage":
        u = agent.usage
        print(f"tokens: {u.prompt_tokens} prompt + {u.completion_tokens} completion = {u.total_tokens} total")
        print(f"context: ~{agent.context_tokens()} tokens of {agent.max_context_tokens} budget"
              + (f" ({agent.trimmed_messages} old messages trimmed)" if agent.trimmed_messages else ""))
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
    elif cmd == "/plan":
        from ..plan import progress, read_plan
        print(read_plan(agent.workspace))
        print(progress(agent.workspace))
    elif cmd == "/agents":
        from ..roles import load_roles, role_dirs, withheld
        definitions = load_roles(agent.workspace)
        for warning in withheld(agent.workspace):
            print(warning)
        if definitions:
            for d in definitions.values():
                kind = "custom specialist" if d.custom else "overrides built-in"
                pin = f", model {d.model}" if d.model else ""
                print(f"  {d.name}: {d.description} ({kind}{pin})")
        else:
            dirs = " or ".join(str(d) for d in role_dirs(agent.workspace))
            print(f"No agent definitions. Add markdown files to {dirs}")
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
    elif cmd == "/new":
        _new_command(agent, session, arg)
    elif cmd == "/project":
        _project_command(agent, config, session, arg)
    elif cmd == "/reload":
        mcp = _reload_command(agent, config, session, mcp)
    else:
        print(f"Unknown command {cmd}. Try /help")
    return False, mcp


def _reload_command(agent: Agent, config: Config, session: dict, mcp) -> object:
    """Hot-reload what can be reloaded in-process: re-read the config file and
    rebuild the provider and MCP servers. The conversation, workspace and
    permissions are kept. Picks up provider changes such as a new 'timeout'
    for a slow provider. New Python code (from `silkcode update`) still needs
    a restart — the session is saved after every turn, so /exit and re-run
    (or `silkcode resume <id>`) to run the new code."""
    from ..update import git_repo_root

    # 1) re-read the config from disk (providers, timeouts, mcp_servers, ...)
    fresh = Config.load()
    config.data = fresh.data
    config.providers = fresh.providers
    print(f"{DIM}config reloaded from {config.path}{RESET}")

    # 2) rebuild the provider with this session's model spec so config changes
    #    (e.g. a raised timeout) apply immediately
    spec = session.get("model") or f"{agent.provider.name}/{agent.model}"
    try:
        provider_name, provider_cfg, model = config.resolve_model(spec)
        api_key = config.api_key_for(provider_cfg)
        agent.provider = build_provider(provider_name, provider_cfg, api_key=api_key)
        agent.model = model
        from ..agent.loop import DEFAULT_CONTEXT_TOKENS
        agent.max_context_tokens = provider_cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS
        session["model"] = spec
        timeout = provider_cfg.get("timeout")
        print(f"provider: {provider_name}/{model}"
              + (f"  ({DIM}timeout {timeout}s{RESET})" if timeout else ""))
    except ConfigError as exc:
        print(f"{RED}{exc}{RESET}")

    # 3) reload MCP servers from the (possibly changed) config
    if mcp is not None:
        mcp.close()
    mcp_servers = config.data.get("mcp_servers") or {}
    new_mcp = None
    if mcp_servers:
        from ..mcp import McpManager
        new_mcp = McpManager(mcp_servers)
        for server_name, error in new_mcp.errors.items():
            print(f"{YELLOW}warning: MCP server '{server_name}' failed to start: {error}{RESET}")
    if new_mcp is not None:
        agent.mcp = new_mcp
        print(f"MCP: {len(new_mcp.servers)} server(s) connected")
    else:
        agent.mcp = None
        print("MCP: none configured")

    # 4) tell the user whether the installed code changed since this REPL
    #    started (they ran `silkcode update` elsewhere); code needs a restart
    repo = git_repo_root()
    if repo is not None:
        try:
            import subprocess
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True, timeout=15).stdout.strip()
            if head:
                print(f"{DIM}code at {head}; new Python code needs /exit + restart{RESET}")
        except Exception:
            pass
    return new_mcp


def _new_command(agent: Agent, session: dict, arg: str) -> None:
    """Create a new project and point this session at it.

    New projects land next to the current one (`/new sibling` in ~/code/app
    creates ~/code/sibling) rather than inside it, so scaffolding never drops
    an unrelated tree into the repository being worked on.
    """
    from pathlib import Path

    from ..context import build_context
    from ..project import record_recent_project
    from ..remotews import RemoteWorkspace
    from ..scaffold import (DEFAULT_TEMPLATE, create_project, format_result,
                            prompt_for_new_project)
    from ..workspace import ToolError

    # A remote workspace's root is an empty local scratch dir; its parent is a
    # temp directory nobody wants a new project in. Use the shell's cwd there.
    parent = (Path.cwd() if isinstance(agent.workspace, RemoteWorkspace)
              else agent.workspace.root.parent)
    parts = arg.split()
    if len(parts) > 2:
        print(f"{RED}usage: /new [name] [template]{RESET}  "
              f"(a name with spaces goes through the prompt: type /new alone)")
        return
    try:
        if parts:
            name, template = parts[0], (parts[1] if len(parts) > 1 else DEFAULT_TEMPLATE)
            result = create_project(name, template=template, parent=parent)
        else:
            result = prompt_for_new_project(parent=parent)
    except ToolError as exc:
        print(f"{RED}{exc}{RESET}")
        return

    print(format_result(result))
    record_recent_project("local", str(result.path), str(result.path))
    workspace = result.workspace
    agent.set_workspace(workspace, build_context(workspace))
    session["cwd"] = str(workspace.root)
    print(f"\n{BOLD}Opened project:{RESET} {CYAN}{workspace.root}{RESET}")
    print(f"{DIM}Files and git now refer to {workspace.root}{RESET}")


def _project_command(agent: Agent, config: Config, session: dict, arg: str) -> None:
    """Open a new project in this session: switch the agent to another
    workspace chosen from GitHub or a local path (SRS: new sessions ask for
    a project)."""
    from ..context import build_context
    from ..project import record_recent_project, resolve_project
    from ..workspace import ToolError

    try:
        if arg:
            choice = resolve_project(arg)
        else:
            from ..project import prompt_for_project
            choice = prompt_for_project()
        if choice is None:
            return  # cancelled / nothing chosen
    except ToolError as exc:
        print(f"{RED}{exc}{RESET}")
        return

    spec = f"github:{choice.github.owner}/{choice.github.repo}" if choice.github \
        else str(choice.workspace.root)
    record_recent_project(choice.kind, spec, choice.label)
    agent.set_workspace(choice.workspace, build_context(choice.workspace))
    session["cwd"] = str(choice.workspace.root)
    banner = (f"{CYAN}{choice.label}{RESET}{DIM}  {choice.workspace.root}{RESET}"
              if choice.kind == "github"
              else f"{CYAN}{choice.workspace.root}{RESET}")
    print(f"\n{BOLD}Opened project:{RESET} {banner}")
    print(f"{DIM}Files and git now refer to {choice.workspace.root}{RESET}")
