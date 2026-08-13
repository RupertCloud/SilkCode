"""silkcode CLI entry point (SRS section 46).

Commands:
    silkcode [path] [--model M] [--mode ask|edit|agent]   interactive REPL
    silkcode gui [path] [--port N] [--model M]            local web GUI
    silkcode models                                        list providers and models
    silkcode models add <name> --base-url URL [...]        onboard a provider/endpoint
    silkcode models pull <model>                           pull a model into Ollama
    silkcode models default <spec>                         set the default model
    silkcode sessions                                      list saved sessions
    silkcode resume <id>                                   resume a session in the REPL
    silkcode config                                        show configuration
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

from ..config import Config, ConfigError
from ..providers import build_provider
from ..sessions import SessionStore


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "models": cmd_models,
        "config": cmd_config,
        "sessions": cmd_sessions,
        "resume": cmd_resume,
        "gui": cmd_gui,
        "test": cmd_test,
        "review": cmd_review,
        "mcp": cmd_mcp,
        "connect": cmd_connect,
        "benchmark": cmd_benchmark,
        "sandbox": cmd_sandbox,
    }
    if argv and argv[0] in commands:
        try:
            return commands[argv[0]](argv[1:])
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return cmd_repl(argv)


def _repl_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current)")
    parser.add_argument("--model", "-m", help="model spec, e.g. 'deepseek' or 'ollama/qwen2.5-coder'")
    parser.add_argument("--mode", choices=("ask", "edit", "agent"), default="ask", help="permission mode")
    parser.add_argument("--allow", help="pre-authorize git operations without prompts, "
                        "comma-separated from: pull,commit,push,merge")
    parser.add_argument("--sandbox", action="store_true",
                        help="run commands in the configured remote sandbox "
                             "(silkcode sandbox connect <url>)")
    return parser


def _parse_grants(allow: str | None) -> list[str]:
    if not allow:
        return []
    from ..permissions import GRANTABLE
    grants = [g.strip() for g in allow.split(",") if g.strip()]
    unknown = [g for g in grants if g not in GRANTABLE]
    if unknown:
        raise SystemExit(f"error: unknown --allow operations: {', '.join(unknown)} "
                         f"(allowed: {', '.join(GRANTABLE)})")
    return grants


def cmd_repl(argv: list[str]) -> int:
    parser = _repl_parser("silkcode")
    parser.add_argument("--prompt", "-p", help="run a single request non-interactively and exit")
    args = parser.parse_args(argv)
    from .repl import run_repl
    return run_repl(args.path, args.model, args.mode, prompt=args.prompt,
                    grants=_parse_grants(args.allow), use_sandbox=args.sandbox)


REVIEW_PROMPT = (
    "Review the current uncommitted changes in this repository. "
    "Use git_status and git_diff to see them, and read any files you need for context. "
    "Report correctness bugs, risky patterns, and clear improvements, referencing files "
    "and lines. Do not modify anything. If there are no changes, say so."
)


def cmd_review(argv: list[str]) -> int:
    args = _repl_parser("silkcode review").parse_args(argv)
    from .repl import run_repl
    return run_repl(args.path, args.model, args.mode, prompt=REVIEW_PROMPT)


def cmd_gui(argv: list[str]) -> int:
    parser = _repl_parser("silkcode gui")
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    from ..gui.server import run_gui
    return run_gui(args.path, args.model, args.mode, host=args.host, port=args.port,
                   grants=_parse_grants(args.allow), use_sandbox=args.sandbox)


def cmd_models(argv: list[str]) -> int:
    if argv and argv[0] == "add":
        return _models_add(argv[1:])
    if argv and argv[0] == "pull":
        return _models_pull(argv[1:])
    if argv and argv[0] == "default":
        return _models_default(argv[1:])
    return _models_list()


def _models_list() -> int:
    config = Config.load()
    print(f"default model: {config.default_model}\n")
    for name in sorted(config.providers):
        cfg = config.providers[name]
        key = config.api_key_for(cfg)
        needs_key = bool(cfg.get("api_key_env") or cfg.get("api_key"))
        key_status = "key: set" if key else ("key: MISSING ($" + cfg.get("api_key_env", "?") + ")" if needs_key else "key: not required")
        default = cfg.get("default_model") or "-"
        print(f"{name:<12} {cfg.get('base_url', ''):<55} default: {default:<24} {key_status}")
        if cfg.get("type") == "ollama":
            provider = build_provider(name, cfg, api_key=key)
            local = provider.list_models()
            if local:
                for m in local:
                    print(f"{'':<12}   local model: {name}/{m}")
            else:
                print(f"{'':<12}   (no local models found - is the server running? try: silkcode models pull qwen2.5-coder)")
    print("\nUse a model with: silkcode --model <provider> or silkcode --model <provider>/<model>")
    return 0


def _models_add(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode models add")
    parser.add_argument("name", help="provider name, e.g. 'myserver' or a builtin like 'cloudflare'")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL (not needed when "
                        "configuring a builtin provider, e.g. cloudflare)")
    parser.add_argument("--type", choices=("openai_compat", "ollama"), default="openai_compat")
    parser.add_argument("--model", help="default model for this provider")
    parser.add_argument("--account-id", help="account id for providers whose URL needs one (Cloudflare)")
    parser.add_argument("--api-key-env", help="environment variable that holds the API key (recommended)")
    parser.add_argument("--api-key", help="API key stored in the config file (prefer --api-key-env)")
    args = parser.parse_args(argv)

    config = Config.load()
    is_builtin = args.name in config.providers
    if not args.base_url and not is_builtin:
        parser.error("--base-url is required for new providers")
    cfg: dict = {}
    if args.base_url:
        cfg["base_url"] = args.base_url
        cfg["type"] = args.type
    if args.account_id:
        cfg["account_id"] = args.account_id
    if args.model:
        cfg["default_model"] = args.model
    if args.api_key_env:
        cfg["api_key_env"] = args.api_key_env
    if args.api_key:
        cfg["api_key"] = args.api_key
        print("warning: storing the API key in the config file; prefer --api-key-env", file=sys.stderr)
    config.set_provider(args.name, cfg)
    config.save()
    merged = config.providers[args.name]
    print(f"{'Configured' if is_builtin else 'Added'} provider '{args.name}' "
          f"({merged.get('base_url', '?')}) in {config.path}")
    has_default = args.model or merged.get("default_model")
    print(f"Use it with: silkcode --model {args.name}" + ("" if has_default else "/<model-name>"))
    if merged.get("api_key_env"):
        print(f"API key is read from ${merged['api_key_env']}")
    return 0


def _models_pull(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode models pull")
    parser.add_argument("model", help="model to pull into Ollama, e.g. qwen2.5-coder")
    parser.add_argument("--provider", default="ollama", help="which configured Ollama provider to use")
    args = parser.parse_args(argv)

    import httpx

    config = Config.load()
    cfg = config.providers.get(args.provider)
    if not cfg or cfg.get("type") != "ollama":
        print(f"error: '{args.provider}' is not a configured Ollama provider", file=sys.stderr)
        return 1
    root = cfg["base_url"].rstrip("/")
    print(f"Pulling {args.model} via {root} ...")
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", f"{root}/api/pull", json={"model": args.model}) as resp:
                if resp.status_code >= 400:
                    print(f"error: HTTP {resp.status_code}: {resp.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
                    return 1
                last = ""
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    status = event.get("status", "")
                    if event.get("error"):
                        print(f"\nerror: {event['error']}", file=sys.stderr)
                        return 1
                    if status != last:
                        print(status)
                        last = status
    except httpx.HTTPError as exc:
        print(f"error: cannot reach Ollama at {root}: {exc}", file=sys.stderr)
        print("Is Ollama running? Install it from https://ollama.com and start it, then retry.", file=sys.stderr)
        return 1
    print(f"Done. Use it with: silkcode --model {args.provider}/{args.model}")
    return 0


def _models_default(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode models default")
    parser.add_argument("spec", help="model spec, e.g. 'deepseek' or 'ollama/qwen2.5-coder'")
    args = parser.parse_args(argv)
    config = Config.load()
    config.resolve_model(args.spec)  # validate
    config.data["default_model"] = args.spec
    config.save()
    print(f"Default model set to {args.spec}")
    return 0


def cmd_test(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode test")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--command", help="explicit test command (auto-detected when omitted)")
    args = parser.parse_args(argv)
    from ..tools.testing import detect_test_command, run_tests
    from ..workspace import ToolError, Workspace
    try:
        ws = Workspace(args.path)
        command = args.command or detect_test_command(ws)
        if not command:
            print("error: no test framework detected; pass --command", file=sys.stderr)
            return 1
        output = run_tests(ws, command)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0 if "exit code: 0" in output else 1


def cmd_benchmark(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode benchmark")
    parser.add_argument("--model", "-m", action="append", dest="models",
                        help="model spec to benchmark; repeat to compare (default: config default)")
    parser.add_argument("--tasks", help="comma-separated task ids (default: all)")
    parser.add_argument("--ab", action="store_true",
                        help="paired-condition run: each task twice, bare agent vs full harness "
                             "context, to measure the harness's contribution")
    parser.add_argument("--list", action="store_true", help="list available tasks and exit")
    args = parser.parse_args(argv)

    from ..benchmark import TASKS, format_results, run_benchmark
    if args.list:
        for task in TASKS:
            print(f"{task.id:<16} {task.prompt[:80]}")
        return 0
    specs = args.models or [Config.load().default_model]
    task_ids = [t.strip() for t in args.tasks.split(",")] if args.tasks else None
    try:
        results = run_benchmark(specs, task_ids, ab=args.ab, on_progress=print)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print()
    print(format_results(results))
    return 0


def cmd_connect(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode connect")
    parser.add_argument("service", choices=["github"])
    parser.add_argument("path", nargs="?", default=".", help="workspace to check the git remote in")
    parser.add_argument("--token-env", help="environment variable holding the token (default GITHUB_TOKEN)")
    args = parser.parse_args(argv)

    from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, token_from_env
    from ..workspace import ToolError, Workspace

    config = Config.load()
    if args.token_env:
        config.data.setdefault("github", {})["token_env"] = args.token_env
        config.save()
        print(f"GitHub token will be read from ${args.token_env}")

    token = token_from_env(config.data)
    env = (config.data.get("github") or {}).get("token_env", "GITHUB_TOKEN")
    if not token:
        print(f"Not connected: ${env} is not set.")
        print("Create a GitHub personal access token (repo scope) at https://github.com/settings/tokens")
        print(f"then: export {env}=ghp_...   and run 'silkcode connect github' again.")
        print("\nAlternative: use GitHub's MCP server instead:")
        print("  silkcode mcp add github --command 'npx -y @modelcontextprotocol/server-github' \\")
        print(f"      --env GITHUB_PERSONAL_ACCESS_TOKEN=$" + env)
        return 1

    api_url = (config.data.get("github") or {}).get("api_url", DEFAULT_API_URL)
    try:
        client = GitHubClient(token, api_url)
        login = client.whoami()
    except ToolError as exc:
        print(f"Token check failed: {exc}")
        return 1
    print(f"Connected to GitHub as {login}")
    try:
        owner, repo = detect_repo(Workspace(args.path))
        print(f"Workspace repository: {owner}/{repo}")
    except ToolError as exc:
        print(f"note: {exc}")
    print("Agent tools available: github_create_pr, github_list_prs, github_list_issues, github_get_issue")
    return 0


def cmd_mcp(argv: list[str]) -> int:
    if argv and argv[0] == "add":
        parser = argparse.ArgumentParser(prog="silkcode mcp add")
        parser.add_argument("name")
        parser.add_argument("--command", required=True, help="command line to launch the server, e.g. 'uvx mcp-server-fetch'")
        parser.add_argument("--env", action="append", default=[], help="KEY=VALUE environment for the server (repeatable)")
        args = parser.parse_args(argv[1:])
        env = {}
        for pair in args.env:
            key, _, value = pair.partition("=")
            env[key] = value
        config = Config.load()
        servers = config.data.setdefault("mcp_servers", {})
        servers[args.name] = {"command": args.command, **({"env": env} if env else {})}
        config.save()
        print(f"Added MCP server '{args.name}'. It will be available in new sessions.")
        return 0
    if argv and argv[0] == "remove":
        config = Config.load()
        servers = config.data.get("mcp_servers") or {}
        if len(argv) < 2 or argv[1] not in servers:
            print(f"error: unknown MCP server; configured: {', '.join(servers) or '(none)'}", file=sys.stderr)
            return 1
        del servers[argv[1]]
        config.save()
        print(f"Removed MCP server '{argv[1]}'.")
        return 0
    # list (default): start each configured server and show its tools
    config = Config.load()
    servers = config.data.get("mcp_servers") or {}
    if not servers:
        print("No MCP servers configured.")
        print("Add one with: silkcode mcp add fetch --command 'uvx mcp-server-fetch'")
        return 0
    from ..mcp import McpManager
    manager = McpManager(servers)
    for name, server in manager.servers.items():
        print(f"{name}: {', '.join(t['name'] for t in server.tools) or '(no tools)'}")
    for name, error in manager.errors.items():
        print(f"{name}: FAILED - {error}")
    manager.close()
    return 0


def cmd_config(argv: list[str]) -> int:
    config = Config.load()
    print(f"config file: {config.path}" + ("" if config.path.exists() else " (not created yet)"))
    redacted = json.loads(json.dumps(config.data))
    for cfg in (redacted.get("providers") or {}).values():
        if "api_key" in cfg:
            cfg["api_key"] = "********"
    print(json.dumps(redacted, indent=2) if redacted else "{}")
    print("\nBuilt-in providers: " + ", ".join(sorted(Config().providers)))
    print("API keys are read from environment variables (e.g. DEEPSEEK_API_KEY) unless set in the config.")
    return 0


def cmd_sandbox(argv: list[str]) -> int:
    from ..execbackend import remote_backend_from_config
    from ..workspace import ToolError

    if argv and argv[0] == "connect":
        parser = argparse.ArgumentParser(prog="silkcode sandbox connect")
        parser.add_argument("url", help="sandbox base URL, e.g. https://sandbox.example.workers.dev")
        parser.add_argument("--token", help="shared secret stored in the config file")
        parser.add_argument("--token-env", help="environment variable holding the secret "
                            "(default SILKCODE_SANDBOX_TOKEN)")
        args = parser.parse_args(argv[1:])
        config = Config.load()
        sandbox: dict = {"url": args.url.rstrip("/")}
        if args.token:
            sandbox["token"] = args.token
        if args.token_env:
            sandbox["token_env"] = args.token_env
        config.data["sandbox"] = sandbox
        config.save()
        print(f"Sandbox configured: {sandbox['url']}")
        print("Use it with: silkcode --sandbox <path>   (or silkcode gui --sandbox <path>)")
        return _sandbox_status()
    if argv and argv[0] == "disconnect":
        config = Config.load()
        config.data.pop("sandbox", None)
        config.save()
        print("Sandbox removed from configuration.")
        return 0
    if argv and argv[0] == "serve":
        parser = argparse.ArgumentParser(prog="silkcode sandbox serve")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8390)
        parser.add_argument("--token", required=True, help="shared secret clients must present")
        parser.add_argument("--dir", default=None, help="directory for synced workspaces")
        args = parser.parse_args(argv[1:])
        from pathlib import Path
        from ..config import config_dir
        from ..sandbox_server import serve
        base_dir = Path(args.dir) if args.dir else config_dir() / "sandbox-workspaces"
        return serve(base_dir, args.token, args.host, args.port)
    return _sandbox_status()


def _sandbox_status() -> int:
    from ..execbackend import remote_backend_from_config
    from ..workspace import ToolError
    config = Config.load()
    if not (config.data.get("sandbox") or {}).get("url"):
        print("No sandbox configured.")
        print("Self-hosted:  silkcode sandbox serve --token <secret>  (on the machine that should run commands)")
        print("              silkcode sandbox connect http://<host>:8390 --token <secret>")
        print("Cloudflare:   deploy sandbox/cloudflare-worker, then connect its URL")
        return 0
    try:
        backend = remote_backend_from_config(config.data)
        health = backend.health()
        print(f"Sandbox: {backend.url}")
        print(f"Status: reachable (protocol v{health.get('version', '?')})")
        return 0
    except ToolError as exc:
        print(f"Sandbox: {config.data['sandbox']['url']}")
        print(f"Status: NOT reachable - {exc}")
        return 1


def cmd_sessions(argv: list[str]) -> int:
    store = SessionStore()
    sessions = store.list()
    if not sessions:
        print("No saved sessions.")
        return 0
    for s in sessions:
        when = datetime.datetime.fromtimestamp(s["updated"]).strftime("%Y-%m-%d %H:%M")
        print(f"#{s['id']:<5} {when}  {s['model']:<28} {s['title']}")
    print("\nResume with: silkcode resume <id>")
    return 0


def cmd_resume(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode resume")
    parser.add_argument("id", type=int)
    args = parser.parse_args(argv)
    store = SessionStore()
    try:
        data = store.load(args.id)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    from .repl import run_repl
    return run_repl(data.get("cwd", "."), data.get("model"), data.get("mode", "ask"), resume=data)


if __name__ == "__main__":
    sys.exit(main())
