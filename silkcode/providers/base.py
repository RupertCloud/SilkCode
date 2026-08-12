"""Model provider abstraction (SRS section 18)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator


class ProviderError(RuntimeError):
    """Raised when a provider request fails."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as sent by the model


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "Usage | None") -> None:
        if other is not None:
            self.prompt_tokens += other.prompt_tokens
            self.completion_tokens += other.completion_tokens


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None


# ("text", str) chunks while streaming, then a final ("result", ChatResult).
StreamEvent = tuple[str, Any]


class ModelProvider(ABC):
    """A connection to one model provider, cloud or local."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        ...

    def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> Iterator[StreamEvent]:
        result = self.chat(model, messages, tools)
        if result.content:
            yield ("text", result.content)
        yield ("result", result)

    @abstractmethod
    def list_models(self) -> list[str]:
        ...

    def capabilities(self) -> dict:
        return {"tools": True, "streaming": True}
