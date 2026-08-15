from .base import ChatResult, ModelProvider, ProviderError, ToolCall, Usage
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatProvider

PROVIDER_TYPES = {
    "openai_compat": OpenAICompatProvider,
    "ollama": OllamaProvider,
}


def build_provider(name: str, cfg: dict, api_key: str | None = None, client=None) -> ModelProvider:
    ptype = cfg.get("type", "openai_compat")
    cls = PROVIDER_TYPES.get(ptype)
    if cls is None:
        raise ProviderError(f"Unknown provider type '{ptype}' for provider '{name}'")
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
