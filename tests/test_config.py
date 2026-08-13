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
