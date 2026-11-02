import json
import os
import stat

import pytest

from silkcode.config import Config, ConfigError


def test_builtin_provider_resolution():
    config = Config({})
    name, cfg, model = config.resolve_model("deepseek")
    assert name == "deepseek"
    assert model == "deepseek-chat"
    assert cfg["base_url"].startswith("https://api.deepseek.com")


def test_provider_slash_model():
    config = Config({})
    name, _cfg, model = config.resolve_model("ollama/qwen2.5-coder:7b")
    assert name == "ollama"
    assert model == "qwen2.5-coder:7b"


def test_unknown_model_errors():
    with pytest.raises(ConfigError):
        Config({}).resolve_model("notaprovider")


def test_provider_without_default_model_errors():
    with pytest.raises(ConfigError):
        Config({}).resolve_model("vllm")


def test_user_override_merges_with_builtin():
    config = Config({"providers": {"deepseek": {"default_model": "deepseek-reasoner"}}})
    _name, cfg, model = config.resolve_model("deepseek")
    assert model == "deepseek-reasoner"
    assert cfg["base_url"].startswith("https://api.deepseek.com")  # kept from builtin


def test_custom_provider(tmp_path):
    config = Config({}, path=tmp_path / "config.json")
    config.set_provider("myserver", {"type": "openai_compat", "base_url": "https://ai.example.com/v1", "default_model": "m1"})
    config.save()

    reloaded = Config.load(tmp_path / "config.json")
    name, cfg, model = reloaded.resolve_model("myserver")
    assert (name, model) == ("myserver", "m1")
    assert cfg["base_url"] == "https://ai.example.com/v1"


def test_api_key_from_env(monkeypatch):
    config = Config({})
    _name, cfg, _model = config.resolve_model("deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert config.api_key_for(cfg) is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert config.api_key_for(cfg) == "sk-test"


def test_cloudflare_requires_account_id():
    with pytest.raises(ConfigError, match="account id"):
        Config({}).resolve_model("cloudflare")


def test_cloudflare_with_account_id_resolves_url(monkeypatch):
    config = Config({"providers": {"cloudflare": {"account_id": "abc123"}}})
    name, cfg, model = config.resolve_model("cloudflare")
    assert name == "cloudflare"
    assert cfg["base_url"] == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1"
    assert cfg["api_key_env"] == "CLOUDFLARE_API_TOKEN"
    assert model == "@cf/qwen/qwen2.5-coder-32b-instruct"

    # explicit model spec keeps the @cf/... path intact
    _n, _c, model = config.resolve_model("cloudflare/@cf/meta/llama-3.3-70b-instruct")
    assert model == "@cf/meta/llama-3.3-70b-instruct"


def test_auto_skips_cloudflare_without_account_id(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    config = Config({"auto_order": ["cloudflare", "deepseek"]})
    name, _cfg, _model = config.resolve_model("auto")
    assert name == "deepseek"


def test_auto_uses_cloudflare_when_onboarded(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    config = Config({
        "auto_order": ["cloudflare"],
        "providers": {"cloudflare": {"account_id": "abc123"}},
    })
    name, cfg, model = config.resolve_model("auto")
    assert name == "cloudflare"
    assert "abc123" in cfg["base_url"]
    assert model == "@cf/qwen/qwen2.5-coder-32b-instruct"


def test_default_model_setting():
    assert Config({}).default_model == "deepseek"
    assert Config({"default_model": "ollama/qwen2.5-coder"}).default_model == "ollama/qwen2.5-coder"


# ---- atomic save (LOW-11) ----------------------------------------------------

def test_save_keeps_the_old_config_when_the_swap_fails(tmp_path, monkeypatch):
    """A crash or ENOSPC while saving must not lose every provider and key."""
    path = tmp_path / "config.json"
    config = Config({"providers": {"deepseek": {"api_key": "sk-old"}}}, path=path)
    config.save()

    config.set_provider("myserver", {
        "type": "openai_compat", "base_url": "https://ai.example.com/v1",
        "default_model": "m1", "api_key": "sk-new",
    })

    def no_space(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", no_space)
    with pytest.raises(OSError, match="No space left"):
        config.save()
    monkeypatch.undo()

    on_disk = json.loads(path.read_text())  # still valid JSON, still the old one
    assert "myserver" not in on_disk["providers"]
    assert on_disk["providers"]["deepseek"]["api_key"] == "sk-old"
    # the temp file was cleaned up, not left next to the config
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_save_keeps_the_old_config_when_the_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    config = Config({"providers": {"deepseek": {"api_key": "sk-old"}}}, path=path)
    config.save()

    config.set_provider("myserver", {"type": "openai_compat",
                                     "base_url": "https://ai.example.com/v1"})

    def no_space(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_space)  # fails before the swap
    with pytest.raises(OSError, match="No space left"):
        config.save()
    monkeypatch.undo()

    assert json.loads(path.read_text())["providers"]["deepseek"]["api_key"] == "sk-old"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_save_writes_keys_only_to_an_owner_only_temp_file(tmp_path, monkeypatch):
    """A key must never land in a world-readable file, even briefly."""
    path = tmp_path / "config.json"
    config = Config({"providers": {"deepseek": {"api_key": "sk-secret"}}}, path=path)
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    config.save()
    assert seen["mode"] & (stat.S_IRGRP | stat.S_IROTH) == 0, oct(seen["mode"])
    assert stat.S_IMODE(os.stat(path).st_mode) & (stat.S_IRGRP | stat.S_IROTH) == 0
