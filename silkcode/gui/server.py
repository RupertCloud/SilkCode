"""Local GUI server: the Silk Code daemon plus a browser front end.

Serves the desktop-style GUI (SRS section 9) from a local HTTP server and
exposes the agent over a small JSON/SSE API (SRS sections 68-69). Multiple
sessions can be open at once (SRS section 44) - each has its own agent,
conversation, and checkpoints; events are tagged with their session id.
Sessions are shared with the CLI, so work started here can be resumed with
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
from ..agent.loop import DEFAULT_CONTEXT_TOKENS
from ..config import Config, ConfigError
from ..context import build_context
from ..permissions import PermissionManager
from ..providers import ProviderError, build_provider
from ..repomap import IGNORED_DIRS
from ..sessions import SessionStore, new_session
from ..tools.git import git_diff, git_status
from ..workspace import ToolError, Workspace

MAX_TREE_ENTRIES = 2000
PERMISSION_TIMEOUT = 600  # seconds; deny if the browser never answers


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


class AgentSession:
    """One conversation: its own agent, transcript, and checkpoints."""

    def __init__(self, state: "GuiState", data: dict):
        self.data = data
        self.id = data["id"]
        self.transcript: list[dict] = []
        self._text_buffer: list[str] = []
        self.running = False

        spec = data.get("model") or state.spec
        try:
            provider_name, provider_cfg, model = state.config.resolve_model(spec)
        except ConfigError:
            spec = state.spec
            provider_name, provider_cfg, model = state.config.resolve_model(spec)
        self.spec = spec
        self.provider_name = provider_name
        provider = build_provider(provider_name, provider_cfg,
                                  api_key=state.config.api_key_for(provider_cfg))

        permissions = PermissionManager(
            mode=data.get("mode") or state.mode,
            asker=lambda prompt, sid=self.id: state._ask_via_gui(prompt, sid),
        )
        permissions.grants = state.shared_grants  # one grant set for the whole app
        self.permissions = permissions

        self.agent = Agent(
            provider, model, state.workspace, permissions,
            on_event=lambda kind, payload: state._on_agent_event(self, kind, payload),
            context=state.context, mcp=state.mcp,
            max_context_tokens=provider_cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS,
        )
        if data.get("messages"):
            self.agent.messages = data["messages"]
            self.transcript = _transcript_from_messages(data["messages"])
        usage = data.get("usage") or {}
        self.agent.usage.prompt_tokens = usage.get("prompt_tokens", 0)
        self.agent.usage.completion_tokens = usage.get("completion_tokens", 0)

    @property
    def model(self) -> str:
        return self.agent.model

    def flush_text(self) -> None:
        if self._text_buffer:
            self.transcript.append({"kind": "assistant", "text": "".join(self._text_buffer)})
            self._text_buffer = []

    def usage_dict(self) -> dict:
        return {
            "prompt_tokens": self.agent.usage.prompt_tokens,
            "completion_tokens": self.agent.usage.completion_tokens,
            "total_tokens": self.agent.usage.total_tokens,
        }


class GuiState:
    def __init__(self, path: str, model_spec: str | None, mode: str,
                 grants: list[str] | None = None, use_sandbox: bool = False,
                 auto_push: bool = False):
        self.auto_push = auto_push
        if auto_push:
            grants = list(grants or []) + ["push"]
        self.workspace = Workspace(path)
        self.config = Config.load()
        if use_sandbox:
            from ..execbackend import remote_backend_from_config
            backend = remote_backend_from_config(self.config.data)
            if backend is None:
                raise ToolError("no sandbox configured; run 'silkcode sandbox connect <url>'")
            backend.health()
            self.workspace.exec_backend = backend
        self.store = SessionStore()
        self.spec = model_spec or self.config.default_model
        self.config.resolve_model(self.spec)  # validate early
        self.mode = mode
        self.shared_grants: set[str] = {g for g in (grants or [])}
        self.context = build_context(self.workspace)
        self.mcp = None
        mcp_servers = self.config.data.get("mcp_servers") or {}
        if mcp_servers:
            from ..mcp import McpManager
            self.mcp = McpManager(mcp_servers)

        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.pending: dict[str, dict] = {}
        self.sessions: dict[int, AgentSession] = {}
        first = self.new_session()
        self.default_session_id = first.id

    # ---- sessions ----------------------------------------------------------

    def new_session(self) -> AgentSession:
        # unsaved open sessions also occupy their ids, not just those on disk
        session_id = max([self.store.new_id()] + [sid + 1 for sid in self.sessions])
        data = new_session(session_id, title="", model=self.spec,
                           cwd=str(self.workspace.root), mode=self.mode)
        session = AgentSession(self, data)
        self.sessions[session.id] = session
        self.default_session_id = session.id
        return session

    def get_session(self, session_id: int | None = None) -> AgentSession:
        if session_id is None:
            session_id = self.default_session_id
        session = self.sessions.get(session_id)
        if session is None:
            data = self.store.load(session_id)  # raises FileNotFoundError
            session = AgentSession(self, data)
            self.sessions[session_id] = session
        return session

    def load_session(self, session_id: int) -> AgentSession:
        session = self.get_session(session_id)
        self.default_session_id = session.id
        self.broadcast({"type": "reload", "session": session.id})
        return session

    def sessions_summary(self) -> list[dict]:
        loaded = {
            s.id: {"id": s.id, "title": s.data.get("title", ""), "model": s.spec,
                   "running": s.running, "open": True}
            for s in self.sessions.values()
        }
        for saved in self.store.list():
            if saved["id"] not in loaded:
                loaded[saved["id"]] = {"id": saved["id"], "title": saved["title"],
                                       "model": saved["model"], "running": False, "open": False}
        return sorted(loaded.values(), key=lambda s: s["id"], reverse=True)

    # ---- compatibility accessors (default session) -------------------------

    @property
    def agent(self):
        return self.get_session().agent

    @property
    def permissions(self):
        return self.get_session().permissions

    @property
    def session(self) -> dict:
        return self.get_session().data

    @property
    def running(self) -> bool:
        return self.get_session().running

    def usage_dict(self) -> dict:
        return self.get_session().usage_dict()

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

    def _on_agent_event(self, session: AgentSession, kind: str, data) -> None:
        if kind == "text":
            session._text_buffer.append(data)
            self.broadcast({"type": "text", "data": data, "session": session.id})
        elif kind == "tool_start":
            session.flush_text()
            summary = json.dumps(data["args"], ensure_ascii=False)
            entry = {"kind": "tool", "name": data["name"],
                     "args": summary if len(summary) <= 200 else summary[:200] + "..."}
            session.transcript.append(entry)
            self.broadcast({"type": "tool_start", "session": session.id, **entry})
        elif kind == "tool_result":
            first = str(data["output"]).splitlines()[0] if str(data["output"]) else ""
            self.broadcast({"type": "tool_result", "name": data["name"],
                            "output": first[:200], "session": session.id})

    def _ask_via_gui(self, prompt: str, session_id: int | None = None) -> str:
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        self.pending[req_id] = {"event": ev, "decision": "no", "prompt": prompt}
        self.broadcast({"type": "permission_request", "id": req_id, "prompt": prompt,
                        "session": session_id})
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

    def start_turn(self, text: str, session_id: int | None = None) -> bool:
        session = self.get_session(session_id)
        with self.lock:
            if session.running:
                return False
            session.running = True
        if not session.data.get("title"):
            session.data["title"] = text[:60]
        session.transcript.append({"kind": "user", "text": text})
        self.broadcast({"type": "user", "text": text, "session": session.id})
        threading.Thread(target=self._run_turn, args=(session, text), daemon=True).start()
        return True

    def _run_turn(self, session: AgentSession, text: str) -> None:
        try:
            session.agent.run_turn(text)
        except ProviderError as exc:
            self.broadcast({"type": "error", "message": str(exc), "session": session.id})
            session.transcript.append({"kind": "error", "text": str(exc)})
        except Exception as exc:  # keep the daemon alive
            self.broadcast({"type": "error", "message": f"{type(exc).__name__}: {exc}",
                            "session": session.id})
            session.transcript.append({"kind": "error", "text": str(exc)})
        finally:
            session.flush_text()
            if self.auto_push:
                from ..tools.git import push_if_needed
                try:
                    pushed = push_if_needed(self.workspace)
                except Exception as exc:
                    pushed = f"auto-push failed: {exc}"
                if pushed:
                    session.transcript.append({"kind": "tool", "name": "auto-push", "args": pushed})
                    self.broadcast({"type": "push_result", "message": pushed, "session": session.id})
            with self.lock:
                session.running = False
            self._save_session(session)
            self.broadcast({"type": "turn_done", "usage": session.usage_dict(),
                            "session": session.id})

    def _save_session(self, session: AgentSession) -> None:
        session.data["messages"] = session.agent.messages
        session.data["model"] = session.spec
        session.data["mode"] = session.permissions.mode
        session.data["usage"] = {
            "prompt_tokens": session.agent.usage.prompt_tokens,
            "completion_tokens": session.agent.usage.completion_tokens,
        }
        self.store.save(session.data)

    # ---- queries / mutations ----------------------------------------------

    def state(self, session_id: int | None = None) -> dict:
        session = self.get_session(session_id)
        return {
            "model": f"{session.provider_name}/{session.model}",
            "spec": session.spec,
            "mode": session.permissions.mode,
            "cwd": str(self.workspace.root),
            "session_id": session.id,
            "running": session.running,
            "auto_push": self.auto_push,
            "usage": session.usage_dict(),
            "sessions": self.sessions_summary(),
        }

    def stop(self, session_id: int | None = None) -> None:
        self.get_session(session_id).agent.request_stop()

    def switch_model(self, spec: str, session_id: int | None = None) -> None:
        session = self.get_session(session_id)
        provider_name, provider_cfg, model = self.config.resolve_model(spec)
        provider = build_provider(provider_name, provider_cfg,
                                  api_key=self.config.api_key_for(provider_cfg))
        session.agent.provider = provider
        session.agent.model = model
        session.provider_name, session.spec = provider_name, spec
        session.data["model"] = spec

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for session in self.sessions.values():
            session.permissions.mode = mode

    # ---- GitHub authorization (SRS sections 30-31, 60) ---------------------

    def github_status(self) -> dict:
        from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, get_token
        from ..github_oauth import client_id_from
        github_cfg = self.config.data.get("github") or {}
        status: dict = {
            "token_env": github_cfg.get("token_env", "GITHUB_TOKEN"),
            "token_stored": bool(github_cfg.get("token")),
            "device_flow_available": bool(client_id_from(self.config.data)),
            "connected": False,
            "login": None,
            "repo": None,
            "grants": sorted(self.shared_grants),
        }
        try:
            owner, repo = detect_repo(self.workspace)
            status["repo"] = f"{owner}/{repo}"
        except ToolError:
            pass
        token = get_token(self.config)
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
        github_cfg.pop("token_expires_at", None)
        self.config.save()
        return self.github_status()

    def github_device_start(self) -> dict:
        from ..github_oauth import DeviceFlow, DeviceFlowError, client_id_from, store_token
        client_id = client_id_from(self.config.data)
        if not client_id:
            raise ConfigError(
                "Sign in with GitHub is not set up: the Silk Code GitHub App client id is "
                "missing. Maintainers: see docs/GITHUB_APP.md. You can also paste a token below."
            )
        flow = DeviceFlow(client_id)
        info = flow.start()

        def wait_for_authorization():
            try:
                data = flow.poll(info["device_code"], int(info.get("interval", 5)),
                                 int(info.get("expires_in", 900)))
                store_token(self.config, data)
                self.broadcast({"type": "github_connected"})
            except DeviceFlowError as exc:
                self.broadcast({"type": "github_error", "message": str(exc)})

        threading.Thread(target=wait_for_authorization, daemon=True).start()
        return {"user_code": info["user_code"], "verification_uri": info["verification_uri"]}

    def set_grants(self, grants: list) -> dict:
        from ..permissions import GRANTABLE
        unknown = [g for g in grants if g not in GRANTABLE]
        if unknown:
            raise ConfigError(f"unknown grants: {', '.join(map(str, unknown))}; "
                              f"allowed: {', '.join(GRANTABLE)}")
        self.shared_grants.clear()
        self.shared_grants.update(g for g in grants if g in GRANTABLE)
        return self.github_status()

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

    def tree(self) -> list[dict]:
        entries: list[dict] = []

        def walk(directory: Path, depth: int) -> None:
            if len(entries) >= MAX_TREE_ENTRIES:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_TREE_ENTRIES:
                    return
                if child.name in IGNORED_DIRS or child.name.endswith(".egg-info"):
                    continue
                entries.append({"path": self.workspace.relative(child), "dir": child.is_dir(),
                                "depth": depth})
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

    @staticmethod
    def _session_of(params: dict | None, body: dict | None = None) -> int | None:
        raw = None
        if body and body.get("session_id") is not None:
            raw = body["session_id"]
        elif params and params.get("session"):
            raw = params["session"][0]
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        st = self.state
        sid = self._session_of(params)
        try:
            if route == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(self.html)))
                self.end_headers()
                self.wfile.write(self.html)
            elif route == "/api/state":
                self._json(st.state(sid))
            elif route == "/api/transcript":
                self._json(st.get_session(sid).transcript)
            elif route == "/api/tree":
                self._json(st.tree())
            elif route == "/api/file":
                path = (params.get("path") or [""])[0]
                self._json(st.read_file(path))
            elif route == "/api/diff":
                self._json({"diff": git_diff(st.workspace), "status": git_status(st.workspace)})
            elif route == "/api/providers":
                self._json(st.providers_info())
            elif route == "/api/sessions":
                self._json(st.sessions_summary())
            elif route == "/api/github/status":
                self._json(st.github_status())
            elif route == "/api/events":
                self._sse()
            else:
                self._error("not found", 404)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
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
        sid = self._session_of(None, body)
        try:
            if route == "/api/message":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._error("empty message")
                if not st.start_turn(text, sid):
                    return self._error("agent is already running in this session", 409)
                self._json({"ok": True})
            elif route == "/api/permission":
                if st.answer_permission(str(body.get("id", "")), str(body.get("decision", "no"))):
                    self._json({"ok": True})
                else:
                    self._error("unknown permission request", 404)
            elif route == "/api/model":
                if st.get_session(sid).running:
                    return self._error("cannot switch models while the agent is running", 409)
                st.switch_model(str(body.get("spec", "")), sid)
                self._json(st.state(sid))
            elif route == "/api/mode":
                mode = str(body.get("mode", ""))
                if mode not in PermissionManager.MODES:
                    return self._error(f"unknown mode '{mode}'")
                st.set_mode(mode)
                self._json(st.state(sid))
            elif route == "/api/revert":
                session = st.get_session(sid)
                if session.running:
                    return self._error("cannot revert while the agent is running", 409)
                self._json({"restored": session.agent.checkpoints.revert_last()})
            elif route == "/api/push":
                if st.get_session(sid).running:
                    return self._error("cannot push while the agent is running", 409)
                from ..tools.git import git_push
                self._json({"result": git_push(st.workspace)})
            elif route == "/api/autopush":
                st.auto_push = bool(body.get("enabled"))
                if st.auto_push:
                    st.shared_grants.add("push")
                self._json(st.state(sid))
            elif route == "/api/stop":
                st.stop(sid)
                self._json({"ok": True})
            elif route == "/api/session/new":
                session = st.new_session()
                self._json(st.state(session.id))
            elif route == "/api/session":
                session = st.load_session(int(body.get("id", 0)))
                self._json(st.state(session.id))
            elif route == "/api/github/token":
                self._json(st.set_github_token(str(body.get("token", ""))))
            elif route == "/api/github/device":
                self._json(st.github_device_start())
            elif route == "/api/github/grants":
                grants = body.get("grants")
                if not isinstance(grants, list):
                    return self._error("'grants' must be a list")
                self._json(st.set_grants(grants))
            elif route == "/api/providers":
                st.add_provider(body)
                self._json(st.providers_info())
            else:
                self._error("not found", 404)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (ToolError, ConfigError, ProviderError) as exc:
            self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass


def run_gui(path: str, model_spec: str | None, mode: str, host: str = "127.0.0.1",
            port: int = 8377, grants: list[str] | None = None,
            use_sandbox: bool = False, auto_push: bool = False) -> int:
    try:
        state = GuiState(path, model_spec, mode, grants=grants, use_sandbox=use_sandbox,
                         auto_push=auto_push)
    except (ToolError, ConfigError) as exc:
        print(f"error: {exc}")
        return 1
    GuiHandler.state = state
    GuiHandler.html = (Path(__file__).parent / "app.html").read_bytes()
    server = ThreadingHTTPServer((host, port), GuiHandler)
    url = f"http://{host}:{port}"
    print(f"Silk Code GUI: {url}")
    print(f"workspace: {state.workspace.root}")
    first = state.get_session()
    print(f"model: {first.provider_name}/{first.model}   session: #{first.id}")
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
