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
        ) from None
    try:
        kwargs["retries"] = int(cfg.get("retries", 2))
        kwargs["retry_delay"] = float(cfg.get("retry_delay", 1.0))
    except (TypeError, ValueError):
        raise ProviderError(
            f"Provider '{name}' has invalid 'retries'/'retry_delay' values"
        ) from None
    if client is not None:
        kwargs["client"] = client
    provider = cls(**kwargs)
    # Where the key comes from, remembered on the provider so an auth failure
    # can say exactly what to fix (see AuthError). Set as attributes rather
    # than constructor kwargs so plugged-in provider classes need not know.
    env = cfg.get("api_key_env")
    if cfg.get("api_key"):
        provider.key_hint = f"The key is the api_key stored in config.json for provider '{name}'."
    elif env and api_key:
        provider.key_hint = f"The key came from ${env}."
    elif env:
        provider.key_hint = f"Set ${env} (it is currently empty), or add the key when asked."
        provider.missing_key_env = env
    else:
        provider.key_hint = (f"No api_key or api_key_env is configured for provider "
                             f"'{name}'; add one, or paste the key when asked.")
    return provider


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
