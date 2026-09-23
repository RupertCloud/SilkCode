"""Provider registry.

The concrete providers are imported when one is actually built, not when
this package is. They pull in httpx, which costs most of the interpreter's
startup, and the commands people run most often - `sessions`, `config`,
`env`, `--help` - never talk to a model at all.
"""

from .base import ChatResult, ModelProvider, ProviderError, ToolCall, Usage

# type -> "module:class", resolved on first use
PROVIDER_TYPES = {
    "openai_compat": "openai_compat:OpenAICompatProvider",
    "ollama": "ollama:OllamaProvider",
}


def _load(spec: str):
    from importlib import import_module
    module_name, _, class_name = spec.partition(":")
    return getattr(import_module(f".{module_name}", __package__), class_name)


def __getattr__(name: str):
    """Keep `from silkcode.providers import OllamaProvider` working, without
    importing it for callers that never mention it (PEP 562)."""
    for spec in PROVIDER_TYPES.values():
        if spec.endswith(f":{name}"):
            return _load(spec)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def build_provider(name: str, cfg: dict, api_key: str | None = None, client=None) -> ModelProvider:
    ptype = cfg.get("type", "openai_compat")
    spec = PROVIDER_TYPES.get(ptype)
    if spec is None:
        raise ProviderError(f"Unknown provider type '{ptype}' for provider '{name}'")
    cls = _load(spec)
    kwargs = {
        "name": name,
        "base_url": cfg["base_url"],
        "default_model": cfg.get("default_model"),
        "api_key": api_key,
    }
    try:
        kwargs["timeout"] = float(cfg.get("timeout", 180.0))
    except (TypeError, ValueError):
        raise ProviderError(
            f"Provider '{name}' has an invalid 'timeout' value: {cfg.get('timeout')!r}"
        )
    try:
        kwargs["retries"] = int(cfg.get("retries", 2))
        kwargs["retry_delay"] = float(cfg.get("retry_delay", 1.0))
    except (TypeError, ValueError):
        raise ProviderError(
            f"Provider '{name}' has invalid 'retries'/'retry_delay' values"
        )
    if client is not None:
        kwargs["client"] = client
    return cls(**kwargs)


__all__ = [
    "ChatResult",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatProvider",
    "ProviderError",
    "ToolCall",
    "Usage",
    "build_provider",
]
