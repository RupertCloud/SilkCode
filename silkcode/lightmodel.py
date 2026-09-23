"""A cheap model for cheap work.

Compaction checkpoints are the first user: summarizing a trimmed
conversation does not need the session's main model, and on a paid provider
it should not use it. Configure a spec and the checkpoint runs there:

    "light_model": "ollama/qwen2.5-coder"      in ~/.silkcode/config.json

Nothing is configured by default, and nothing falls back to the main model
implicitly - an unconfigured light model means compaction stays what it was
(mechanical trimming), because silently spending main-model tokens on
summaries is exactly the surprise this setting exists to prevent.

The checkpoint prompt is nac's discipline, kept because both of its rules
close real failure modes:

- constraints are marked active/satisfied/superseded, because a summary
  that keeps every constraint forever makes the agent obey orders the user
  already withdrew
- work is labeled "reported" unless verification evidence appears in the
  transcript, because a summary that upgrades an agent's claim into a fact
  is how "the tests pass" survives three compactions without anyone having
  run the tests
"""

from __future__ import annotations

from typing import Callable

CHECKPOINT_INPUT_CHARS = 24_000     # what the light model reads
CHECKPOINT_OUTPUT_CHARS = 4_000     # what the conversation keeps

CHECKPOINT_PROMPT = """Summarize the conversation excerpt into one standalone historical checkpoint. Do not continue the task. Output only the checkpoint, in these sections, omitting empty ones:

## User intent and constraints
The user's goals, constraints, prohibitions, and decisions - each marked active, satisfied, or superseded, as the excerpt supports.

## Decisions
Approaches chosen or rejected and why. Distinguish the user's decisions from the assistant's suggestions.

## Work state
What was done and what was verified. Label any outcome without verification evidence in the excerpt as "reported, not verified" - do not upgrade a claim into a fact. Keep exact file paths, commands that worked, the current blocker, and the next step."""


def light_spec(config) -> str | None:
    spec = config.data.get("light_model")
    return str(spec) if spec else None


def checkpoint_summarizer(config) -> Callable[[str], str] | None:
    """A transcript -> checkpoint function on the configured light model,
    or None when none is configured. The provider is built on first use and
    reused; a summarizer that cannot be built is reported by failing its
    first call, which the caller treats as 'no checkpoint this time'."""
    spec = light_spec(config)
    if not spec:
        return None

    from .providers import build_provider

    cache: dict = {}

    def summarize(transcript: str) -> str:
        if "provider" not in cache:
            name, cfg, model = config.resolve_model(spec)
            cache["provider"] = build_provider(name, cfg, api_key=config.api_key_for(cfg))
            cache["model"] = model
        # stream(), not chat(): it is the one method every provider serves
        # the same way the agent loop consumes it.
        result = None
        for kind, data in cache["provider"].stream(cache["model"], [
            {"role": "system", "content": CHECKPOINT_PROMPT},
            {"role": "user", "content": transcript[-CHECKPOINT_INPUT_CHARS:]},
        ]):
            if kind == "result":
                result = data
        content = (result.content or "") if result is not None else ""
        return content.strip()[:CHECKPOINT_OUTPUT_CHARS]

    return summarize
