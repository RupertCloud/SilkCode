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


def test_default_model_setting():
    assert Config({}).default_model == "deepseek"
    assert Config({"default_model": "ollama/qwen2.5-coder"}).default_model == "ollama/qwen2.5-coder"
