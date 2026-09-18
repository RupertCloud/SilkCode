"""The Anthropic adapter, and especially the translation it has to do.

Silkcode's loop speaks OpenAI's shapes; the Messages API does not. Most of
what can go wrong here is silent - a request the API accepts that quietly
degrades the conversation - so the translation gets more attention than the
happy path.
"""

import json

import httpx
import pytest

from silkcode.providers import build_provider
from silkcode.providers.anthropic import AnthropicProvider
from silkcode.providers.base import ProviderError


def make_provider(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnthropicProvider("test", "http://test", api_key="k", client=client, **kwargs)


def capture():
    """A handler that records the request body and returns a minimal reply."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        seen["_headers"] = dict(request.headers)
        seen["_url"] = str(request.url)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    return handler, seen


# ---- authentication and framing ---------------------------------------------

def test_uses_x_api_key_and_a_version_header_not_a_bearer_token():
    handler, seen = capture()
    make_provider(handler).chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])
    assert seen["_headers"]["x-api-key"] == "k"
    assert "anthropic-version" in seen["_headers"]
    assert "authorization" not in {k.lower() for k in seen["_headers"]}
    assert seen["_url"].endswith("/v1/messages")


def test_max_tokens_is_always_sent_because_the_api_requires_it():
    handler, seen = capture()
    make_provider(handler).chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])
    assert seen["max_tokens"] > 0


def test_max_tokens_is_configurable_through_the_factory():
    provider = build_provider(
        "anthropic",
        {"type": "anthropic", "base_url": "http://test", "max_tokens": 1234},
        api_key="k",
    )
    assert provider.max_tokens == 1234


# ---- translation -------------------------------------------------------------

def test_system_turn_becomes_the_top_level_system_parameter():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ])
    assert seen["system"] == "be terse"
    assert [m["role"] for m in seen["messages"]] == ["user"]


def test_several_system_turns_are_joined_rather_than_dropped():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "system", "content": "first"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "second"},
    ])
    assert "first" in seen["system"] and "second" in seen["system"]


def test_assistant_tool_calls_become_tool_use_blocks_with_parsed_input():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "working",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": '{"path": "a.py"}'}}]},
    ])
    blocks = seen["messages"][-1]["content"]
    assert seen["messages"][-1]["role"] == "assistant"
    assert blocks[0] == {"type": "text", "text": "working"}
    # input must be an object, not the JSON string the loop stores
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"] == {"path": "a.py"}


def test_malformed_tool_arguments_degrade_to_an_empty_object():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{not json"}}]},
    ])
    assert seen["messages"][-1]["content"][0]["input"] == {}


def test_tool_results_become_a_user_message_of_tool_result_blocks():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result text"},
    ])
    last = seen["messages"][-1]
    assert last["role"] == "user"          # not "tool"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "t1"


def test_parallel_tool_results_are_coalesced_into_one_user_message():
    """The failure this prevents is silent.

    The loop appends one {"role": "tool"} message per call. Sent as separate
    user messages the API accepts them and the model quietly stops making
    parallel calls, which reads as a model regression rather than a bug here.
    """
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "f", "arguments": "{}"}},
            {"id": "t2", "function": {"name": "g", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "one"},
        {"role": "tool", "tool_call_id": "t2", "content": "two"},
    ])
    tool_turns = [m for m in seen["messages"]
                  if m["role"] == "user"
                  and all(b.get("type") == "tool_result" for b in m["content"])]
    assert len(tool_turns) == 1, "results were split across messages"
    assert [b["tool_use_id"] for b in tool_turns[0]["content"]] == ["t1", "t2"]


def test_a_later_user_turn_does_not_get_swallowed_into_tool_results():
    handler, seen = capture()
    make_provider(handler).chat("m", [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "one"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t2", "function": {"name": "g", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t2", "content": "two"},
    ])
    kinds = [(m["role"], m["content"][0]["type"]) for m in seen["messages"]]
    assert kinds.count(("user", "tool_result")) == 2, "separate turns were merged"


def test_tool_schemas_are_converted_to_input_schema():
    handler, seen = capture()
    make_provider(handler).chat("m", [{"role": "user", "content": "hi"}], tools=[{
        "type": "function",
        "function": {"name": "run", "description": "runs",
                     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
    }])
    assert seen["tools"] == [{
        "name": "run",
        "description": "runs",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    }]


# ---- responses ---------------------------------------------------------------

def test_response_blocks_become_content_and_tool_calls():
    def handler(request):
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "I will read it. "},
                {"type": "tool_use", "id": "tu1", "name": "read_file",
                 "input": {"path": "a.py"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    result = make_provider(handler).chat("m", [{"role": "user", "content": "hi"}])
    assert result.content == "I will read it. "
    assert result.finish_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    # arguments come back as the JSON string the loop expects
    assert json.loads(result.tool_calls[0].arguments) == {"path": "a.py"}


def test_cache_tokens_are_reported_separately_from_input_tokens():
    def handler(request):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 3,
                      "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 900},
        })

    usage = make_provider(handler).chat("m", [{"role": "user", "content": "hi"}]).usage
    # Rolling cache reads into prompt_tokens would misprice the turn: a read
    # costs a fraction of an ordinary input token and a write costs more.
    assert usage.prompt_tokens == 12
    assert usage.cache_write_tokens == 100
    assert usage.cache_read_tokens == 900


def test_thinking_blocks_do_not_break_parsing():
    def handler(request):
        return httpx.Response(200, json={
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
        })

    assert make_provider(handler).chat("m", [{"role": "user", "content": "hi"}]).content == "answer"


def test_a_shapeless_response_is_a_provider_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(ProviderError):
        make_provider(handler).chat("m", [{"role": "user", "content": "hi"}])


# ---- streaming ---------------------------------------------------------------

def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_stream_yields_text_then_a_final_result():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 7, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    )

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})

    events = list(make_provider(handler).stream("m", [{"role": "user", "content": "hi"}]))
    assert [e[1] for e in events if e[0] == "text"] == ["Hel", "lo"]
    result = events[-1][1]
    assert events[-1][0] == "result"
    assert result.content == "Hello"
    assert result.finish_reason == "end_turn"
    # input_tokens arrive on message_start, output_tokens on message_delta
    assert result.usage.prompt_tokens == 7
    assert result.usage.completion_tokens == 4


def test_streamed_tool_arguments_are_reassembled_from_fragments():
    """Each fragment is invalid JSON on its own; only the concatenation parses."""
    body = sse(
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu1", "name": "run"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "py'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'test -q"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
    )

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})

    result = list(make_provider(handler).stream("m", [{"role": "user", "content": "hi"}]))[-1][1]
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "tu1"
    assert json.loads(result.tool_calls[0].arguments) == {"cmd": "pytest -q"}


def test_two_streamed_tool_calls_keep_their_own_fragments():
    body = sse(
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "a", "name": "one"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"x": 1}'}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "b", "name": "two"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"y": 2}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
    )

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})

    result = list(make_provider(handler).stream("m", [{"role": "user", "content": "hi"}]))[-1][1]
    assert [c.id for c in result.tool_calls] == ["a", "b"]
    assert json.loads(result.tool_calls[0].arguments) == {"x": 1}
    assert json.loads(result.tool_calls[1].arguments) == {"y": 2}


def test_truncated_tool_arguments_do_not_raise():
    """max_tokens can cut a tool input mid-object; degrade rather than crash."""
    body = sse(
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu1", "name": "run"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "pyte'}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {}},
    )

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})

    result = list(make_provider(handler).stream("m", [{"role": "user", "content": "hi"}]))[-1][1]
    assert json.loads(result.tool_calls[0].arguments) == {}
    assert result.finish_reason == "max_tokens"


def test_a_stream_error_event_is_a_provider_error():
    body = sse({"type": "error", "error": {"message": "overloaded"}})

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "text/event-stream"})

    with pytest.raises(ProviderError, match="overloaded"):
        list(make_provider(handler).stream("m", [{"role": "user", "content": "hi"}]))


# ---- retries and models ------------------------------------------------------

def test_chat_retries_a_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "stop_reason": "end_turn"})

    provider = make_provider(handler, retries=2, retry_delay=0)
    assert provider.chat("m", [{"role": "user", "content": "hi"}]).content == "ok"
    assert len(calls) == 3


def test_a_400_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(ProviderError):
        make_provider(handler, retries=2, retry_delay=0).chat(
            "m", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_list_models_reads_the_api_rather_than_a_hardcoded_list():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "claude-opus-5"},
                                                  {"id": "claude-sonnet-5"}]})

    assert make_provider(handler).list_models() == ["claude-opus-5", "claude-sonnet-5"]


def test_list_models_is_empty_rather_than_fatal_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    assert make_provider(handler).list_models() == []
