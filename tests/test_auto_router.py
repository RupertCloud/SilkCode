import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from silkcode.config import Config, ConfigError


class FakeOllama:
    """Answers GET /api/tags like a running Ollama server."""

    def __init__(self, models):
        payload = json.dumps({"models": [{"name": m} for m in models]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_auto_prefers_running_local_coder_model():
    ollama = FakeOllama(["llama3:8b", "qwen2.5-coder:7b"])
    try:
        config = Config({
            "auto_order": ["local"],
            "providers": {"local": {"type": "ollama", "base_url": ollama.base_url}},
        })
        name, _cfg, model = config.resolve_model("auto")
        assert (name, model) == ("local", "qwen2.5-coder:7b")
    finally:
        ollama.close()


def test_auto_falls_back_to_cloud_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    config = Config({
        # ollama points at a closed port so the local probe fails fast
        "auto_order": ["ollama", "deepseek"],
        "providers": {"ollama": {"type": "ollama", "base_url": "http://127.0.0.1:1"}},
    })
    name, _cfg, model = config.resolve_model("auto")
    assert (name, model) == ("deepseek", "deepseek-chat")


def test_auto_skips_cloud_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
    config = Config({"auto_order": ["deepseek", "qwen"]})
    name, _cfg, model = config.resolve_model("auto")
    assert (name, model) == ("qwen", "qwen3-coder-plus")


def test_auto_errors_when_nothing_available(monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    config = Config({
        "auto_order": ["deepseek", "qwen", "deadlocal"],
        "providers": {"deadlocal": {"type": "ollama", "base_url": "http://127.0.0.1:1"}},
    })
    with pytest.raises(ConfigError, match="auto: no available model"):
        config.resolve_model("auto")
