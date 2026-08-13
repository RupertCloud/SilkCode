import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from silkcode.providers.base import ChatResult, ModelProvider


class FakeProvider(ModelProvider):
    """Scripted in-process provider for agent-loop tests."""

    def __init__(self, results: list[ChatResult]):
        super().__init__("fake")
        self.results = list(results)
        self.calls: list[list[dict]] = []

    def chat(self, model, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        return self.results.pop(0)

    def list_models(self):
        return ["fake-model"]


def sse_response(content=None, tool_calls=None, usage=None):
    """Build SSE bytes for one scripted chat-completions stream."""
    chunks = []
    if content:
        for piece in (content[: len(content) // 2], content[len(content) // 2 :]):
            if piece:
                chunks.append({"choices": [{"delta": {"content": piece}}]})
    if tool_calls:
        for i, (name, arguments) in enumerate(tool_calls):
            chunks.append({"choices": [{"delta": {"tool_calls": [
                {"index": i, "id": f"call_{i}", "function": {"name": name, "arguments": ""}}
            ]}}]})
            half = len(arguments) // 2
            for piece in (arguments[:half], arguments[half:]):
                if piece:
                    chunks.append({"choices": [{"delta": {"tool_calls": [
                        {"index": i, "function": {"arguments": piece}}
                    ]}}]})
    finish = "tool_calls" if tool_calls else "stop"
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    if usage:
        chunks.append({"choices": [], "usage": usage})
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


class StubModelServer:
    """A local OpenAI-compatible server that replays scripted responses."""

    def __init__(self, scripted: list[bytes]):
        self.scripted = list(scripted)
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                server.requests.append(json.loads(self.rfile.read(length)))
                body = server.scripted.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub_server():
    def make(scripted):
        return StubModelServer(scripted)
    return make
