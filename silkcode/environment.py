"""Environment overview: credentials, token usage, and live sessions.

One place to answer "what is this install holding, spending, and running?"
(SRS section 19 provider configuration, section 48 usage dashboard).

Credentials are never returned in full: a caller sees whether a key is set,
where it came from, and its last four characters — enough to tell two keys
apart, useless to anyone reading over a shoulder.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .config import Config, valid_env_name
from .sessions import SessionStore

MASK_TAIL = 4


def mask(secret: str | None) -> str:
    """'…a1b2' — enough to identify a key, not to use it."""
    if not secret:
        return ""
    tail = secret[-MASK_TAIL:] if len(secret) > MASK_TAIL else secret
    return "…" + tail


def credentials(config: Config) -> list[dict]:
    """Every configured provider and the state of its credential."""
    out: list[dict] = []
    for name in sorted(config.providers):
        cfg = config.providers[name]
        raw_env_var = cfg.get("env") or cfg.get("api_key_env")
        env_var = valid_env_name(raw_env_var)
        from_env = os.environ.get(env_var) if env_var else None
        stored = cfg.get("api_key")
        key = from_env or stored
        needs_key = bool(env_var or stored)
        out.append({
            "provider": name,
            "type": cfg.get("type", "openai_compat"),
            "base_url": cfg.get("base_url", ""),
            "default_model": cfg.get("default_model"),
            "env_var": env_var,
            "invalid_env_var": bool(raw_env_var and not env_var),
            "needs_key": needs_key,
            "set": bool(key),
            # where the value in use came from, so a stale env var that shadows
            # a stored key is visible rather than mysterious
            "source": "environment" if from_env else ("config" if stored else None),
            "shadowed": bool(from_env and stored),
            "masked": mask(key),
            "local": cfg.get("type") == "ollama" or "localhost" in cfg.get("base_url", ""),
        })
    return out


def set_key(config: Config, provider: str, key: str) -> dict:
    """Store a provider key in the config file (the GUI's key field)."""
    key = (key or "").strip()
    if provider not in config.providers:
        raise ValueError(f"unknown provider '{provider}'")
    if not key:
        raise ValueError("empty key")
    config.set_provider(provider, {"api_key": key})
    config.save()
    return {"provider": provider, "set": True, "masked": mask(key)}


def clear_key(config: Config, provider: str) -> dict:
    """Remove a stored key. A key coming from the environment is untouched —
    the caller is told so rather than being left wondering why it is still set."""
    if provider not in config.providers:
        raise ValueError(f"unknown provider '{provider}'")
    stored = (config.data.get("providers") or {}).get(provider, {})
    stored.pop("api_key", None)
    config.providers[provider].pop("api_key", None)
    config.save()
    cfg = config.providers[provider]
    env_var = valid_env_name(cfg.get("env") or cfg.get("api_key_env"))
    still_set = bool(env_var and os.environ.get(env_var))
    return {"provider": provider, "set": still_set,
            "note": f"still set from ${env_var}" if still_set else ""}


def _model_of(session: dict) -> str:
    return session.get("model") or "(unknown)"


def usage(store: SessionStore | None = None, days: int = 7) -> dict:
    """Token usage across every saved session, by model and by day.

    Sessions are the ledger: each records the tokens its conversation spent,
    so this covers the CLI, the GUI, and the swarm alike.
    """
    store = store or SessionStore()
    by_model: dict[str, dict] = {}
    total_prompt = total_completion = 0
    day_totals: dict[str, int] = {}
    now = time.time()
    window = now - days * 86400
    counted = 0

    for summary in store.list():
        data = summary
        session_usage = data.get("usage") or {}
        prompt = int(session_usage.get("prompt_tokens") or 0)
        completion = int(session_usage.get("completion_tokens") or 0)
        if prompt or completion:
            counted += 1
        model = _model_of(data)
        entry = by_model.setdefault(model, {"model": model, "prompt_tokens": 0,
                                            "completion_tokens": 0, "sessions": 0})
        entry["prompt_tokens"] += prompt
        entry["completion_tokens"] += completion
        entry["sessions"] += 1
        total_prompt += prompt
        total_completion += completion
        updated = float(data.get("updated") or 0)
        if updated >= window and (prompt or completion):
            day = datetime.fromtimestamp(updated, timezone.utc).strftime("%Y-%m-%d")
            day_totals[day] = day_totals.get(day, 0) + prompt + completion

    models = sorted(by_model.values(),
                    key=lambda e: e["prompt_tokens"] + e["completion_tokens"],
                    reverse=True)
    for entry in models:
        entry["total_tokens"] = entry["prompt_tokens"] + entry["completion_tokens"]
    return {
        "by_model": models,
        "by_day": [{"day": d, "total_tokens": t} for d, t in sorted(day_totals.items())],
        "totals": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "sessions": len(store.list()),
            "sessions_with_usage": counted,
        },
    }


def overview(config: Config | None = None, store: SessionStore | None = None,
             live_sessions: list[dict] | None = None) -> dict:
    """Everything the environment page shows."""
    config = config or Config.load()
    return {
        "credentials": credentials(config),
        "usage": usage(store),
        "live": live_sessions if live_sessions is not None else [],
        "default_model": config.default_model,
        "config_path": str(config.path),
    }
