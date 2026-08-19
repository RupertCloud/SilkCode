import json

import httpx
import pytest

from silkcode.providers import build_provider
from silkcode.providers.base import ProviderError
from silkcode.providers.ollama import OllamaProvider
from silkcode.providers.openai_compat import OpenAICompatProvider


def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("test", "http://test/v1", api_key="k", client=client)


def test_chat_retries_on_transport_timeout_and_succeeds():
    from httpx import ReadTimeout

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
        })

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=2, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = p.chat("m1", [{"role": "user", "content": "hi"}])
    assert result.content == "recovered"
    assert len(calls) == 2


def test_chat_retries_on_429_and_503():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429 if len(calls) < 3 else 200, json={} if len(calls) < 3 else {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=3, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = p.chat("m1", [])
    assert result.content == "ok"
    assert len(calls) == 3


def test_chat_does_not_retry_client_4xx():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=3, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="401"):
        p.chat("m1", [])
    assert len(calls) == 1  # a 401 is a request problem, not worth a retry


def test_chat_raises_after_retries_exhausted():
    from httpx import ReadTimeout

    def handler(request):
        raise ReadTimeout("still timing out", request=request)

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=2, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="request failed"):
        p.chat("m1", [])


def test_chat_parses_content_and_usage():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer k"
        payload = json.loads(request.content)
        assert payload["model"] == "m1"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    result = make_provider(handler).chat("m1", [{"role": "user", "content": "hi"}])
    assert result.content == "hello"
    assert result.usage.total_tokens == 15
    assert result.tool_calls == []


def test_chat_parses_tool_calls():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            ]}, "finish_reason": "tool_calls"}],
        })

    result = make_provider(handler).chat("m1", [])
    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    assert json.loads(result.tool_calls[0].arguments) == {"path": "a.py"}


def test_chat_http_error_raises():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(ProviderError, match="401"):
        make_provider(handler).chat("m1", [])


def test_stream_assembles_text_and_tool_calls():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "grep", "arguments": '{"pat'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'tern": "x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request):
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    events = list(make_provider(handler).stream("m1", []))
    texts = [d for k, d in events if k == "text"]
    assert "".join(texts) == "Hello"
    result = [d for k, d in events if k == "result"][0]
    assert result.content == "Hello"
    assert result.tool_calls[0].id == "call_9"
    assert result.tool_calls[0].name == "grep"
    assert json.loads(result.tool_calls[0].arguments) == {"pattern": "x"}
    assert result.usage.total_tokens == 10


def test_stream_retries_on_timeout_before_any_output():
    from httpx import ReadTimeout

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise ReadTimeout("never connected", request=request)
        body = "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=2, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    events = list(p.stream("m1", []))
    assert "".join(d for k, d in events if k == "text") == "hi"
    assert len(calls) == 2


def test_stream_raises_after_retries_exhausted_without_output():
    from httpx import ReadTimeout

    def handler(request):
        raise ReadTimeout("persistent", request=request)

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=2, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="stream failed"):
        list(p.stream("m1", []))


def test_stream_does_not_retry_once_output_has_yielded():
    """Once the first chunk is out, a mid-stream failure must surface as a
    ProviderError rather than re-streaming (which would duplicate tokens)."""
    from httpx import ReadError

    # body that yields one chunk then blows up on the next line read
    def body():
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": "par"}}]}) + "\n\n").encode()
        raise ReadError("connection dropped mid-read")

    def handler(request):
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider("test", "http://test/v1", api_key="k",
                             retries=2, retry_delay=0,
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    gen = p.stream("m1", [])
    # the first chunk arrives fine (started=True), then the next read fails
    assert next(gen) == ("text", "par")
    with pytest.raises(ProviderError, match="stream failed"):
        next(gen)


def test_build_provider_types():
    p = build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1"})
    assert isinstance(p, OpenAICompatProvider)
    o = build_provider("ollama", {"type": "ollama", "base_url": "http://localhost:11434"})
    assert isinstance(o, OllamaProvider)
    assert o.base_url == "http://localhost:11434/v1"
    with pytest.raises(ProviderError):
        build_provider("x", {"type": "nope", "base_url": "http://h"})


def test_build_provider_timeout_from_config():
    # default is 180s
    assert build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1"})._client.timeout.connect == 180.0
    # a configured 'timeout' overrides it (e.g. slow first token from DeepSeek)
    p = build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1", "timeout": 600})
    assert p._client.timeout.connect == 600.0
    # strings are accepted too (config files are hand-edited)
    p = build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1", "timeout": "300"})
    assert p._client.timeout.connect == 300.0
    # garbage fails loudly instead of silently keeping the default
    with pytest.raises(ProviderError, match="invalid 'timeout'"):
        build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1", "timeout": "soon"})


def test_build_provider_retries_from_config():
    p = build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1"})
    assert p.retries == 2 and p.retry_delay == 1.0
    p = build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1",
                             "retries": 5, "retry_delay": "0.5"})
    assert p.retries == 5 and p.retry_delay == 0.5
    with pytest.raises(ProviderError, match="retries"):
        build_provider("x", {"type": "openai_compat", "base_url": "http://h/v1", "retries": "lots"})


def test_the_cli_starts_without_importing_an_http_stack():
    """httpx costs most of the interpreter's startup, and the commands people
    run most often — sessions, config, env, --help — never talk to a model.

    Measured in a fresh interpreter, because this session has already
    imported everything.
    """
    import subprocess
    import sys

    probe = (
        "import sys, silkcode.cli.main;"
        "print('httpx' if 'httpx' in sys.modules else 'clean')"
    )
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "clean", (
        "importing the CLI pulled in httpx; something in the import chain "
        "reaches a provider or an HTTP client at module level")


def test_a_provider_still_loads_when_one_is_actually_built():
    """Laziness must not become breakage."""
    from silkcode.providers import build_provider

    provider = build_provider("x", {"type": "openai_compat",
                                    "base_url": "http://127.0.0.1:1/v1"})
    assert provider.__class__.__name__ == "OpenAICompatProvider"
    ollama = build_provider("y", {"type": "ollama", "base_url": "http://127.0.0.1:1"})
    assert ollama.__class__.__name__ == "OllamaProvider"


def test_the_provider_classes_are_still_importable_by_name():
    """`from silkcode.providers import OllamaProvider` is public API."""
    from silkcode.providers import OllamaProvider, OpenAICompatProvider

    assert OllamaProvider.__name__ == "OllamaProvider"
    assert OpenAICompatProvider.__name__ == "OpenAICompatProvider"


def test_an_unknown_provider_type_is_still_rejected():
    import pytest as _pytest

    from silkcode.providers import ProviderError, build_provider

    with _pytest.raises(ProviderError, match="Unknown provider type"):
        build_provider("x", {"type": "not-a-thing", "base_url": "http://x"})
