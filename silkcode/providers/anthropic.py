"""Provider for Anthropic's Messages API.

Kept deliberately symmetrical with `openai_compat.py`: raw httpx, the same
retry rules, the same injectable client the tests drive with a mock transport.
The official SDK would hand us streaming and tool-call reassembly for free, but
it would also be the only provider here that needs a dependency and the only
one the test suite cannot drive the same way, so the reassembly is done below
and tested instead.

Silkcode's agent loop keeps its conversation in OpenAI's shapes - a leading
`system` message, `tool_calls` on the assistant turn, one `{"role": "tool"}`
message per result. The Messages API wants none of those, so most of this file
is translation:

  system message      -> top-level `system` parameter
  assistant.tool_calls-> `tool_use` content blocks (input is an object, not a
                         JSON string)
  role "tool"         -> a *user* message of `tool_result` blocks
  tool schemas        -> {name, description, input_schema}

The one that fails silently is the third. Anthropic wants every `tool_result`
for a turn in a single user message; the loop appends one message per call, so
consecutive tool messages are coalesced here. Splitting them is accepted by the
API and then quietly teaches the model to stop making parallel calls, which
looks like a model regression rather than a bug in this file.

Not handled yet: `thinking` blocks. Models that think by default return them,
and this adapter drops them rather than echoing them back, which costs some
reasoning continuity across turns. Text and tool use are unaffected. Adding
them means giving Silkcode's message format somewhere to keep an opaque block.
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import ChatResult, ModelProvider, ProviderError, StreamEvent, ToolCall, Usage

# Sent on every request. The Messages API requires it and is explicit that an
# unknown version is an error rather than a default.
API_VERSION = "2023-06-01"

# The Messages API requires max_tokens; there is no "as much as you need".
# A coding turn writing a file can be long, so this is generous rather than
# safe-looking - a truncated tool call is a broken turn.
DEFAULT_MAX_TOKENS = 8192

_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class AnthropicProvider(ModelProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 180.0,
        retries: int = 2,
        retry_delay: float = 1.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: httpx.Client | None = None,
    ):
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.max_tokens = int(max_tokens)
        # follow_redirects=False deliberately, and checked by
        # tests/test_redirects.py: this client carries the API key and the
        # whole conversation in every POST body, so a 307 must surface as an
        # error rather than replay both to an address the responding server
        # chose.
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    # ---- plumbing shared with the OpenAI adapter ---------------------------

    def _sleep_before_retry(self, attempt: int) -> None:
        import time
        time.sleep(self.retry_delay * (2 ** attempt))

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        return isinstance(exc, _TRANSIENT)

    @staticmethod
    def _should_retry_status(status: int) -> bool:
        return status == 429 or status >= 500

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": API_VERSION,
        }
        if self.api_key:
            # Note: x-api-key, not an Authorization bearer.
            headers["x-api-key"] = self.api_key
        return headers

    # ---- translation --------------------------------------------------------

    @staticmethod
    def _tools(tools: list[dict] | None) -> list[dict] | None:
        """OpenAI function schemas -> Messages API tool definitions."""
        if not tools:
            return None
        converted = []
        for tool in tools:
            fn = tool.get("function") or tool
            converted.append({
                "name": fn["name"],
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return converted

    @classmethod
    def _split_messages(cls, messages: list[dict]) -> tuple[str, list[dict]]:
        """Return (system prompt, Messages-API messages).

        System turns leave the message list entirely; several are joined, which
        keeps a caller that appends a second one from silently losing it.
        """
        system_parts: list[str] = []
        out: list[dict] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                if message.get("content"):
                    system_parts.append(str(message["content"]))
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id") or "",
                    "content": str(message.get("content") or ""),
                }
                # Coalesce with the previous turn when it is already a
                # tool_result user message - see the module docstring.
                if out and out[-1]["role"] == "user" and cls._is_tool_result_turn(out[-1]):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif role == "assistant":
                blocks: list[dict] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": str(message["content"])})
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": fn.get("name") or "",
                        # Silkcode carries arguments as a JSON string; the API
                        # wants the object. A malformed string becomes {} so a
                        # bad turn degrades instead of raising here.
                        "input": cls._loads_or_empty(fn.get("arguments")),
                    })
                # An assistant turn with neither text nor tool calls is not a
                # valid message; drop it rather than send an empty content list.
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "user":
                out.append({"role": "user",
                            "content": [{"type": "text", "text": str(message.get("content") or "")}]})
        return "\n".join(system_parts), out

    @staticmethod
    def _is_tool_result_turn(message: dict) -> bool:
        content = message.get("content")
        return (isinstance(content, list) and bool(content)
                and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))

    @staticmethod
    def _loads_or_empty(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _payload(self, model: str, messages: list[dict], tools: list[dict] | None,
                 stream: bool = False) -> dict:
        system, converted = self._split_messages(messages)
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": converted,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        tool_defs = self._tools(tools)
        if tool_defs:
            payload["tools"] = tool_defs
        return payload

    @staticmethod
    def _parse_usage(usage: dict | None) -> Usage | None:
        if not usage:
            return None
        return Usage(
            prompt_tokens=usage.get("input_tokens") or 0,
            completion_tokens=usage.get("output_tokens") or 0,
            cache_write_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
        )

    def _parse_response(self, data: dict) -> ChatResult:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(
                f"{self.name}: unexpected response shape: {json.dumps(data)[:500]}")
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id") or f"call_{index}",
                    name=block.get("name") or "",
                    # Back to a JSON string, which is what the loop stores.
                    arguments=json.dumps(block.get("input") or {}),
                ))
        return ChatResult(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=self._parse_usage(data.get("usage")),
            finish_reason=data.get("stop_reason"),
        )

    # ---- requests -----------------------------------------------------------

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/v1/messages",
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
                last_error = exc
                if attempt < self.retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: non-JSON response: {resp.text[:500]}") from exc
            return self._parse_response(data)
        raise ProviderError(f"{self.name}: request failed: {last_error or 'unexpected'}")

    def stream(self, model: str, messages: list[dict],
               tools: list[dict] | None = None) -> Iterator[StreamEvent]:
        """Stream a turn, reassembling tool arguments as they arrive.

        Tool input streams as `input_json_delta` fragments that are only valid
        JSON once concatenated, so partial fragments are accumulated per content
        block index and parsed at the end - never matched on as strings.
        """
        text_parts: list[str] = []
        blocks: dict[int, dict] = {}
        usage = Usage()
        stop_reason = None
        started = False

        for attempt in range(self.retries + 1):
            try:
                with self._client.stream(
                    "POST",
                    f"{self.base_url}/v1/messages",
                    json=self._payload(model, messages, tools, stream=True),
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code >= 400:
                        if (self._should_retry_status(resp.status_code)
                                and attempt < self.retries and not started):
                            resp.read()
                            self._sleep_before_retry(attempt)
                            continue
                        body = resp.read().decode("utf-8", "replace")
                        raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {body[:500]}")
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue  # the `event:` lines carry no payload
                        raw = line[len("data:"):].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        kind = event.get("type")

                        if kind == "message_start":
                            first = self._parse_usage(
                                (event.get("message") or {}).get("usage"))
                            if first:
                                usage.add(first)
                        elif kind == "content_block_start":
                            block = event.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                blocks[event.get("index", 0)] = {
                                    "id": block.get("id") or "",
                                    "name": block.get("name") or "",
                                    "json": "",
                                }
                        elif kind == "content_block_delta":
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                piece = delta.get("text") or ""
                                if piece:
                                    started = True
                                    text_parts.append(piece)
                                    yield ("text", piece)
                            elif delta.get("type") == "input_json_delta":
                                slot = blocks.setdefault(
                                    event.get("index", 0), {"id": "", "name": "", "json": ""})
                                slot["json"] += delta.get("partial_json") or ""
                        elif kind == "message_delta":
                            # output_tokens lands here, not on message_start.
                            more = self._parse_usage(event.get("usage"))
                            if more:
                                usage.add(more)
                            stop_reason = (event.get("delta") or {}).get(
                                "stop_reason") or stop_reason
                        elif kind == "error":
                            detail = (event.get("error") or {}).get("message") or "stream error"
                            raise ProviderError(f"{self.name}: {detail}")
                    break  # completed cleanly
            except httpx.HTTPError as exc:
                if not started and attempt < self.retries and self._is_transient_error(exc):
                    self._sleep_before_retry(attempt)
                    continue
                raise ProviderError(f"{self.name}: stream failed: {exc}") from exc

        tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=json.dumps(self._loads_or_empty(slot["json"])),
            )
            for index, slot in sorted(blocks.items())
        ]
        yield ("result", ChatResult(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=stop_reason,
        ))

    def list_models(self) -> list[str]:
        """Ask the API which models this key can use.

        Live rather than hardcoded: model ids change, and a stale list here
        would be a confusing way to learn that.
        """
        try:
            resp = self._client.get(f"{self.base_url}/v1/models", headers=self._headers())
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [m["id"] for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
