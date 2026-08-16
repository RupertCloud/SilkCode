"""Every CLI subcommand, driven through main() the way a shell invokes it.

The CLI is the whole user-facing surface and it is almost all glue: argument
parsing, dispatch, and the wiring between a command and the module behind it.
Glue is exactly where a signature drifts and nothing notices - `silkcode gui`
shipped broken because cmd_gui passed an argument run_gui did not accept and
no test ever ran the command.

So these tests call main() with real argv and assert on exit codes and
printed output. Anything that would reach the network or block on a terminal
is stubbed at the boundary; everything on this side of that boundary is the
real code.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys

import pytest

from silkcode.cli import main as cli_main
from silkcode.cli.main import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated ~/.silkcode so tests never read or write the real one."""
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(d))
    # A stray key in the ambient environment would change what `models` and
    # `env` print, so scrub the ones the built-in providers look at.
    for var in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY",
                "GLM_API_KEY", "MINIMAX_API_KEY", "OPENROUTER_API_KEY",
                "CLOUDFLARE_API_TOKEN", "GITHUB_TOKEN", "SILKCODE_KEY"):
        monkeypatch.delenv(var, raising=False)
    return d


@pytest.fixture
def no_ollama(monkeypatch):
    """Ollama is not running: probing it must not reach the network."""
    monkeypatch.setattr("silkcode.providers.ollama.OllamaProvider.list_models",
                        lambda self: [])


