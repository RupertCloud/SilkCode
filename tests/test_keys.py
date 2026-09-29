"""Key-error management: an auth failure asks for the key, everywhere.

A model call that fails on its API key - missing, rejected, expired - is the
most fixable error Silk Code has, and it used to surface as a raw HTTP status.
Now the provider layer raises a typed AuthError that says exactly which key
and where to set it; the GUI answers it with an input in the conversation
(POST /api/keys stores the key owner-only and rebuilds live sessions), and
the interactive REPL asks via a hidden prompt. Non-interactive runs never
prompt - they get the actionable message.
"""

import json

import httpx
import pytest

from silkcode.providers import build_provider
from silkcode.providers.base import AuthError
from silkcode.workspace import ToolError

CFG = {"type": "openai_compat", "base_url": "http://test/v1", "default_model": "m"}


def provider_with(status=None, cfg=None, api_key="sk-test", handler=None):
    calls = []

    def default_handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "denied"})

    client = httpx.Client(transport=httpx.MockTransport(handler or default_handler))
    provider = build_provider("acme", dict(cfg or CFG), api_key=api_key, client=client)
    return provider, calls


# ---- the provider layer: typed, actionable, never retried ---------------------

@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_an_auth_error_naming_the_fix(status):
    cfg = dict(CFG, api_key_env="ACME_API_KEY")
    provider, calls = provider_with(status, cfg=cfg)
    with pytest.raises(AuthError) as caught:
        provider.chat("m", [{"role": "user", "content": "hi"}])
    assert f"HTTP {status}" in str(caught.value)
    assert "$ACME_API_KEY" in str(caught.value)
    assert caught.value.provider_name == "acme"
    assert len(calls) == 1, "auth failures must not be retried"


def test_a_key_stored_in_config_is_named_as_the_thing_to_fix():
    provider, _ = provider_with(401, cfg=dict(CFG, api_key="sk-old"))
    with pytest.raises(AuthError) as caught:
        provider.chat("m", [{"role": "user", "content": "hi"}])
    assert "config.json" in str(caught.value)


def test_an_empty_configured_key_env_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    cfg = dict(CFG, api_key_env="ACME_API_KEY")
    provider, calls = provider_with(200, cfg=cfg, api_key=None)
    with pytest.raises(AuthError) as caught:
        provider.chat("m", [{"role": "user", "content": "hi"}])
    assert "$ACME_API_KEY" in str(caught.value)
    assert calls == [], "no request should be made with a knowably missing key"
    with pytest.raises(AuthError):
        list(provider.stream("m", [{"role": "user", "content": "hi"}]))


def test_a_keyless_local_endpoint_is_not_treated_as_broken():
    """Ollama and friends need no key: no api_key_env configured means no
    pre-flight refusal - requests go out as before."""
    def ok(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    provider, _ = provider_with(handler=ok, api_key=None)
    assert provider.chat("m", [{"role": "user", "content": "hi"}]).content == "hi"


# ---- the GUI: ask for the key in the conversation ------------------------------

@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat",
                               "base_url": "http://127.0.0.1:1",
                               "default_model": "m"}},
    }))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    from silkcode.gui.server import GuiState
    return GuiState(str(workspace), None, "edit"), home


def test_an_auth_error_broadcasts_key_needed_not_a_bare_error(gui_state):
    state, _home = gui_state
    session = state.get_session()
    session.agent.provider, _ = provider_with(
        401, cfg=dict(CFG, api_key_env="STUB_KEY"))
    queue = state.subscribe()
    state._run_turn(session, "hello")
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    state.unsubscribe(queue)
    needed = [e for e in events if e["type"] == "key_needed"]
    assert needed and needed[0]["provider"] == "acme"
    assert "$STUB_KEY" in needed[0]["message"]
    assert not any(e["type"] == "error" for e in events)


def test_api_keys_stores_owner_only_and_rebuilds_the_session(gui_state):
    state, home = gui_state
    session = state.get_session()
    old_provider = session.agent.provider
    result = state.set_api_key("stub", "sk-fresh", session.id)
    assert result == {"ok": True, "provider": "stub", "sessions_updated": 1}

    saved = json.loads((home / "config.json").read_text())
    assert saved["providers"]["stub"]["api_key"] == "sk-fresh"
    assert (home / "config.json").stat().st_mode & 0o077 == 0, "keys stay owner-only"
    assert session.agent.provider is not old_provider
    assert session.agent.provider.api_key == "sk-fresh"
    notice = next(t for t in session.transcript if t["kind"] == "notice")
    assert "sk-fresh" not in notice["text"], "the key is never echoed"


def test_a_builtin_provider_gets_its_first_config_entry_from_the_key(gui_state):
    """deepseek & co. are builtins with no entry in config.json until their
    first key arrives - attaching one must create the entry, not 404."""
    state, home = gui_state
    result = state.set_api_key("deepseek", "sk-first", None)
    assert result["ok"] is True
    saved = json.loads((home / "config.json").read_text())
    assert saved["providers"]["deepseek"]["api_key"] == "sk-first"


def test_storing_a_key_keeps_a_custom_providers_base_url(gui_state):
    """Regression: environment.set_key used to *replace* the provider's file
    entry with just the key, dropping a user-defined provider's base_url from
    config.json - fine in memory, broken on the next load."""
    state, home = gui_state
    state.set_api_key("stub", "sk-keep", None)
    saved = json.loads((home / "config.json").read_text())["providers"]["stub"]
    assert saved["api_key"] == "sk-keep"
    assert saved["base_url"] == "http://127.0.0.1:1", "base_url must survive"


def test_api_keys_refuses_junk(gui_state):
    state, _home = gui_state
    with pytest.raises(ToolError):
        state.set_api_key("stub", "   ", None)
    with pytest.raises(ToolError):
        state.set_api_key("stub", "not a key", None)
    with pytest.raises(ToolError):
        state.set_api_key("nonexistent", "sk-x", None)


def test_the_page_knows_how_to_ask_for_a_key():
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_text()
    assert "key_needed" in app
    assert "/api/keys" in app
    assert 'input.type = "password"' in app, "the key input must not echo"


# ---- the REPL: ask, but only a human ------------------------------------------

def test_the_repl_prompt_saves_and_rebuilds(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path))
    from silkcode.cli.repl import _offer_key_fix
    from silkcode.config import Config

    (tmp_path / "config.json").write_text(json.dumps({
        "default_model": "acme",
        "providers": {"acme": dict(CFG)},
    }))
    config = Config.load()

    class FakeAgent:
        provider = None

    import getpass
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "sk-typed")
    agent = FakeAgent()
    assert _offer_key_fix(config, agent, "acme") is True
    assert json.loads((tmp_path / "config.json").read_text())["providers"]["acme"]["api_key"] == "sk-typed"
    assert agent.provider.api_key == "sk-typed"


def test_the_repl_never_prompts_without_a_tty(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKCODE_HOME", str(tmp_path))
    from silkcode.cli.repl import _offer_key_fix
    from silkcode.config import Config
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: False)})())
    import getpass
    monkeypatch.setattr(getpass, "getpass",
                        lambda prompt: pytest.fail("prompted without a tty"))
    assert _offer_key_fix(Config(), None, "acme") is False
