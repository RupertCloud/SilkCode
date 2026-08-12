"""Provider and model configuration (SRS sections 17-20).

Config lives at ~/.silkcode/config.json (override with SILKCODE_HOME).
Built-in providers cover the major OpenAI-compatible clouds and common
local servers; users add or override providers in the config file or with
`silkcode models add`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("SILKCODE_HOME", str(Path.home() / ".silkcode")))


def config_path() -> Path:
    return config_dir() / "config.json"


BUILTIN_PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "type": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "type": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen3-coder-plus",
    },
    "kimi": {
        "type": "openai_compat",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "default_model": "kimi-k2-0711-preview",
    },
    "openrouter": {
        "type": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": None,
    },
    "ollama": {
        "type": "ollama",
        "base_url": "http://localhost:11434",
        "default_model": None,
    },
    "vllm": {
        "type": "openai_compat",
        "base_url": "http://localhost:8000/v1",
        "default_model": None,
    },
    "lmstudio": {
        "type": "openai_compat",
        "base_url": "http://localhost:1234/v1",
        "default_model": None,
    },
}


class ConfigError(ValueError):
    pass


class Config:
    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.path = path or config_path()
        self.data = data if data is not None else {}
        providers: dict[str, dict] = {name: dict(cfg) for name, cfg in BUILTIN_PROVIDERS.items()}
        for name, cfg in (self.data.get("providers") or {}).items():
            merged = dict(providers.get(name, {}))
            merged.update(cfg)
            providers[name] = merged
        self.providers = providers

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError as exc:
                raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
        else:
            data = {}
        return cls(data, path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")

    @property
    def default_model(self) -> str:
        return self.data.get("default_model") or "deepseek"

    def set_provider(self, name: str, cfg: dict) -> None:
        self.data.setdefault("providers", {})[name] = cfg
        merged = dict(self.providers.get(name, {}))
        merged.update(cfg)
        self.providers[name] = merged

    def resolve_model(self, spec: str | None = None) -> tuple[str, dict, str]:
        """Resolve 'deepseek', 'ollama/qwen2.5-coder', or 'auto' to (provider, cfg, model)."""
        spec = spec or self.default_model
        if spec == "auto":
            return self._resolve_auto()
        if "/" in spec:
            prefix, rest = spec.split("/", 1)
            if prefix in self.providers:
                return prefix, self.providers[prefix], rest
        if spec in self.providers:
            cfg = self.providers[spec]
            model = cfg.get("default_model")
            if not model:
                raise ConfigError(
                    f"Provider '{spec}' has no default model; use '{spec}/<model-name>' "
                    f"(e.g. '{spec}/qwen2.5-coder')."
                )
            return spec, cfg, model
        raise ConfigError(
            f"Unknown model '{spec}'. Known providers: {', '.join(sorted(self.providers))}. "
            "Use '<provider>' or '<provider>/<model>'."
        )

    def _resolve_auto(self) -> tuple[str, dict, str]:
        """Pick the first available model (SRS section 17, basic router):
        local servers that are actually running, then cloud providers with a
        configured API key. Order is configurable via 'auto_order'."""
        from .providers.ollama import OllamaProvider
        from .providers.openai_compat import OpenAICompatProvider

        order = self.data.get("auto_order") or [
            "ollama", "deepseek", "qwen", "kimi", "openrouter", "vllm", "lmstudio",
        ]
        for name in order:
            cfg = self.providers.get(name)
            if not cfg:
                continue
            if cfg.get("type") == "ollama":
                probe = OllamaProvider(name, base_url=cfg["base_url"], timeout=2.0)
                models = probe.list_models()
                if models:
                    model = next((m for m in models if "coder" in m), models[0])
                    return name, cfg, model
            elif cfg.get("api_key_env") or cfg.get("api_key"):
                model = cfg.get("default_model")
                if model and self.api_key_for(cfg):
                    return name, cfg, model
            else:
                probe = OpenAICompatProvider(name, cfg["base_url"], timeout=2.0)
                models = probe.list_models()
                if models:
                    return name, cfg, cfg.get("default_model") or models[0]
        raise ConfigError(
            "auto: no available model found. Set an API key (e.g. DEEPSEEK_API_KEY), "
            "start a local server (Ollama/vLLM/LM Studio), or pass --model explicitly."
        )

    def api_key_for(self, cfg: dict) -> str | None:
        if cfg.get("api_key"):
            return cfg["api_key"]
        env = cfg.get("api_key_env")
        if env:
            return os.environ.get(env)
        return None