def run(argv, capsys):
    """Invoke the CLI and return (exit code, stdout, stderr)."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def test_every_dispatched_command_exists_and_is_callable():
    """The dispatch table is written by hand; a typo there is a dead command."""
    source = inspect.getsource(cli_main.main)
    names = dict(re.findall(r'"(\w+)":\s*(cmd_\w+)', source))
    assert names, "dispatch table not found in main()"
    for command, function in names.items():
        assert hasattr(cli_main, function), f"'{command}' dispatches to missing {function}"
        assert callable(getattr(cli_main, function))


def test_unknown_command_falls_through_to_the_repl(home, monkeypatch):
    """'silkcode somedir' is a path, not a bad command."""
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    assert main(["somedir"]) == 0
    assert seen["args"][0] == "somedir"


def test_config_errors_are_reported_not_raised(home, capsys):
    """A bad model spec should print an error, not dump a traceback."""
    code, out, err = run(["models", "default", "no-such-provider"], capsys)
    assert code == 1
    assert "error:" in err
    assert "no-such-provider" in err


# --------------------------------------------------------------------------
# new (project creation)
# --------------------------------------------------------------------------

def test_new_lists_the_templates(home, capsys):
    from silkcode.scaffold import TEMPLATES
    code, out, _ = run(["new", "--list"], capsys)
    assert code == 0
    for name in TEMPLATES:
        assert name in out


def test_new_creates_a_project_and_remembers_it(home, tmp_path, capsys):
    code, out, _ = run(["new", "My App", "--dir", str(tmp_path), "--no-git"], capsys)
    assert code == 0
    project = tmp_path / "my-app"
    assert (project / "pyproject.toml").is_file()
    assert (project / "SILKCODE.md").is_file()
    assert str(project) in out
    recent = json.loads((home / "recent_projects.json").read_text())
    assert recent[0]["spec"] == str(project.resolve())


def test_new_honors_the_template_flag(home, tmp_path, capsys):
    code, out, _ = run(["new", "site", "-t", "web", "--dir", str(tmp_path), "--no-git"], capsys)
    assert code == 0
    assert (tmp_path / "site" / "index.html").is_file()
    assert "web template" in out


def test_new_rejects_an_unknown_template_before_creating_anything(home, tmp_path, capsys):
    code, _, err = run(["new", "demo", "-t", "cobol", "--dir", str(tmp_path)], capsys)
    assert code == 1
    assert "unknown template" in err
    assert not (tmp_path / "demo").exists()


def test_new_rejects_a_bad_name(home, tmp_path, capsys):
    code, _, err = run(["new", "nested/name", "--dir", str(tmp_path)], capsys)
    assert code == 1
    assert "path separator" in err


def test_new_refuses_to_scribble_into_a_non_empty_directory(home, tmp_path, capsys):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "keep.txt").write_text("mine\n")
    code, _, err = run(["new", "taken", "--dir", str(tmp_path), "--no-git"], capsys)
    assert code == 1
    assert "--force" in err
    assert (tmp_path / "taken" / "keep.txt").read_text() == "mine\n"


def test_new_prompts_when_no_name_is_given(home, tmp_path, monkeypatch, capsys):
    answers = iter(["prompted", "web", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    code, out, _ = run(["new", "--dir", str(tmp_path)], capsys)
    assert code == 0
    assert (tmp_path / "prompted" / "index.html").is_file()


def test_new_prompt_flag_runs_one_agent_turn_in_the_new_project(home, tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    code, _, _ = run(["new", "demo", "--dir", str(tmp_path), "--no-git",
                      "-p", "add a --json flag", "--mode", "agent"], capsys)
    assert code == 0
    assert seen["args"][0] == str((tmp_path / "demo").resolve())
    assert seen["args"][2] == "agent"
    assert seen["kwargs"]["prompt"] == "add a --json flag"


def test_new_open_flag_starts_the_repl_in_the_new_project(home, tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    code, _, _ = run(["new", "demo", "--dir", str(tmp_path), "--no-git", "--open"], capsys)
    assert code == 0
    assert seen["args"][0] == str((tmp_path / "demo").resolve())
    assert "prompt" not in seen["kwargs"]


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

def test_models_lists_every_builtin_provider(home, no_ollama, capsys):
    code, out, _ = run(["models"], capsys)
    assert code == 0
    for provider in ("deepseek", "qwen", "kimi", "glm", "minimax", "openrouter",
                     "cloudflare", "ollama", "vllm", "lmstudio"):
        assert provider in out
    assert "default model: deepseek" in out


def test_models_reports_which_keys_are_missing(home, no_ollama, capsys, monkeypatch):
    _, out, _ = run(["models"], capsys)
    assert "key: MISSING ($DEEPSEEK_API_KEY)" in out
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-whatever")
    _, out, _ = run(["models"], capsys)
    assert "deepseek" in out
    # the key itself must never be printed, only its state
    assert "sk-whatever" not in out


def test_models_add_new_provider_requires_a_base_url(home, capsys):
    with pytest.raises(SystemExit):
        main(["models", "add", "brandnew"])


def test_models_add_writes_the_provider_to_config(home, capsys):
    code, out, _ = run(["models", "add", "myserver", "--base-url",
                        "http://localhost:9999/v1", "--model", "my-model"], capsys)
    assert code == 0
    assert "Added provider 'myserver'" in out
    data = json.loads((home / "config.json").read_text())
    assert data["providers"]["myserver"]["base_url"] == "http://localhost:9999/v1"
    assert data["providers"]["myserver"]["default_model"] == "my-model"


def test_models_add_configures_a_builtin_without_a_base_url(home, capsys):
    """Onboarding Cloudflare is supplying an account id, not a URL."""
    code, out, _ = run(["models", "add", "cloudflare", "--account-id", "acct123"], capsys)
    assert code == 0
    assert "Configured provider 'cloudflare'" in out
    assert "acct123" in json.loads((home / "config.json").read_text())["providers"]["cloudflare"]["account_id"]


def test_models_add_warns_when_a_key_is_stored_in_the_file(home, capsys):
    code, out, err = run(["models", "add", "myserver", "--base-url", "http://x/v1",
                          "--api-key", "sk-secret"], capsys)
    assert code == 0
    assert "prefer --api-key-env" in err


def test_models_default_validates_before_saving(home, capsys):
    code, _, err = run(["models", "default", "nonsense"], capsys)
    assert code == 1
    assert not (home / "config.json").exists(), "invalid default must not be saved"


def test_models_default_saves_a_valid_spec(home, capsys):
    code, out, _ = run(["models", "default", "ollama/qwen2.5-coder"], capsys)
    assert code == 0
    assert json.loads((home / "config.json").read_text())["default_model"] == "ollama/qwen2.5-coder"


def test_models_pull_rejects_a_non_ollama_provider(home, capsys):
    code, _, err = run(["models", "pull", "qwen2.5-coder", "--provider", "deepseek"], capsys)
    assert code == 1
    assert "not a configured Ollama provider" in err


def test_models_pull_explains_an_unreachable_ollama(home, capsys):
    """The most common failure: Ollama isn't installed or isn't running."""
    code, _, err = run(["models", "add", "deadollama", "--base-url",
                        "http://127.0.0.1:1/", "--type", "ollama"], capsys)
    assert code == 0
    code, _, err = run(["models", "pull", "qwen2.5-coder", "--provider", "deadollama"], capsys)
    assert code == 1
    assert "cannot reach Ollama" in err
    assert "https://ollama.com" in err


# --------------------------------------------------------------------------
# config / env  (credential handling)
# --------------------------------------------------------------------------

def test_config_redacts_a_stored_api_key(home, capsys):
    run(["models", "add", "myserver", "--base-url", "http://x/v1",
         "--api-key", "sk-do-not-print-me"], capsys)
    code, out, _ = run(["config"], capsys)
    assert code == 0
    assert "sk-do-not-print-me" not in out
    assert "********" in out


def test_env_shows_credentials_without_revealing_them(home, capsys, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-1234567890abcdef")
    code, out, _ = run(["env"], capsys)
    assert code == 0
    assert "CREDENTIALS" in out and "TOKEN USAGE" in out and "SESSIONS" in out
    assert "sk-1234567890abcdef" not in out
    assert "cdef" in out, "the masked tail should still be shown for identification"


def test_env_set_reads_the_key_from_the_environment_and_never_echoes_it(home, capsys, monkeypatch):
    monkeypatch.setenv("SILKCODE_KEY", "sk-supersecretvalue")
    code, out, _ = run(["env", "--set", "deepseek"], capsys)
    assert code == 0
    assert "sk-supersecretvalue" not in out
    stored = json.loads((home / "config.json").read_text())
    assert stored["providers"]["deepseek"]["api_key"] == "sk-supersecretvalue"


def test_env_set_stores_the_key_in_an_owner_only_file(home, capsys, monkeypatch):
    monkeypatch.setenv("SILKCODE_KEY", "sk-supersecretvalue")
    run(["env", "--set", "deepseek"], capsys)
    mode = (home / "config.json").stat().st_mode & 0o777
    assert mode == 0o600, f"config holding an API key is {oct(mode)}, expected 0o600"


def test_env_clear_removes_a_stored_key(home, capsys, monkeypatch):
    monkeypatch.setenv("SILKCODE_KEY", "sk-supersecretvalue")
    run(["env", "--set", "deepseek"], capsys)
    code, out, _ = run(["env", "--clear", "deepseek"], capsys)
    assert code == 0
    assert "api_key" not in json.loads((home / "config.json").read_text())["providers"]["deepseek"]


def test_env_rejects_an_unknown_provider(home, capsys, monkeypatch):
    monkeypatch.setenv("SILKCODE_KEY", "sk-x")
    code, _, err = run(["env", "--set", "nosuchprovider"], capsys)
    assert code == 1
    assert "error:" in err


# --------------------------------------------------------------------------
# sessions / resume
# --------------------------------------------------------------------------

def test_sessions_on_a_fresh_install(home, capsys):
    code, out, _ = run(["sessions"], capsys)
    assert code == 0
    assert "No saved sessions." in out


def test_sessions_lists_what_the_store_holds(home, capsys):
    from silkcode.sessions import SessionStore
    store = SessionStore()
    store.save({"id": store.new_id(), "model": "deepseek/deepseek-chat", "cwd": ".",
                "mode": "ask", "title": "fix the parser", "messages": []})
    code, out, _ = run(["sessions"], capsys)
    assert code == 0
    assert "fix the parser" in out
    assert "deepseek/deepseek-chat" in out
    assert "silkcode resume" in out


def test_resume_reports_a_missing_session(home, capsys):
    code, _, err = run(["resume", "999"], capsys)
    assert code == 1
    assert "error:" in err


def test_resume_hands_the_saved_state_to_the_repl(home, monkeypatch, capsys):
    from silkcode.sessions import SessionStore
    store = SessionStore()
    session_id = store.new_id()
    store.save({"id": session_id, "model": "ollama/x", "cwd": "/some/where",
                "mode": "agent", "title": "t", "messages": []})
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    assert main(["resume", str(session_id)]) == 0
    assert seen["args"][0] == "/some/where"
    assert seen["args"][2] == "agent"
    assert seen["kwargs"]["resume"]["title"] == "t"


# --------------------------------------------------------------------------
# gui  (the command that shipped broken)
# --------------------------------------------------------------------------

def test_gui_passes_only_arguments_run_gui_accepts(home, monkeypatch):
    """The regression that made every `silkcode gui` die with TypeError.

    The real signature has to be read before the stub replaces it, or we would
    be checking the stub's own **kwargs and this would pass for anything.
    """
    from silkcode.gui.server import run_gui
    accepted = set(inspect.signature(run_gui).parameters)
    assert "path" in accepted, "sanity: we captured the real signature"

    seen = {}
    monkeypatch.setattr("silkcode.gui.server.run_gui",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    assert main(["gui", ".", "--port", "1234"]) == 0

    positional = [p for p in accepted][:len(seen["args"])]
    passed = set(seen["kwargs"]) | set(positional)
    assert passed <= accepted, (
        f"cmd_gui passes {passed - accepted} which run_gui rejects")


def test_gui_forwards_every_flag(home, monkeypatch):
    seen = {}
    monkeypatch.setattr("silkcode.gui.server.run_gui",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    assert main(["gui", "/proj", "--model", "ollama/x", "--mode", "agent",
                 "--host", "0.0.0.0", "--port", "9001", "--allow", "push,commit",
                 "--token", "tok123", "--auto-push"]) == 0
    assert seen["args"] == ("/proj", "ollama/x", "agent")
    assert seen["kwargs"]["host"] == "0.0.0.0"
    assert seen["kwargs"]["port"] == 9001
    assert set(seen["kwargs"]["grants"]) == {"push", "commit"}
    assert seen["kwargs"]["token"] == "tok123"
    assert seen["kwargs"]["auto_push"] is True


def test_gui_restart_args_reproduce_the_invocation(home, monkeypatch):
    """After a self-update the daemon re-execs itself with these args; if they
    don't round-trip, the GUI comes back configured differently."""
    seen = {}
    monkeypatch.setattr("silkcode.gui.server.run_gui",
                        lambda *a, **k: seen.update(kwargs=k) or 0)
    main(["gui", "/proj", "--model", "ollama/x", "--mode", "agent",
          "--host", "0.0.0.0", "--port", "9001", "--allow", "push", "--auto-push"])
    restart = seen["kwargs"]["restart_args"]
    assert restart[0] == "gui" and restart[1] == "/proj"
    for flag, value in (("--model", "ollama/x"), ("--mode", "agent"),
                        ("--host", "0.0.0.0"), ("--port", "9001"), ("--allow", "push")):
        assert flag in restart and restart[restart.index(flag) + 1] == value
    assert "--auto-push" in restart


