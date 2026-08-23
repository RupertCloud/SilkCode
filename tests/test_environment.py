"""Environment overview: credentials, token usage, live sessions."""

import json

import pytest

from silkcode.config import Config
from silkcode.environment import (
    clear_key,
    credentials,
    mask,
    overview,
    set_key,
    usage,
)
from silkcode.sessions import SessionStore, new_session


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(d))
    for var in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY",
                "GLM_API_KEY", "MINIMAX_API_KEY", "OPENROUTER_API_KEY",
                "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return d


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def test_mask_never_reveals_the_key():
    assert mask("sk-abcdef123456") == "…3456"
    assert mask("") == ""
    assert mask(None) == ""
    assert "abcdef" not in mask("sk-abcdef123456")


def test_credentials_report_state_and_source(home, monkeypatch):
    config = Config.load()
    rows = {c["provider"]: c for c in credentials(config)}

    assert rows["deepseek"]["set"] is False
    assert rows["deepseek"]["needs_key"] is True
    assert rows["deepseek"]["env_var"] == "DEEPSEEK_API_KEY"
    assert rows["deepseek"]["source"] is None
    # local providers need no key at all
    assert rows["ollama"]["needs_key"] is False
    assert rows["ollama"]["local"] is True

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env-9999")
    row = {c["provider"]: c for c in credentials(Config.load())}["deepseek"]
    assert row["set"] is True
    assert row["source"] == "environment"
    assert row["masked"] == "…9999"
    assert "from-env" not in json.dumps(row)  # the key itself never appears


def test_environment_key_shadows_stored_key(home, monkeypatch):
    config = Config.load()
    set_key(config, "deepseek", "sk-stored-1111")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-2222")

    row = {c["provider"]: c for c in credentials(Config.load())}["deepseek"]
    assert row["source"] == "environment"
    assert row["masked"] == "…2222"
    assert row["shadowed"] is True  # the stored key is present but not in use


def test_malformed_environment_variable_never_leaks_through_overview(home):
    config = Config.load()
    # A historical GUI bug could put the credential itself in api_key_env.
    secret = "sk-secret-was-put-in-the-wrong-field-1234"
    config.providers["broken"] = {
        "type": "openai_compat", "base_url": "https://example.test/v1",
        "api_key_env": secret, "api_key": "stored-safe-copy-5678",
    }

    row = {c["provider"]: c for c in credentials(config)}["broken"]

    assert row["env_var"] is None
    assert row["invalid_env_var"] is True
    assert secret not in json.dumps(row)
    assert row["masked"] == "…5678"


def test_provider_rejects_a_secret_where_an_environment_name_is_expected(home):
    with pytest.raises(Exception, match="environment variable"):
        Config.load().set_provider("bad", {"api_key_env": "sk-not-a-variable"})


def test_set_and_clear_key(home, monkeypatch):
    config = Config.load()
    result = set_key(config, "deepseek", "sk-abc-7777")
    assert result["masked"] == "…7777"
    assert Config.load().providers["deepseek"]["api_key"] == "sk-abc-7777"

    cleared = clear_key(Config.load(), "deepseek")
    assert cleared["set"] is False
    assert "api_key" not in Config.load().providers["deepseek"]

    # clearing a stored key does not (and cannot) unset the environment one
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-3333")
    config = Config.load()
    set_key(config, "deepseek", "sk-stored-4444")
    cleared = clear_key(Config.load(), "deepseek")
    assert cleared["set"] is True
    assert "DEEPSEEK_API_KEY" in cleared["note"]


def test_set_key_rejects_unknown_provider_and_empty_key(home):
    config = Config.load()
    with pytest.raises(ValueError, match="unknown provider"):
        set_key(config, "nope", "sk-1")
    with pytest.raises(ValueError, match="empty key"):
        set_key(config, "deepseek", "   ")


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #

def test_usage_aggregates_by_model(home, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    for i, (model, prompt, completion) in enumerate([
        ("deepseek/deepseek-chat", 1000, 200),
        ("deepseek/deepseek-chat", 500, 100),
        ("ollama/qwen2.5-coder", 300, 50),
    ], start=1):
        session = new_session(i, f"task {i}", model, "/repo", "ask")
        session["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
        store.save(session)

    stats = usage(store)
    by_model = {m["model"]: m for m in stats["by_model"]}
    assert by_model["deepseek/deepseek-chat"]["total_tokens"] == 1800
    assert by_model["deepseek/deepseek-chat"]["sessions"] == 2
    assert by_model["ollama/qwen2.5-coder"]["total_tokens"] == 350
    # busiest model first
    assert stats["by_model"][0]["model"] == "deepseek/deepseek-chat"

    totals = stats["totals"]
    assert totals["total_tokens"] == 2150
    assert totals["prompt_tokens"] == 1800
    assert totals["sessions"] == 3
    assert totals["sessions_with_usage"] == 3
    assert stats["by_day"], "recent sessions should appear in the daily breakdown"


def test_usage_handles_empty_and_unused_sessions(home, tmp_path):
    store = SessionStore(tmp_path / "sessions")
    assert usage(store)["totals"]["total_tokens"] == 0

    store.save(new_session(1, "never ran", "deepseek", "/repo", "ask"))
    stats = usage(store)
    assert stats["totals"]["sessions"] == 1
    assert stats["totals"]["sessions_with_usage"] == 0
    assert stats["totals"]["total_tokens"] == 0


def test_overview_shape(home, tmp_path):
    data = overview(Config.load(), SessionStore(tmp_path / "sessions"),
                    live_sessions=[{"id": 1, "running": True}])
    assert {"credentials", "usage", "live", "default_model", "config_path"} <= set(data)
    assert data["live"][0]["running"] is True
    assert data["config_path"].endswith("config.json")
