"""Provider for OpenAI-compatible chat-completions endpoints.

Covers DeepSeek, Qwen (DashScope compatible mode), Kimi/Moonshot, OpenRouter,
vLLM, LM Studio, and custom enterprise endpoints.
"""

from __future__ import annotations

import json
import time
from typing import Iterator

import httpx

from .base import ChatResult, ModelProvider, ProviderError, StreamEvent, ToolCall, Usage

# Network errors that are safe to retry. A timeout (ReadTimeout, ConnectTimeout)
# or a dropped connection (ConnectError, ReadError, RemoteProtocolError) means
# the request never produced an authoritative answer — retrying it is harmless.
# HTTP status errors (4xx/5xx) are handled separately in _should_retry_status.
_TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


class OpenAICompatProvider(ModelProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 180.0,
        retries: int = 2,
        retry_delay: float = 1.0,
        client: httpx.Client | None = None,
    ):
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.retries = max(0, int(retries))
        self.retry_delay = float(retry_delay)
        # follow_redirects stays off, explicitly: this client carries the API
        # key and the full conversation in every POST body, and following a
        # redirect replays both to whatever address the response names. A
        # provider that "moves" mid-request is an error to show the user,
        # not a destination.
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def _sleep_before_retry(self, attempt: int) -> None:
        # exponential backoff: delay * 2**attempt (attempt is 0-based)
        time.sleep(self.retry_delay * (2 ** attempt))

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        return isinstance(exc, _TRANSIENT)

    @staticmethod
    def _should_retry_status(status: int) -> bool:
        # 429 (rate limited) and 5xx (provider transient failure) are worth a
        # second try; a 4xx is a request problem and retrying won't help.
        return status == 429 or status >= 500

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, model: str, messages: list[dict], tools: list[dict] | None, stream: bool = False) -> dict:
        payload: dict = {"model": model, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        return payload

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=self._payload(model, messages, tools),
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if self._is_transient_error(exc) and attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: request failed: {exc}") from exc
            if resp.status_code >= 400:
                last_error = None
                if self._should_retry_status(resp.status_code) and attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError as exc:
                # non-JSON body is a transient provider-side glitch worth one retry
                last_error = exc
                if attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: non-JSON response: {resp.text[:500]}") from exc
            return self._parse_response(data)
        # only reached when retries exhausted without returning; keep a clear error
        raise ProviderError(f"{self.name}: request failed: {last_error or 'unexpected'}")

    def _parse_response(self, data: dict) -> ChatResult:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: unexpected response shape: {json.dumps(data)[:500]}") from exc
        tool_calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments") or "{}",
            )
            for i, tc in enumerate(message.get("tool_calls") or [])
        ]
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            usage=self._parse_usage(data.get("usage")),
            finish_reason=choice.get("finish_reason"),
        )

    @staticmethod
    def _parse_usage(usage: dict | None) -> Usage | None:
        if not usage:
            return None
        return Usage(
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
        )

    def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> Iterator[StreamEvent]:
        # Retry the *whole* stream only while nothing has been yielded yet. Once
        # the first chunk is out, retrying would duplicate output, so a failure
        # mid-stream surfaces as a ProviderError instead.
        content_parts: list[str] = []
        calls: dict[int, dict] = {}
        usage = None
        finish_reason = None
        started = False
        for attempt in range(self.retries + 1):
            try:
                with self._client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=self._payload(model, messages, tools, stream=True),
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code >= 400:
                        if self._should_retry_status(resp.status_code) and attempt < self.retries and not started:
                            resp.read()  # drain so the connection can be reused
                            self._sleep_before_retry(attempt)
                            continue
                        body = resp.read().decode("utf-8", "replace")
                        raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {body[:500]}")
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("usage"):
                            usage = self._parse_usage(chunk["usage"])
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            started = True
                            content_parts.append(text)
                            yield ("text", text)
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                    break  # stream completed cleanly
            except httpx.HTTPError as exc:
                if not started and attempt < self.retries and self._is_transient_error(exc):
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: stream failed: {exc}") from exc
        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=slot["arguments"] or "{}")
            for idx, slot in sorted(calls.items())
        ]
        yield ("result", ChatResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        ))

    def list_models(self) -> list[str]:
        try:
            resp = self._client.get(f"{self.base_url}/models", headers=self._headers())
            resp.raise_for_status()
            return sorted(m.get("id", "") for m in resp.json().get("data", []) if m.get("id"))
        except (httpx.HTTPError, ValueError):
            return []