def test_gui_restart_args_omit_defaults(home, monkeypatch):
    seen = {}
    monkeypatch.setattr("silkcode.gui.server.run_gui",
                        lambda *a, **k: seen.update(kwargs=k) or 0)
    main(["gui", "."])
    restart = seen["kwargs"]["restart_args"]
    assert "--host" not in restart and "--port" not in restart


# --------------------------------------------------------------------------
# permission grants
# --------------------------------------------------------------------------

def test_allow_rejects_an_unknown_grant(home):
    """A typo in --allow must fail loudly, not silently grant nothing."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--allow", "push,delete-everything"])
    assert "delete-everything" in str(excinfo.value)


def test_allow_parses_the_known_grants(home, monkeypatch):
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(kwargs=k) or 0)
    main([".", "--allow", "pull,commit,push,merge"])
    assert set(seen["kwargs"]["grants"]) == {"pull", "commit", "push", "merge"}


def test_repl_defaults_to_the_safest_mode(home, monkeypatch):
    seen = {}
    monkeypatch.setattr("silkcode.cli.repl.run_repl",
                        lambda *a, **k: seen.update(args=a, kwargs=k) or 0)
    main([])
    assert seen["args"][2] == "ask"
    assert seen["kwargs"]["grants"] == []


# --------------------------------------------------------------------------
# test / benchmark / swarm
# --------------------------------------------------------------------------

def test_test_command_detects_and_runs_pytest(home, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (project / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    code, out, _ = run(["test", str(project)], capsys)
    assert code == 0, out
    assert "exit code: 0" in out


def test_test_command_fails_when_the_tests_fail(home, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    (project / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    code, out, _ = run(["test", str(project)], capsys)
    assert code == 1


def test_test_command_finds_tests_in_a_flat_project(home, tmp_path, capsys):
    """A small project keeps test_foo.py beside foo.py, with no tests/
    directory and no pytest marker in pyproject.toml."""
    project = tmp_path / "flat"
    project.mkdir()
    (project / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (project / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    code, out, _ = run(["test", str(project)], capsys)
    assert code == 0, out


def test_test_command_says_so_when_nothing_is_detected(home, tmp_path, capsys):
    project = tmp_path / "empty"
    project.mkdir()
    (project / "notes.txt").write_text("no code here")
    code, _, err = run(["test", str(project)], capsys)
    assert code == 1
    assert "no test framework detected" in err


def test_benchmark_list_shows_the_builtin_tasks(home, capsys):
    code, out, _ = run(["benchmark", "--list"], capsys)
    assert code == 0
    assert out.strip(), "the built-in suite should not be empty"
    from silkcode.benchmark import TASKS
    for task in TASKS:
        assert task.id in out


def test_swarm_rejects_a_missing_workspace(home, tmp_path, capsys):
    code, _, err = run(["swarm", str(tmp_path / "nope")], capsys)
    assert code == 1
    assert "error:" in err


# --------------------------------------------------------------------------
# mcp
# --------------------------------------------------------------------------

def test_mcp_starts_empty(home, capsys):
    code, out, _ = run(["mcp"], capsys)
    assert code == 0
    assert "No MCP servers configured." in out


def test_mcp_add_then_remove(home, capsys):
    code, out, _ = run(["mcp", "add", "fetch", "--command", "uvx mcp-server-fetch",
                        "--env", "TOKEN=abc"], capsys)
    assert code == 0
    servers = json.loads((home / "config.json").read_text())["mcp_servers"]
    assert servers["fetch"]["command"] == "uvx mcp-server-fetch"
    assert servers["fetch"]["env"] == {"TOKEN": "abc"}

    code, out, _ = run(["mcp", "remove", "fetch"], capsys)
    assert code == 0
    assert json.loads((home / "config.json").read_text())["mcp_servers"] == {}


def test_mcp_remove_reports_an_unknown_server(home, capsys):
    code, _, err = run(["mcp", "remove", "ghost"], capsys)
    assert code == 1
    assert "unknown MCP server" in err


# --------------------------------------------------------------------------
# sandbox
# --------------------------------------------------------------------------

def test_sandbox_status_when_none_is_configured(home, capsys):
    code, out, _ = run(["sandbox"], capsys)
    assert code == 0
    assert "No sandbox configured." in out
    assert "silkcode sandbox serve" in out


def test_sandbox_connect_stores_the_url_and_reports_unreachable(home, capsys):
    code, out, _ = run(["sandbox", "connect", "http://127.0.0.1:1",
                        "--token", "s3cret"], capsys)
    assert code == 1, "an unreachable sandbox is a failed status"
    assert "Sandbox configured" in out
    assert "NOT reachable" in out
    assert json.loads((home / "config.json").read_text())["sandbox"]["url"] == "http://127.0.0.1:1"


def test_sandbox_connect_strips_a_trailing_slash(home, capsys):
    run(["sandbox", "connect", "http://127.0.0.1:1/", "--token", "s"], capsys)
    assert json.loads((home / "config.json").read_text())["sandbox"]["url"] == "http://127.0.0.1:1"


def test_sandbox_disconnect_removes_it(home, capsys):
    run(["sandbox", "connect", "http://127.0.0.1:1", "--token", "s"], capsys)
    code, out, _ = run(["sandbox", "disconnect"], capsys)
    assert code == 0
    assert "sandbox" not in json.loads((home / "config.json").read_text())


def test_sandbox_serve_requires_a_token(home):
    """An open sandbox server would execute commands for anyone who finds it."""
    with pytest.raises(SystemExit):
        main(["sandbox", "serve", "--port", "0"])


def test_sandbox_status_reports_a_reachable_server(home, capsys, tmp_path):
    """Start a real sandbox server and connect to it."""
    import threading
    from silkcode.sandbox_server import make_server
    server = make_server(tmp_path / "ws", "s3cret", "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, out, _ = run(["sandbox", "connect", f"http://127.0.0.1:{port}",
                            "--token", "s3cret"], capsys)
        assert code == 0, out
        assert "reachable" in out and "NOT reachable" not in out
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# connect github
# --------------------------------------------------------------------------

def test_connect_github_explains_both_paths_when_unconfigured(home, capsys):
    code, out, _ = run(["connect", "github"], capsys)
    assert code == 1
    assert "Sign in with GitHub" in out
    assert "Personal access token" in out


def test_connect_github_saves_the_token_env(home, capsys, monkeypatch):
    monkeypatch.setenv("MY_GH_TOKEN", "")
    code, out, _ = run(["connect", "github", "--token-env", "MY_GH_TOKEN"], capsys)
    assert "MY_GH_TOKEN" in out
    assert json.loads((home / "config.json").read_text())["github"]["token_env"] == "MY_GH_TOKEN"


def test_connect_github_saves_the_client_id(home, capsys):
    run(["connect", "github", "--client-id", "Iv1.abc123"], capsys)
    assert json.loads((home / "config.json").read_text())["github"]["client_id"] == "Iv1.abc123"


def test_connect_github_reports_a_rejected_token(home, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "bad-token")

    from silkcode.workspace import ToolError
    monkeypatch.setattr("silkcode.github.GitHubClient.whoami",
                        lambda self: (_ for _ in ()).throw(ToolError("401 Unauthorized")))
    code, out, _ = run(["connect", "github"], capsys)
    assert code == 1
    assert "Token check failed" in out
    assert "bad-token" not in out, "a rejected token must not be echoed"


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------

def test_update_outside_a_git_checkout_points_at_pip(home, capsys, monkeypatch):
    monkeypatch.setattr("silkcode.update.git_repo_root", lambda: None)
    code, _, err = run(["update"], capsys)
    assert code == 1
    assert "pip install -U silkcode" in err


# --------------------------------------------------------------------------
# the installed console script
# --------------------------------------------------------------------------

def test_the_module_is_runnable_as_installed(home):
    """`python -m silkcode` is how the console script reaches main()."""
    env = dict(os.environ, SILKCODE_HOME=str(home))
    proc = subprocess.run([sys.executable, "-m", "silkcode", "sessions"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "No saved sessions." in proc.stdout


# --------------------------------------------------------------------------
# test-framework detection on a remote workspace
# --------------------------------------------------------------------------

def test_detection_on_a_remote_workspace_uses_the_file_listing():
    """The remote branch of detect_test_command used an unimported name, so it
    raised NameError for any remote project whose tests live in tests/."""
    from silkcode.remotews import RemoteWorkspace
    from silkcode.tools.testing import detect_test_command

    class FakeRemote(RemoteWorkspace):
        def __init__(self, files):
            self._files = files

        def list_files(self):
            return self._files

        def read_text(self, rel):
            raise AssertionError(f"should not need to read {rel}")

    assert detect_test_command(FakeRemote(["tests/test_app.py", "app.py"])) is not None
    assert detect_test_command(FakeRemote(["test_app.py", "app.py"])) is not None
    assert detect_test_command(FakeRemote(["src/app.py", "README.md"])) is None
    # a path that merely contains "test_" deeper down is not a root test file
    assert detect_test_command(FakeRemote(["vendor/pkg/test_x.py"])) is None
