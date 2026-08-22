"""silkcode CLI entry point (SRS section 46).

Commands:
    silkcode [path] [--model M] [--mode ask|edit|agent]   interactive REPL
    silkcode new [name] [--template T] [--dir D] [...]     scaffold a new project
    silkcode gui [path] [--port N] [--host H] [--model M] local web GUI
    silkcode models                                        list providers and models
    silkcode models add <name> --base-url URL [...]        onboard a provider/endpoint
    silkcode models pull <model>                           pull a model into Ollama
    silkcode models default <spec>                         set the default model
    silkcode inference                                     status of a linked inference server
    silkcode inference discover                            find model servers on this network
    silkcode inference link <host|url>                     run the models on another machine
    silkcode inference ping [--chat]                       is it up, and can it generate?
    silkcode inference host                                (on that machine) how to let it in
    silkcode sessions                                      list saved sessions
    silkcode resume <id>                                   resume a session in the REPL
    silkcode config                                        show configuration
    silkcode swarm [path] [--model M] [...]                multi-agent improvement loop
    silkcode update [--branch B]                           pull updates and hot-apply them
    silkcode sync [path] [--apply]                         check/reconcile a branch that moved
    silkcode version [--json]                              what this install is, and how to update it

Run several GUI instances on one machine - each on its own --host/--port and
project. Session ids are unique across instances and every session is tagged
with the instance that created it (see `silkcode sessions`).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

from ..config import Config, ConfigError
from ..providers import build_provider
from ..sessions import SessionStore


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "new": cmd_new,
        "models": cmd_models,
        "config": cmd_config,
        "env": cmd_env,
        "sessions": cmd_sessions,
        "resume": cmd_resume,
        "gui": cmd_gui,
        "test": cmd_test,
        "review": cmd_review,
        "mcp": cmd_mcp,
        "connect": cmd_connect,
        "inference": cmd_inference,
        "benchmark": cmd_benchmark,
        "swarm": cmd_swarm,
        "update": cmd_update,
        "sandbox": cmd_sandbox,
        "sync": cmd_sync,
        "version": cmd_version,
    }
    if argv and argv[0] in commands:
        try:
            return commands[argv[0]](argv[1:])
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return cmd_repl(argv)


def _repl_parser(prog: str) -> argparse.ArgumentParser:
    from ..version import build_id
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--version", "-V", action="version",
                        version=f"Silk Code {build_id()}",
                        help="print the version and exit "
                             "('silkcode version' for the full report)")
    parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current)")
    parser.add_argument("--model", "-m", help="model spec, e.g. 'deepseek' or 'ollama/qwen2.5-coder'")
    parser.add_argument("--mode", choices=("ask", "edit", "agent"), default="ask", help="permission mode")
    parser.add_argument("--allow", help="pre-authorize git operations without prompts, "
                        "comma-separated from: pull,commit,push,merge")
    parser.add_argument("--sandbox", action="store_true",
                        help="run commands in the configured remote sandbox "
                             "(silkcode sandbox connect <url>)")
    parser.add_argument("--remote", metavar="REPO",
                        help="work on a GitHub repo that lives entirely in the sandbox "
                             "(e.g. 'github:owner/repo'); the repo never touches this "
                             "machine - requires a configured sandbox")
    parser.add_argument("--auto-push", action="store_true",
                        help="automatically push unpushed commits after each turn "
                             "(implies the push grant)")
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
                    grants=_parse_grants(args.allow), use_sandbox=args.sandbox,
                    auto_push=args.auto_push, remote=args.remote)


def cmd_new(argv: list[str]) -> int:
    """Scaffold a new project from a template (SRS section 10: a session needs
    a project, and sometimes the project does not exist yet)."""
    from ..scaffold import (DEFAULT_TEMPLATE, TEMPLATES, create_project, format_result,
                            get_template, prompt_for_new_project, template_names)
    from ..workspace import ToolError

    parser = argparse.ArgumentParser(
        prog="silkcode new",
        description="Create a new project from a template, git-init it, and "
                    "optionally start working on it right away.")
    parser.add_argument("name", nargs="?",
                        help="project name; omit to be prompted for name and template")
    parser.add_argument("--template", "-t", default=None,
                        help=f"template to use (default: {DEFAULT_TEMPLATE}); "
                             f"one of: {', '.join(template_names())}")
    parser.add_argument("--dir", "-d", dest="parent", default=".",
                        help="directory to create the project in (default: current)")
    parser.add_argument("--describe", default="",
                        help="one-line description, written into the README, "
                             "SILKCODE.md and package metadata")
    parser.add_argument("--no-git", action="store_true",
                        help="do not run 'git init' or make the initial commit")
    parser.add_argument("--force", action="store_true",
                        help="scaffold into an existing non-empty directory "
                             "(existing files are never overwritten)")
    parser.add_argument("--list", action="store_true", help="list templates and exit")
    parser.add_argument("--open", action="store_true",
                        help="open the new project in the interactive REPL when done")
    parser.add_argument("--prompt", "-p",
                        help="after creating it, run one agent turn in the new project "
                             "(e.g. 'add a --json flag to the CLI')")
    parser.add_argument("--model", "-m", help="model spec for --prompt / --open")
    parser.add_argument("--mode", choices=("ask", "edit", "agent"), default="ask",
                        help="permission mode for --prompt / --open (default: ask)")
    args = parser.parse_args(argv)

    if args.list:
        print("Templates:")
        for name in template_names():
            marker = "  (default)" if name == DEFAULT_TEMPLATE else ""
            print(f"  {name:<12} {TEMPLATES[name].description}{marker}")
        print("\nCreate one with: silkcode new <name> --template <template>")
        return 0

    try:
        if args.template:
            get_template(args.template)  # fail before creating anything
        if args.name:
            result = create_project(args.name, template=args.template or DEFAULT_TEMPLATE,
                                    parent=args.parent, description=args.describe,
                                    git=not args.no_git, force=args.force)
        else:
            result = prompt_for_new_project(parent=args.parent)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from ..project import record_recent_project
    record_recent_project("local", str(result.path), str(result.path))
    print(format_result(result))

    if args.prompt or args.open:
        from .repl import run_repl
        if args.prompt:
            print()
            code = run_repl(str(result.path), args.model, args.mode, prompt=args.prompt)
            if code != 0:
                return code
        if args.open:
            return run_repl(str(result.path), args.model, args.mode)
    return 0


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
    parser.add_argument("--token", help="access token required on every request; "
                        "generated automatically when the daemon is reachable "
                        "beyond this machine (--host other than loopback)")
    args = parser.parse_args(argv)
    from ..gui.server import run_gui
    # Normalized launch args so the daemon can re-exec itself with the same
    # configuration after a self-update (silkcode update / GUI Update button).
    restart_args = ["gui", args.path or "."]
    if args.model:
        restart_args += ["--model", args.model]
    restart_args += ["--mode", args.mode]
    if args.host != "127.0.0.1":
        restart_args += ["--host", args.host]
    if args.port != 8377:
        restart_args += ["--port", str(args.port)]
    if args.allow:
        restart_args += ["--allow", args.allow]
    if args.sandbox:
        restart_args += ["--sandbox"]
    if args.remote:
        restart_args += ["--remote", args.remote]
    if args.auto_push:
        restart_args += ["--auto-push"]
    return run_gui(args.path, args.model, args.mode, host=args.host, port=args.port,
                   grants=_parse_grants(args.allow), use_sandbox=args.sandbox,
                   auto_push=args.auto_push, restart_args=restart_args,
                   remote=args.remote, token=args.token)


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
        timeout = cfg.get("timeout")
        timeout_status = ""
        if timeout:
            try:
                shown = int(timeout) if float(timeout).is_integer() else float(timeout)
            except (TypeError, ValueError):
                shown = timeout
            timeout_status = f"timeout: {shown}s"
        print(f"{name:<12} {cfg.get('base_url', ''):<55} default: {default:<24} {key_status} {timeout_status}")
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
    parser.add_argument("--timeout", type=float, default=None,
                        help="request timeout in seconds (default 180); raise it for slow "
                             "providers like DeepSeek that can exceed 3 minutes to first token")
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
    if args.timeout is not None:
        cfg["timeout"] = args.timeout
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
    parser.add_argument("--from-history", nargs="?", const=".", metavar="REPO",
                        help="build the task set from this repository's own merged work "
                             "instead of the built-in suite (default: current directory)")
    parser.add_argument("--limit", type=int, default=5,
                        help="how many mined tasks to collect (default 5)")
    parser.add_argument("--scan", type=int, default=60,
                        help="how many recent changes to consider when mining (default 60)")
    parser.add_argument("--difficulty",
                        help="comma-separated tiers to keep when mining: easy,medium,hard")
    parser.add_argument("--tasks-file", help="run a previously mined task file")
    parser.add_argument("--mine-only", action="store_true",
                        help="mine and save tasks without running any model")
    args = parser.parse_args(argv)

    from ..benchmark import TASKS, format_results, run_benchmark
    from ..workspace import ToolError
    if args.list:
        for task in TASKS:
            print(f"{task.id:<16} {task.prompt[:80]}")
        return 0

    mined_tasks = None
    if args.from_history or args.tasks_file or args.mine_only:
        from ..config import config_dir
        from ..histbench import load_tasks, mine, save_tasks, to_bench_task
        try:
            if args.tasks_file:
                mined = load_tasks(Path(args.tasks_file))
                print(f"loaded {len(mined)} mined task(s) from {args.tasks_file}")
            else:
                difficulties = ([d.strip() for d in args.difficulty.split(",")]
                                if args.difficulty else None)
                mined = mine(args.from_history or ".", limit=args.limit, scan=args.scan,
                             difficulties=difficulties, on_progress=print)
                if not mined:
                    print("error: no tasks could be mined; try --scan with a larger number "
                          "or --difficulty easy", file=sys.stderr)
                    return 1
                out = save_tasks(mined, config_dir() / "benchmarks" /
                                 f"tasks-{int(time.time())}.json")
                print(f"\n{len(mined)} task(s) saved to {out}")
        except ToolError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for task in mined:
            print(f"  {task.id:<16} [{task.difficulty}] {task.subject[:60]}")
        if args.mine_only:
            return 0
        mined_tasks = [to_bench_task(t) for t in mined]

    specs = args.models or [Config.load().default_model]
    task_ids = [t.strip() for t in args.tasks.split(",")] if args.tasks else None
    try:
        results = run_benchmark(specs, task_ids, ab=args.ab, tasks=mined_tasks,
                                on_progress=print)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print()
    print(format_results(results))
    return 0


def cmd_update(argv: list[str]) -> int:
    """Update the installed Silk Code from its git remote (no manual restart
    needed: a running GUI daemon hot-applies the new code by re-execing
    itself once the pull lands)."""
    import subprocess as _subprocess
    parser = argparse.ArgumentParser(prog="silkcode update")
    parser.add_argument("--branch", help="branch to update to (default: current tracked branch)")
    parser.add_argument("--force", action="store_true",
                        help="update even with a dirty working tree")
    parser.add_argument("--install", action="store_true",
                        help="also run 'pip install -e .' after pulling (for new dependencies)")
    args = parser.parse_args(argv)

    from ..update import git_repo_root, update_installation
    repo = git_repo_root()
    if repo is None:
        print("error: silkcode is not installed from a git checkout.", file=sys.stderr)
        print("Update it with: pip install -U silkcode", file=sys.stderr)
        return 1
    result = update_installation(repo=repo, branch=args.branch, force=args.force,
                                 on_progress=print)
    if args.install and result["status"] == "updated":
        print("installing editable package ...")
        proc = _subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."],
                               cwd=repo, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            print(f"error: pip install failed: {proc.stderr.strip()[-300:]}", file=sys.stderr)
            return 1
    print(result["detail"])
    if result["status"] == "updated":
        print("The GUI daemon (if running) will restart itself with the new code automatically.")
        return 0
    return 0 if result["status"] == "up-to-date" else 1


def cmd_swarm(argv: list[str]) -> int:
    """Run the multi-agent improvement swarm (tester + critic + worker) until
    the workspace scores 10/10, the score stalls, or a cap is reached."""
    parser = argparse.ArgumentParser(
        prog="silkcode swarm",
        description="Multi-agent improvement loop: tester, critic and worker agents "
                    "iterate until the workspace scores 10/10 (or the loop stalls).")
    parser.add_argument("path", nargs="?", default=".",
                        help="workspace directory (default: current)")
    parser.add_argument("--model", "-m", help="model for all roles (default: config default)")
    parser.add_argument("--critic-model", help="model for the critic role (default: --model)")
    parser.add_argument("--tester-model", help="model for the tester role (default: --model)")
    parser.add_argument("--target", type=float, default=10.0,
                        help="score to reach before stopping (default: 10)")
    parser.add_argument("--max-iterations", type=int, default=0,
                        help="hard cap on iterations; 0 = run without end (default: 0)")
    parser.add_argument("--stall-limit", type=int, default=3,
                        help="stop after this many non-improving iterations (default: 3)")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="stop once this many model tokens are spent; 0 = no budget (default: 0)")
    parser.add_argument("--no-skip-tester", action="store_true",
                        help="always run the tester, even when the tests already pass "
                             "(default: tester is skipped on a green suite)")
    parser.add_argument("--test-command", help="explicit test command (auto-detected when omitted)")
    args = parser.parse_args(argv)

    from ..swarm import format_swarm_report, run_swarm
    from ..workspace import ToolError, Workspace
    try:
        ws = Workspace(args.path)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = run_swarm(
            ws,
            worker_spec=args.model or Config.load().default_model,
            critic_spec=args.critic_model,
            tester_spec=args.tester_model,
            target=args.target,
            max_iterations=args.max_iterations,
            stall_limit=args.stall_limit,
            max_tokens=args.max_tokens,
            skip_tester_when_tests_pass=not args.no_skip_tester,
            test_command=args.test_command,
            on_progress=print,
        )
    except (ConfigError, ValueError, ToolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print()
    print(format_swarm_report(result))
    return 0


def cmd_connect(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="silkcode connect")
    parser.add_argument("service", choices=["github"])
    parser.add_argument("path", nargs="?", default=".", help="workspace to check the git remote in")
    parser.add_argument("--token-env", help="environment variable holding a token (default GITHUB_TOKEN)")
    parser.add_argument("--client-id", help="GitHub App client id for device-flow sign-in (saved to config)")
    args = parser.parse_args(argv)

    from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, get_token
    from ..github_oauth import DeviceFlow, DeviceFlowError, client_id_from, store_token
    from ..workspace import ToolError, Workspace

    config = Config.load()
    if args.token_env:
        config.data.setdefault("github", {})["token_env"] = args.token_env
        config.save()
        print(f"GitHub token will be read from ${args.token_env}")
    if args.client_id:
        config.data.setdefault("github", {})["client_id"] = args.client_id
        config.save()
        print("GitHub App client id saved.")

    token = get_token(config)
    if not token:
        client_id = client_id_from(config.data)
        if client_id:
            # Sign in with GitHub (device flow) - no tokens to create or paste.
            try:
                flow = DeviceFlow(client_id)
                info = flow.start()
                print(f"\nSign in with GitHub:")
                print(f"  1. Open {info['verification_uri']}")
                print(f"  2. Enter code: {info['user_code']}")
                print("  3. Click Authorize\n")
                try:
                    import webbrowser
                    webbrowser.open(info["verification_uri"])
                except Exception:
                    pass
                print("Waiting for authorization ...")
                data = flow.poll(info["device_code"], int(info.get("interval", 5)),
                                 int(info.get("expires_in", 900)))
                store_token(config, data)
                token = data["access_token"]
                print("Authorized.")
            except DeviceFlowError as exc:
                print(f"Sign-in failed: {exc}", file=sys.stderr)
                return 1
        else:
            print("Not connected. Two options:\n")
            print("A) Sign in with GitHub (recommended, no tokens):")
            print("   The Silk Code GitHub App needs a client id. Maintainers: register it once")
            print("   (see docs/GITHUB_APP.md), then everyone can just run 'silkcode connect github'.")
            print("   If you have the client id: silkcode connect github --client-id <id>\n")
            print("B) Personal access token:")
            print("   Create one at https://github.com/settings/personal-access-tokens (Contents,")
            print("   Pull requests, Issues: read/write), then: export GITHUB_TOKEN=... and rerun.")
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


def cmd_env(argv: list[str]) -> int:
    """Credentials, token usage and sessions — the CLI view of the GUI's
    Environment page."""
    parser = argparse.ArgumentParser(prog="silkcode env")
    parser.add_argument("--set", metavar="PROVIDER",
                        help="store a key for this provider (read from $SILKCODE_KEY "
                             "or prompted, never echoed)")
    parser.add_argument("--clear", metavar="PROVIDER", help="remove a stored key")
    args = parser.parse_args(argv)

    import os
    from ..environment import clear_key, credentials, set_key, usage

    config = Config.load()
    if args.set:
        key = os.environ.get("SILKCODE_KEY")
        if not key:
            import getpass
            key = getpass.getpass(f"key for {args.set}: ")
        try:
            result = set_key(config, args.set, key)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"stored key {result['masked']} for {args.set} in {config.path}")
        return 0
    if args.clear:
        try:
            result = clear_key(config, args.clear)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"cleared stored key for {args.clear}"
              + (f" ({result['note']})" if result["note"] else ""))
        return 0

    print(f"config: {config.path}   default model: {config.default_model}\n")
    print("CREDENTIALS")
    for c in credentials(config):
        state = (f"set {c['masked']}" if c["set"]
                 else ("MISSING" if c["needs_key"] else "not required"))
        source = f" [{c['source']}]" if c["source"] else ""
        env_var = f" ${c['env_var']}" if c["env_var"] else ""
        print(f"  {c['provider']:<12} {state:<16}{source}{env_var}")

    stats = usage()
    totals = stats["totals"]
    print(f"\nTOKEN USAGE   {totals['total_tokens']:,} total "
          f"({totals['prompt_tokens']:,} in / {totals['completion_tokens']:,} out) "
          f"across {totals['sessions']} session(s)")
    for entry in stats["by_model"]:
        if entry["total_tokens"]:
            print(f"  {entry['model']:<32} {entry['total_tokens']:>10,} "
                  f"({entry['sessions']} session(s))")
    if stats["by_day"]:
        print("\n  last 7 days:")
        for day in stats["by_day"]:
            print(f"    {day['day']}  {day['total_tokens']:>10,}")

    store = SessionStore()
    sessions = store.list()
    instances = {s.get("instance") for s in sessions if s.get("instance")}
    print(f"\nSESSIONS   {len(sessions)} saved"
          + (f", from {len(instances)} GUI instance(s)" if instances else ""))
    for s in sessions[:5]:
        print(f"  #{s['id']:<5} {s['model']:<28} {s['title'][:40]}")
    if len(sessions) > 5:
        print(f"  ... and {len(sessions) - 5} more (silkcode sessions)")
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


def cmd_version(argv: list[str]) -> int:
    """Report what this install actually is, in enough detail to act on."""
    parser = argparse.ArgumentParser(
        prog="silkcode version",
        description="Print the version, the commit it was built from, and how "
                    "to update this particular install.")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (for bug reports and scripts)")
    args = parser.parse_args(argv)

    from ..version import info, report
    print(json.dumps(info(), indent=2) if args.json else report())
    return 0


def cmd_sync(argv: list[str]) -> int:
    """Report whether the branch has moved underneath this workspace, and
    optionally reconcile it."""
    parser = argparse.ArgumentParser(
        prog="silkcode sync",
        description="Fetch and report where this branch stands against its "
                    "upstream and the base branch. Without --apply it changes "
                    "nothing.")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--apply", action="store_true",
                        help="perform the suggested action (fast-forward or merge)")
    parser.add_argument("--restart", action="store_true",
                        help="when the branch is already merged as a squash, move it "
                             "onto the base branch (implies --apply)")
    parser.add_argument("--base", help="branch to measure against (default: the "
                                       "remote's default branch)")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)

    from ..sync import resync, survey
    from ..workspace import ToolError, Workspace
    try:
        ws = Workspace(args.path)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.apply or args.restart:
        print(resync(ws, remote=args.remote, base=args.base,
                     allow_restart=args.restart))
        return 0

    state = survey(ws, remote=args.remote, base=args.base)
    print(state.summary())
    # a branch that needs attention is worth a non-zero exit, so this can gate
    # a script: `silkcode sync || silkcode sync --apply`
    return 0 if state.recommendation()[0] in ("none", "push") else 1


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
        instance = s.get("instance")
        where = f"  [{instance}]" if instance else ""
        print(f"#{s['id']:<5} {when}  {s['model']:<28} {s['title']}{where}")
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



# ---- inference: drive from the phone, run the model on the laptop -----------

def cmd_inference(argv: list[str]) -> int:
    """`silkcode inference ...` - point this install at a model server on
    another machine (SRS section 20: local models, not necessarily this CPU).

    The shape of the problem: the phone is where you want to type and the
    laptop is where the weights are. Everything here is either finding that
    laptop, proving it answers, or telling the laptop to let the phone in.
    """
    subcommands = {
        "discover": _inference_discover,
        "link": _inference_link,
        "unlink": _inference_unlink,
        "ping": _inference_ping,
        "host": _inference_host,
    }
    if argv and argv[0] in subcommands:
        return subcommands[argv[0]](argv[1:])
    if argv and argv[0].startswith("-"):
        argparse.ArgumentParser(prog="silkcode inference").parse_args(argv)
    if argv:
        print(f"error: unknown subcommand '{argv[0]}' "
              f"(try: {', '.join(sorted(subcommands))})", file=sys.stderr)
        return 1
    return _inference_status()


def _inference_status() -> int:
    from ..inference import linked_providers, probe

    config = Config.load()
    linked = linked_providers(config)
    if not linked:
        print("No inference server linked - this install runs models locally or in the cloud.\n")
        print("To run the models on your laptop and drive them from here:")
        print("  1. on the laptop:  silkcode inference host")
        print("  2. here:           silkcode inference discover")
        print("  3. here:           silkcode inference link <address-it-found>")
        print()
        _print_cloud_chain(config)
        return 0
    print(f"default model: {config.default_model}\n")
    down = 0
    for name, cfg in sorted(linked.items()):
        url = cfg.get("base_url", "")
        result = probe(url, token=config.api_key_for(cfg), timeout=4.0)
        if result.ok:
            print(f"{name:<12} {url:<40} up "
                  f"({result.latency_ms:.0f} ms, {len(result.models)} models)")
        else:
            down += 1
            print(f"{name:<12} {url:<40} unreachable")
            print(f"{'':<12} {result.error}")
    print()
    _print_cloud_chain(config, skip=frozenset(linked))
    print("\nCheck a round trip through the model with: silkcode inference ping --chat")
    # Same convention as `silkcode sandbox`: a status command that found the
    # thing it describes to be down exits non-zero, so a script can act on it.
    return 1 if down else 0


def _cloud_chain(config, skip: frozenset = frozenset()) -> list[tuple[str, bool, str]]:
    """The direct-to-provider endpoints `auto` falls through to.

    Linking a laptop must never look like it took DeepSeek or Kimi away - those
    are still one `--model deepseek` away, and they are what answers when the
    laptop is asleep or on a network it cannot be reached from. Returned as
    (name, ready-right-now, what-it-is-missing).
    """
    from ..config import DEFAULT_AUTO_ORDER

    # The auto chain first, because that is the order they would be tried in,
    # then anything else that holds a key. Cloudflare is the reason for the
    # second half: it is a built-in provider that is not in the auto order, and
    # leaving it off this list would tell a user who has one configured that
    # they have no cloud provider at all.
    order = list(config.data.get("auto_order") or DEFAULT_AUTO_ORDER)
    order += sorted(n for n in config.providers if n not in order)

    chain: list[tuple[str, bool, str]] = []
    for name in order:
        cfg = config.providers.get(name)
        if not cfg or name in skip:
            continue
        if not (cfg.get("api_key_env") or cfg.get("api_key")):
            continue  # a local server: whether it can serve depends on it running
        if "{account_id}" in cfg.get("base_url", "") and not cfg.get("account_id"):
            chain.append((name, False, "needs --account-id"))
        elif not config.api_key_for(cfg):
            chain.append((name, False, f"needs ${cfg.get('api_key_env', 'an API key')}"))
        elif not cfg.get("default_model"):
            chain.append((name, False, "needs a model"))
        else:
            chain.append((name, True, ""))
    return chain


def _print_cloud_chain(config, skip: frozenset = frozenset()) -> None:
    chain = _cloud_chain(config, skip=skip)
    if not chain:
        return
    ready = [name for name, ok, _ in chain if ok]
    missing = [name for name, ok, _ in chain if not ok]
    if ready:
        print("Direct to a cloud provider: " + ", ".join(ready))
        print(f"  available at any time:  silkcode --model {ready[0]}")
    else:
        print("No cloud provider is set up yet - Silk Code also talks straight to "
              "DeepSeek, Kimi and the rest.")
    if missing:
        # Names only: `silkcode models` already prints the env var each one wants,
        # and six "needs $SOMETHING_API_KEY" clauses on one line reads as noise.
        print(f"  not set up: {', '.join(missing)}"
              "   (silkcode models shows what each needs)")


def _inference_discover(argv: list[str]) -> int:
    from ..inference import KNOWN_PORTS, InferenceError, discover, local_ipv4, preferred_model

    parser = argparse.ArgumentParser(
        prog="silkcode inference discover",
        description="sweep this network for Ollama / LM Studio / vLLM / llama.cpp servers")
    parser.add_argument("--host", action="append", dest="hosts", metavar="HOST",
                        help="check this host only (repeatable); skips the sweep")
    parser.add_argument("--port", action="append", type=int, dest="ports", metavar="PORT",
                        help="extra port to try (repeatable)")
    parser.add_argument("--prefix", type=int, default=24,
                        help="how much of the subnet to sweep (default 24 = 254 addresses)")
    parser.add_argument("--timeout", type=float, default=0.35,
                        help="per-address connect timeout in seconds (default 0.35)")
    parser.add_argument("--token", help="bearer token, if the servers require one")
    args = parser.parse_args(argv)

    ports = [p for p, _ in KNOWN_PORTS]
    ports += [p for p in (args.ports or []) if p not in ports]
    own = local_ipv4()
    if args.hosts:
        print(f"Checking {len(args.hosts)} host(s) on {len(ports)} ports ...")
    else:
        print(f"Sweeping {own or '?'}/{args.prefix} on ports "
              f"{', '.join(str(p) for p in ports)} ...")
    try:
        found = discover(hosts=args.hosts, ports=ports, prefix=args.prefix,
                         connect_timeout=args.timeout, token=args.token)
    except InferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not found:
        print("\nNothing found.")
        print("On the machine that should run the model, run: silkcode inference host")
        print("It prints the address to use and how to open the server to this network.")
        return 1
    print()
    for result in found:
        model = preferred_model(result.models)
        print(f"{result.url:<34} {result.server:<10} {result.latency_ms:>6.0f} ms  "
              f"{len(result.models)} models" + (f" (e.g. {model})" if model else ""))
    print(f"\nLink one with: silkcode inference link {found[0].url}")
    return 0


def _inference_link(argv: list[str]) -> int:
    from ..inference import (DEFAULT_LINK_NAME, InferenceError, link, normalize_url,
                             preferred_model, probe)

    parser = argparse.ArgumentParser(
        prog="silkcode inference link",
        description="point this install at a model server on another machine")
    parser.add_argument("address", help="host, host:port or URL, e.g. 192.168.1.20:11434")
    parser.add_argument("--name", default=DEFAULT_LINK_NAME,
                        help=f"provider name to save it as (default {DEFAULT_LINK_NAME})")
    parser.add_argument("--model", help="default model on that server "
                                        "(default: the best-looking one it reports)")
    parser.add_argument("--token", help="bearer token stored in the config file")
    parser.add_argument("--token-env", help="environment variable holding the bearer token "
                                            "(preferred over --token)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="request timeout in seconds; a laptop loading a big model "
                             "cold can take a while to answer the first turn")
    parser.add_argument("--no-default", action="store_true",
                        help="save the provider but leave the default model alone")
    parser.add_argument("--force", action="store_true",
                        help="save it even if it does not answer right now")
    args = parser.parse_args(argv)

    config = Config.load()
    token = args.token or (os.environ.get(args.token_env) if args.token_env else None)
    if args.token:
        # Same trade-off `models add` flags: the config file is chmod 0600, but an
        # environment variable keeps the secret out of a file that gets synced,
        # backed up and pasted into bug reports.
        print("warning: storing the token in the config file; prefer --token-env",
              file=sys.stderr)
    try:
        url = normalize_url(args.address, default_port=11434)
    except InferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Probing {url} ...")
    result = probe(url, token=token)
    if not result.ok:
        print(f"error: {result.error}", file=sys.stderr)
        if not args.force:
            print("\nOn the machine running the model: silkcode inference host", file=sys.stderr)
            print("Save it anyway (e.g. the laptop is asleep) with --force.", file=sys.stderr)
            return 1
        print("warning: saving an endpoint that did not answer (--force)", file=sys.stderr)
        result.kind = "ollama" if (result.port == 11434) else "openai_compat"
        result.base_url = url if result.kind == "ollama" else f"{url}/v1"

    model = args.model or preferred_model(result.models)
    if args.model and result.models and args.model not in result.models:
        print(f"warning: {url} does not list '{args.model}' "
              f"(it has: {', '.join(result.models[:6])})", file=sys.stderr)
    saved = link(config, args.name, result, model=model, token=args.token,
                 token_env=args.token_env, timeout=args.timeout,
                 make_default=not args.no_default)
    print(f"Linked '{args.name}' -> {saved['base_url']} ({result.server or 'model server'})")
    if result.models:
        print(f"Models: {', '.join(result.models[:8])}"
              + (f" (+{len(result.models) - 8} more)" if len(result.models) > 8 else ""))
    if model:
        spec = f"{args.name}/{model}"
        print(f"Default model: {config.default_model}" if not args.no_default
              else f"Use it with: silkcode --model {spec}")
    else:
        print(f"Use it with: silkcode --model {args.name}/<model-name>")
    print(f"Saved in {config.path}")
    print("\n'auto' now tries this server first and falls back to the cloud when it is away.")
    _print_cloud_chain(config, skip=frozenset([args.name]))
    return 0


def _inference_unlink(argv: list[str]) -> int:
    from ..inference import DEFAULT_LINK_NAME, linked_providers, unlink

    parser = argparse.ArgumentParser(prog="silkcode inference unlink")
    parser.add_argument("name", nargs="?", default=None,
                        help="provider to remove (default: the only linked one)")
    args = parser.parse_args(argv)

    config = Config.load()
    linked = linked_providers(config)
    name = args.name
    if name is None:
        if len(linked) == 1:
            name = next(iter(linked))
        elif not linked:
            print("Nothing linked.")
            return 0
        else:
            print(f"error: several servers are linked ({', '.join(sorted(linked))}); "
                  "name the one to remove", file=sys.stderr)
            return 1
    if not unlink(config, name):
        print(f"error: no provider named '{name}' in {config.path}", file=sys.stderr)
        return 1
    print(f"Unlinked '{name}'. Default model is now: {config.default_model}")
    return 0


def _inference_ping(argv: list[str]) -> int:
    """Is it up, and can it actually generate?

    Two different questions, and the gap between them is where the bad
    surprises live: a laptop answers /api/tags in a millisecond and can still
    take half a minute to produce a first token while it pages a 30B model in
    from disk.
    """
    from ..inference import (DEFAULT_LINK_NAME, InferenceError, linked_providers,
                             measure_chat, normalize_url, probe)

    parser = argparse.ArgumentParser(prog="silkcode inference ping")
    parser.add_argument("target", nargs="?",
                        help="a linked provider name or an address "
                             f"(default: '{DEFAULT_LINK_NAME}', or the only linked server)")
    parser.add_argument("--chat", action="store_true",
                        help="also send a one-word prompt and time the full round trip")
    parser.add_argument("--model", help="model to use for --chat")
    parser.add_argument("--count", type=int, default=3, help="how many probes (default 3)")
    parser.add_argument("--token", help="bearer token, if the server requires one")
    args = parser.parse_args(argv)

    config = Config.load()
    linked = linked_providers(config)
    name, cfg = None, None
    if args.target and args.target in config.providers:
        name, cfg = args.target, config.providers[args.target]
    elif args.target:
        try:
            url = normalize_url(args.target, default_port=11434)
        except InferenceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        cfg = {"base_url": url}
    elif len(linked) == 1:
        name, cfg = next(iter(linked.items()))
    elif DEFAULT_LINK_NAME in linked:
        name, cfg = DEFAULT_LINK_NAME, linked[DEFAULT_LINK_NAME]
    else:
        print("error: nothing linked to ping. Run: silkcode inference link <address>",
              file=sys.stderr)
        return 1

    url = cfg["base_url"]
    token = args.token or config.api_key_for(cfg)
    label = f"{name} ({url})" if name else url
    print(f"Pinging {label}")
    latencies: list[float] = []
    last = None
    for _ in range(max(1, args.count)):
        last = probe(url, token=token)
        if last.ok and last.latency_ms is not None:
            latencies.append(last.latency_ms)
            print(f"  reply in {last.latency_ms:.0f} ms")
        else:
            print(f"  no reply: {last.error}")
    if not latencies:
        print("\nUnreachable.", file=sys.stderr)
        print("If the laptop is awake and the server is running, it is probably bound to "
              "loopback there - run `silkcode inference host` on it.", file=sys.stderr)
        return 1
    best, worst = min(latencies), max(latencies)
    print(f"\n{len(latencies)}/{args.count} answered - "
          f"min {best:.0f} ms, avg {sum(latencies) / len(latencies):.0f} ms, max {worst:.0f} ms")
    if last and last.models:
        print(f"Models: {', '.join(last.models[:8])}")
    if not args.chat:
        print("\nThat is the server answering, not the model generating. "
              "Add --chat to time a real turn.")
        return 0

    model = args.model or cfg.get("default_model")
    if not model:
        from ..inference import preferred_model
        model = preferred_model(last.models if last else [])
    if not model:
        print("error: no model to test with; pass --model", file=sys.stderr)
        return 1
    chat_cfg = dict(cfg)
    chat_cfg.setdefault("type", "ollama" if (last and last.kind == "ollama") else "openai_compat")
    chat_cfg.setdefault("base_url", url)
    print(f"\nGenerating with {model} ...")
    try:
        reply, elapsed = measure_chat(chat_cfg, model, api_key=token)
    except InferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Reply in {elapsed:.0f} ms: {reply[:120] or '(empty)'}")
    if elapsed > 30_000:
        print("\nThat first turn was slow - usually the model being loaded into memory. "
              "The next one should be much faster; if it is not, try a smaller model.")
    return 0


def _inference_host(argv: list[str]) -> int:
    """Run this on the machine with the GPU. It answers one question: what do
    I type on the phone, and why can't the phone see me yet?

    Local model servers ship bound to 127.0.0.1, which is the right default and
    the exact reason a phone on the same Wi-Fi gets 'connection refused'. So
    the check that matters is not 'is it running' but 'is it listening on an
    address something else can reach'.
    """
    from ..inference import KNOWN_PORTS, local_ipv4_addresses, port_open, probe

    parser = argparse.ArgumentParser(
        prog="silkcode inference host",
        description="show what a phone or tablet needs to reach the model servers here")
    parser.add_argument("--port", action="append", type=int, dest="ports", metavar="PORT",
                        help="extra port to check (repeatable)")
    args = parser.parse_args(argv)

    ports = [p for p, _ in KNOWN_PORTS]
    ports += [p for p in (args.ports or []) if p not in ports]
    addresses = local_ipv4_addresses()
    if addresses:
        print("This machine is reachable at: " + ", ".join(addresses))
    else:
        print("This machine has no network address - it is offline or on a network that "
              "gives it none. Connect it to the same Wi-Fi as the phone.")
    print()

    reachable: list[tuple[str, str]] = []   # (url, server kind)
    loopback_only: list[int] = []
    for port in ports:
        if not port_open("127.0.0.1", port, timeout=0.3):
            continue
        exposed = [a for a in addresses if port_open(a, port, timeout=0.5)]
        if not exposed:
            loopback_only.append(port)
            continue
        for address in exposed:
            result = probe(f"http://{address}:{port}", timeout=3.0)
            if result.ok:
                reachable.append((result.url, result.server or "model server"))
                print(f"  {result.url:<28} {result.server:<12} "
                      f"{len(result.models)} models - reachable from the network")

    for port in loopback_only:
        kind = next((name for p, name in KNOWN_PORTS if p == port), "server")
        print(f"  port {port} ({kind}) is running, but only on 127.0.0.1 - "
              "nothing else can reach it")
        print("    " + _open_up_hint(kind, port))
    if not reachable and not loopback_only:
        print("  no model server is running here.")
        print("  Start one first, e.g.:  ollama serve   (https://ollama.com)")

    if reachable:
        url = reachable[0][0]
        print(f"\nOn the phone, run:\n  silkcode inference link {url}")
        print("\nIf that fails while this machine is awake, the firewall here is dropping "
              "the connection - allow inbound TCP on the port above for private networks.")
    return 0


def _open_up_hint(kind: str, port: int) -> str:
    """The one command that makes a loopback-bound server answer the LAN."""
    if kind == "ollama":
        return (f"restart it listening on every interface:  "
                f"OLLAMA_HOST=0.0.0.0:{port} ollama serve"
                "\n      (macOS app: launchctl setenv OLLAMA_HOST \"0.0.0.0:11434\" and restart Ollama;"
                "\n       Windows: set OLLAMA_HOST=0.0.0.0:11434 in your user environment variables)")
    if kind == "lmstudio":
        return "in LM Studio: Developer -> Server -> enable 'Serve on Local Network', then restart the server"
    if kind == "vllm":
        return f"restart it with:  vllm serve <model> --host 0.0.0.0 --port {port}"
    if kind == "llamacpp":
        return f"restart it with:  llama-server --host 0.0.0.0 --port {port} -m <model>"
    return f"restart it bound to 0.0.0.0 instead of 127.0.0.1 on port {port}"


if __name__ == "__main__":
    sys.exit(main())
