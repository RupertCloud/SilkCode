"""Local GUI server: the Silk Code daemon plus a browser front end.

Serves the desktop-style GUI (SRS section 9) from a local HTTP server and
exposes the agent over a small JSON/SSE API (SRS sections 68-69). The same
sessions are shared with the CLI, so work started here can be resumed with
`silkcode resume <id>` (SRS section 47).
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..agent import Agent
from ..config import Config, ConfigError
from ..permissions import PermissionManager
from ..providers import ProviderError, build_provider
from ..context import build_context
from ..repomap import IGNORED_DIRS
from ..sessions import SessionStore, new_session
from ..tools.git import git_diff, git_status
from ..workspace import ToolError, Workspace

MAX_TREE_ENTRIES = 2000
PERMISSION_TIMEOUT = 600  # seconds; deny if the browser never answers


class GuiState:
    def __init__(self, path: str, model_spec: str | None, mode: str,
                 grants: list[str] | None = None):
        self.workspace = Workspace(path)
        self.config = Config.load()
        self.store = SessionStore()
        self.spec = model_spec or self.config.default_model
        provider_name, provider_cfg, model = self.config.resolve_model(self.spec)
        self.provider_name = provider_name
        self.model = model
        provider = build_provider(provider_name, provider_cfg, api_key=self.config.api_key_for(provider_cfg))
        mcp = None
        mcp_servers = self.config.data.get("mcp_servers") or {}
        if mcp_servers:
            from ..mcp import McpManager
            mcp = McpManager(mcp_servers)
        self.permissions = PermissionManager(mode=mode, asker=self._ask_via_gui, grants=grants)
        self.agent = Agent(provider, model, self.workspace, self.permissions,
                           on_event=self._on_agent_event, context=build_context(self.workspace),
                           mcp=mcp)
        self.session = new_session(self.store.new_id(), title="", model=self.spec,
                                   cwd=str(self.workspace.root), mode=mode)

        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.pending: dict[str, dict] = {}
        self.running = False
        self.transcript: list[dict] = []
        self._text_buffer: list[str] = []

    # ---- events -----------------------------------------------------------

    def broadcast(self, event: dict) -> None:
        with self.lock:
            for q in list(self.subscribers):
                q.put(event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _flush_text(self) -> None:
        if self._text_buffer:
            self.transcript.append({"kind": "assistant", "text": "".join(self._text_buffer)})
            self._text_buffer = []

    def _on_agent_event(self, kind: str, data) -> None:
        if kind == "text":
            self._text_buffer.append(data)
            self.broadcast({"type": "text", "data": data})
        elif kind == "tool_start":
            self._flush_text()
            summary = json.dumps(data["args"], ensure_ascii=False)
            entry = {"kind": "tool", "name": data["name"],
                     "args": summary if len(summary) <= 200 else summary[:200] + "..."}
            self.transcript.append(entry)
            self.broadcast({"type": "tool_start", **entry})
        elif kind == "tool_result":
            first = str(data["output"]).splitlines()[0] if str(data["output"]) else ""
            self.broadcast({"type": "tool_result", "name": data["name"], "output": first[:200]})

    def _ask_via_gui(self, prompt: str) -> str:
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        self.pending[req_id] = {"event": ev, "decision": "no", "prompt": prompt}
        self.broadcast({"type": "permission_request", "id": req_id, "prompt": prompt})
        ev.wait(PERMISSION_TIMEOUT)
        return self.pending.pop(req_id)["decision"]

    def answer_permission(self, req_id: str, decision: str) -> bool:
        entry = self.pending.get(req_id)
        if entry is None:
            return False
        if decision not in ("yes", "no", "always"):
            decision = "no"
        entry["decision"] = decision
        entry["event"].set()
        return True

    # ---- agent turns ------------------------------------------------------

    def start_turn(self, text: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        if not self.session["title"]:
            self.session["title"] = text[:60]
        self.transcript.append({"kind": "user", "text": text})
        self.broadcast({"type": "user", "text": text})
        threading.Thread(target=self._run_turn, args=(text,), daemon=True).start()
        return True

    def _run_turn(self, text: str) -> None:
        try:
            self.agent.run_turn(text)
        except ProviderError as exc:
            self.broadcast({"type": "error", "message": str(exc)})
            self.transcript.append({"kind": "error", "text": str(exc)})
        except Exception as exc:  # keep the daemon alive
            self.broadcast({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            self.transcript.append({"kind": "error", "text": str(exc)})
        finally:
            self._flush_text()
            with self.lock:
                self.running = False
            self._save_session()
            self.broadcast({"type": "turn_done", "usage": self.usage_dict()})

    def _save_session(self) -> None:
        self.session["messages"] = self.agent.messages
        self.session["model"] = self.spec
        self.session["mode"] = self.permissions.mode
        self.session["usage"] = self.usage_dict()
        self.store.save(self.session)

    def usage_dict(self) -> dict:
        return {
            "prompt_tokens": self.agent.usage.prompt_tokens,
            "completion_tokens": self.agent.usage.completion_tokens,
            "total_tokens": self.agent.usage.total_tokens,
        }

    # ---- queries / mutations ----------------------------------------------

    def state(self) -> dict:
        return {
            "model": f"{self.provider_name}/{self.model}",
            "spec": self.spec,
            "mode": self.permissions.mode,
            "cwd": str(self.workspace.root),
            "session_id": self.session["id"],
            "running": self.running,
            "usage": self.usage_dict(),
        }

    def stop(self) -> None:
        self.agent.request_stop()

    def load_session(self, session_id: int) -> None:
        if self.running:
            raise ToolError("cannot switch sessions while the agent is running")
        data = self.store.load(session_id)
        self.session = data
        if data.get("messages"):
            self.agent.messages = data["messages"]
        self.agent.usage.prompt_tokens = data.get("usage", {}).get("prompt_tokens", 0)
        self.agent.usage.completion_tokens = data.get("usage", {}).get("completion_tokens", 0)
        self.transcript = _transcript_from_messages(self.agent.messages)
        try:
            self.switch_model(data.get("model") or self.spec)
        except ConfigError:
            pass  # keep the current model if the saved one is gone
        if data.get("mode") in PermissionManager.MODES:
            self.permissions.mode = data["mode"]
        self.broadcast({"type": "reload"})

    def switch_model(self, spec: str) -> None:
        provider_name, provider_cfg, model = self.config.resolve_model(spec)
        provider = build_provider(provider_name, provider_cfg, api_key=self.config.api_key_for(provider_cfg))
        self.agent.provider = provider
        self.agent.model = model
        self.provider_name, self.model, self.spec = provider_name, model, spec

    def providers_info(self) -> list[dict]:
        out = []
        for name in sorted(self.config.providers):
            cfg = self.config.providers[name]
            info = {
                "name": name,
                "base_url": cfg.get("base_url", ""),
                "type": cfg.get("type", "openai_compat"),
                "default_model": cfg.get("default_model"),
                "has_key": bool(self.config.api_key_for(cfg)),
                "needs_key": bool(cfg.get("api_key_env") or cfg.get("api_key")),
                "local_models": [],
            }
            if cfg.get("type") == "ollama":
                provider = build_provider(name, cfg)
                info["local_models"] = provider.list_models()
            out.append(info)
        return out

    def add_provider(self, body: dict) -> None:
        name = str(body.get("name", "")).strip()
        base_url = str(body.get("base_url", "")).strip()
        if not name or not base_url:
            raise ConfigError("Provider name and base URL are required")
        cfg = {"type": body.get("type", "openai_compat"), "base_url": base_url}
        if body.get("default_model"):
            cfg["default_model"] = body["default_model"]
        if body.get("api_key_env"):
            cfg["api_key_env"] = body["api_key_env"]
        if body.get("api_key"):
            cfg["api_key"] = body["api_key"]
        self.config.set_provider(name, cfg)
        self.config.save()

    # ---- GitHub authorization (SRS sections 30-31, 60) ---------------------

    def github_status(self) -> dict:
        from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, token_from_env
        github_cfg = self.config.data.get("github") or {}
        status: dict = {
            "token_env": github_cfg.get("token_env", "GITHUB_TOKEN"),
            "token_stored": bool(github_cfg.get("token")),
            "connected": False,
            "login": None,
            "repo": None,
            "grants": sorted(self.permissions.grants),
        }
        try:
            owner, repo = detect_repo(self.workspace)
            status["repo"] = f"{owner}/{repo}"
        except ToolError:
            pass
        token = token_from_env(self.config.data)
        if token:
            try:
                client = GitHubClient(token, github_cfg.get("api_url", DEFAULT_API_URL))
                status["login"] = client.whoami()
                status["connected"] = True
            except ToolError as exc:
                status["error"] = str(exc)
        return status

    def set_github_token(self, token: str) -> dict:
        from ..github import DEFAULT_API_URL, GitHubClient
        token = token.strip()
        if not token:
            raise ConfigError("empty token")
        github_cfg = self.config.data.setdefault("github", {})
        client = GitHubClient(token, github_cfg.get("api_url", DEFAULT_API_URL))
        client.whoami()  # verify before storing
        github_cfg["token"] = token
        self.config.save()
        return self.github_status()

    def set_grants(self, grants: list) -> dict:
        from ..permissions import GRANTABLE
        cleaned = {g for g in grants if g in GRANTABLE}
        unknown = [g for g in grants if g not in GRANTABLE]
        if unknown:
            raise ConfigError(f"unknown grants: {', '.join(map(str, unknown))}; "
                              f"allowed: {', '.join(GRANTABLE)}")
        self.permissions.grants = cleaned
        return self.github_status()

    def tree(self) -> list[dict]:
        entries: list[dict] = []

        def walk(directory: Path, depth: int) -> None:
            try:
                children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_TREE_ENTRIES:
                    return
                if child.name in IGNORED_DIRS or child.name.endswith(".egg-info"):
                    continue
                entries.append({"path": self.workspace.relative(child), "dir": child.is_dir(), "depth": depth})
                if child.is_dir():
                    walk(child, depth + 1)

        walk(self.workspace.root, 0)
        return entries

    def read_file(self, rel_path: str) -> dict:
        p = self.workspace.resolve(rel_path)
        if not p.is_file():
            raise ToolError(f"Not a file: {rel_path}")
        if p.stat().st_size > 1_000_000:
            return {"path": rel_path, "content": "(file too large to display)"}
        return {"path": rel_path, "content": p.read_text(errors="replace")}


def _transcript_from_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "user":
            out.append({"kind": "user", "text": m.get("content", "")})
        elif m.get("role") == "assistant":
            if m.get("content"):
                out.append({"kind": "assistant", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                args = tc.get("function", {}).get("arguments", "")
                out.append({
                    "kind": "tool",
                    "name": tc.get("function", {}).get("name", "?"),
                    "args": args if len(args) <= 200 else args[:200] + "...",
                })
    return out


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState = None  # type: ignore[assignment]
    html: bytes = b""

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        pass

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        st = self.state
        try:
            if route == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(self.html)))
                self.end_headers()
                self.wfile.write(self.html)
            elif route == "/api/state":
                self._json(st.state())
            elif route == "/api/transcript":
                self._json(st.transcript)
            elif route == "/api/tree":
                self._json(st.tree())
            elif route == "/api/file":
                params = parse_qs(parsed.query)
                path = (params.get("path") or [""])[0]
                self._json(st.read_file(path))
            elif route == "/api/diff":
                self._json({"diff": git_diff(st.workspace), "status": git_status(st.workspace)})
            elif route == "/api/providers":
                self._json(st.providers_info())
            elif route == "/api/sessions":
                self._json(st.store.list())
            elif route == "/api/github/status":
                self._json(st.github_status())
            elif route == "/api/events":
                self._sse()
            else:
                self._error("not found", 404)
        except (ToolError, ConfigError) as exc:
            self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self) -> None:
        st = self.state
        q = st.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    event = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            st.unsubscribe(q)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        st = self.state
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error("invalid JSON body")
        route = urlparse(self.path).path
        try:
            if route == "/api/message":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._error("empty message")
                if not st.start_turn(text):
                    return self._error("agent is already running", 409)
                self._json({"ok": True})
            elif route == "/api/permission":
                if st.answer_permission(str(body.get("id", "")), str(body.get("decision", "no"))):
                    self._json({"ok": True})
                else:
                    self._error("unknown permission request", 404)
            elif route == "/api/model":
                if st.running:
                    return self._error("cannot switch models while the agent is running", 409)
                st.switch_model(str(body.get("spec", "")))
                self._json(st.state())
            elif route == "/api/mode":
                mode = str(body.get("mode", ""))
                if mode not in PermissionManager.MODES:
                    return self._error(f"unknown mode '{mode}'")
                st.permissions.mode = mode
                self._json(st.state())
            elif route == "/api/revert":
                if st.running:
                    return self._error("cannot revert while the agent is running", 409)
                self._json({"restored": st.agent.checkpoints.revert_last()})
            elif route == "/api/providers":
                st.add_provider(body)
                self._json(st.providers_info())
            elif route == "/api/stop":
                st.stop()
                self._json({"ok": True})
            elif route == "/api/github/token":
                self._json(st.set_github_token(str(body.get("token", ""))))
            elif route == "/api/github/grants":
                grants = body.get("grants")
                if not isinstance(grants, list):
                    return self._error("'grants' must be a list")
                self._json(st.set_grants(grants))
            elif route == "/api/session":
                try:
                    st.load_session(int(body.get("id", 0)))
                except FileNotFoundError as exc:
                    return self._error(str(exc), 404)
                self._json(st.state())
            else:
                self._error("not found", 404)
        except (ToolError, ConfigError, ProviderError) as exc:
            self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass


def run_gui(path: str, model_spec: str | None, mode: str, host: str = "127.0.0.1",
            port: int = 8377, grants: list[str] | None = None) -> int:
    try:
        state = GuiState(path, model_spec, mode, grants=grants)
    except (ToolError, ConfigError) as exc:
        print(f"error: {exc}")
        return 1
    GuiHandler.state = state
    GuiHandler.html = (Path(__file__).parent / "app.html").read_bytes()
    server = ThreadingHTTPServer((host, port), GuiHandler)
    url = f"http://{host}:{port}"
    print(f"Silk Code GUI: {url}")
    print(f"workspace: {state.workspace.root}")
    print(f"model: {state.provider_name}/{state.model}   session: #{state.session['id']}")
    print("Press Ctrl+C to stop.")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0
