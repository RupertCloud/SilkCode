"""Local Ollama server, via its OpenAI-compatible API (SRS section 20)."""

from __future__ import annotations

import httpx

from .openai_compat import OpenAICompatProvider


class OllamaProvider(OpenAICompatProvider):
    def __init__(self, name: str = "ollama", base_url: str = "http://localhost:11434", **kwargs):
        self.root_url = base_url.rstrip("/")
        super().__init__(name, base_url=f"{self.root_url}/v1", **kwargs)

    def list_models(self) -> list[str]:
        # Same headers as a chat request: an Ollama reached over the network is
        # often behind an authenticating proxy (see `silkcode inference link
        # --token-env`), and a listing that 401s would report the server as
        # holding no models at all.
        try:
            resp = self._client.get(f"{self.root_url}/api/tags", headers=self._headers())
            resp.raise_for_status()
            return sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
        except (httpx.HTTPError, ValueError):
            return []

    def capabilities(self) -> dict:
        caps = super().capabilities()
        caps["local"] = True
        return caps
