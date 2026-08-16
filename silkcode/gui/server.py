"""Local GUI server: the Silk Code daemon plus a browser front end.

Serves the desktop-style GUI (SRS section 9) from a local HTTP server and
exposes the agent over a small JSON/SSE API (SRS sections 68-69). Multiple
sessions can be open at once (SRS section 44) - each has its own agent,
conversation, and checkpoints; events are tagged with their session id.
Sessions are shared with the CLI, so work started here can be resumed with
`silkcode resume <id>` (SRS section 47).

Several daemons can run on the same machine at the same time - each on its
own address (--host/--port) and pointed at its own project. Session ids are
allocated atomically across processes (see silkcode.sessions) so instances
never hand out the same id or clobber each other's session files, and each
session is tagged with the instance that created it. Opening the same
project in two instances is allowed; the per-workspace lock makes the
second one read-only until the first closes.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..agent import Agent
from ..agent.loop import DEFAULT_CONTEXT_TOKENS
from ..config import Config, ConfigError
from ..context import build_context
from ..lock import LockError, acquire, lock_state, release
from ..permissions import PermissionManager
from ..providers import ProviderError, build_provider
from ..project import record_recent_project, resolve_project
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
    """One conversation: its own agent, transcript, checkpoints — and its
    own workspace (a project), so sessions can open different projects."""

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

        # Resolve this session's workspace: a specific project, or the app's
        # default workspace when the session was created on a directory.
        cwd = data.get("cwd") or str(state.workspace.root)
        self.workspace = state._resolve_workspace(cwd)

        # Advisory per-workspace lock: one writer at a time per project. A
        # second session on the same project is told "already in use" up front
        # and its file writes are refused until the lock goes stale.
        self.lock_owner = f"session-{self.id}"
        self.lock_conflict: str | None = None
        try:
            acquire(self.workspace.root, self.lock_owner)
        except LockError as exc:
            self.lock_conflict = str(exc)
            notice = (f"⚠ This project is already open in {exc.holder()} — "
                      "edits are refused until that session closes or the lock goes stale.")
            self.transcript.append({"kind": "notice", "text": notice})
            state.broadcast({"type": "notice", "text": notice, "session": self.id})
        except OSError:
            pass  # cannot create the lock file (e.g. read-only fs): locking is off

        permissions = PermissionManager(
            mode=data.get("mode") or state.mode,
            # note: read self.permissions inside the lambda (call time), not as a
            # default arg - it doesn't exist yet while we are constructing it
            asker=lambda prompt, sid=self.id: state._ask_via_gui(prompt, sid, self.permissions),
        )
        permissions.grants = state.shared_grants  # one grant set for the whole app
        self.permissions = permissions

        self.agent = Agent(
            provider, model, self.workspace, permissions,
            on_event=lambda kind, payload: state._on_agent_event(self, kind, payload),
            context=build_context(self.workspace), mcp=state.mcp,
            max_context_tokens=provider_cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS,
            session_id=self.id,
            attribution=state.config.data.get("attribution", True),
            lock_owner=self.lock_owner,
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

    def transcript_snapshot(self) -> list[dict]:
        """Transcript including any assistant text still streaming, so
        switching to a running session shows its partial output."""
        snapshot = list(self.transcript)
        if self._text_buffer:
            snapshot.append({"kind": "assistant", "text": "".join(self._text_buffer)})
        return snapshot

    def usage_dict(self) -> dict:
        return {
            "prompt_tokens": self.agent.usage.prompt_tokens,
            "completion_tokens": self.agent.usage.completion_tokens,
            "total_tokens": self.agent.usage.total_tokens,
        }


class GuiState:
    def __init__(self, path: str, model_spec: str | None, mode: str,
                 grants: list[str] | None = None, use_sandbox: bool = False,
                 auto_push: bool = False, instance: str | None = None,
                 remote: str | None = None):
        self.auto_push = auto_push
        if auto_push:
            grants = list(grants or []) + ["push"]
        # Address this daemon listens on (host:port); sessions it creates are
        # tagged with it so `silkcode sessions` can tell instances apart when
        # several daemons run on the same machine.
        self.instance = instance
        self.config = Config.load()
        self.remote_spec = remote
        if remote:
            # remote-workspace mode: the repo lives entirely in the sandbox;
            # this machine only ever sees the browser tab.
            from ..remotews import RemoteWorkspace
            from ..execbackend import remote_backend_from_config
            backend = remote_backend_from_config(self.config.data)
            if backend is None:
                raise ToolError("no sandbox configured; run 'silkcode sandbox connect <url>'")
            backend.health()
            self.workspace = RemoteWorkspace(backend, remote)
        else:
            self.workspace = Workspace(path)
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
        self.mcp = None
        mcp_servers = self.config.data.get("mcp_servers") or {}
        if mcp_servers:
            from ..mcp import McpManager
            self.mcp = McpManager(mcp_servers)

        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.pending: dict[str, dict] = {}
        self.sessions: dict[int, AgentSession] = {}
        self.swarms: dict[int, dict] = {}  # session id -> swarm status
        self._restart_args: list[str] | None = None
        self._restarting = False
        self._restore_active_session()
        if not self.sessions:
            self.new_session()

    # ---- sessions ----------------------------------------------------------

    def _restore_active_session(self) -> None:
        """Reopen the session that was active in this daemon instance before it
        stopped/restarted, so a self-update restart does not collapse the view
        to a fresh empty conversation. The session is loaded from the store; if
        it no longer exists (e.g. the project moved), a fresh default session
        is created instead."""
        try:
            sid = self.store.active_session(self.instance)
        except Exception:
            return
        if sid is None or sid in self.sessions:
            return
        try:
            session = self.get_session(sid)
        except (FileNotFoundError, Exception):
            return  # keep the fresh default session
        self.sessions[session.id] = session
        self.default_session_id = session.id
        self.broadcast({"type": "reload", "session": session.id})

    def save_all_sessions(self) -> None:
        """Persist every open session. Called before a self-update re-exec so
        the restarted daemon can reopen the previously-active session."""
        for session in self.sessions.values():
            try:
                self._save_session(session)
            except Exception:
                pass  # best-effort; the store may be read-only

    def _mark_active(self, session_id: int | None = None) -> None:
        self.store.set_active(self.instance, session_id if session_id is not None
                              else self.default_session_id)

    def _resolve_workspace(self, cwd: str) -> Workspace:
        """Resolve a session's working directory to a Workspace. The default
        workspace is used for the very first session; a new session may point
        at another project (see new_session / /api/session/new)."""
        if self.remote_spec:
            return self.workspace  # remote mode: everything runs in the sandbox
        p = Path(cwd).expanduser().resolve()
        if str(p) in (str(self.workspace.root), ""):
            return self.workspace
        ws = Workspace(str(p))
        if getattr(self.workspace, "exec_backend", None) is not None:
            ws.exec_backend = self.workspace.exec_backend
        return ws

    def new_session(self, project: str | None = None) -> AgentSession:
        """Create a session. `project` is a 'github:owner/repo' spec or a local
        directory; when None the session uses the app's default workspace. In
        remote-workspace mode every session uses the sandbox clone."""
        # unsaved open sessions also occupy their ids, not just those on disk
        session_id = max([self.store.new_id()] + [sid + 1 for sid in self.sessions])
        ws = self.workspace
        if project and not self.remote_spec:
            choice = resolve_project(project)
            ws = choice.workspace
            record_recent_project(choice.kind, project, choice.label)
        data = new_session(session_id, title="", model=self.spec,
                           cwd=str(ws.root), mode=self.mode, instance=self.instance)
        session = AgentSession(self, data)
        self.sessions[session.id] = session
        self.default_session_id = session.id
        self._mark_active(session.id)
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
        self._mark_active(session.id)
        self.broadcast({"type": "reload", "session": session.id})
        return session

    def sessions_summary(self) -> list[dict]:
        loaded = {
            s.id: {"id": s.id, "title": s.data.get("title", ""), "model": s.spec,
                   "cwd": str(s.workspace.root), "running": s.running, "open": True,
                   "instance": s.data.get("instance"),
                   "lock_conflict": s.lock_conflict,
                   "lock": lock_state(s.workspace.root)}
            for s in self.sessions.values()
        }
        for saved in self.store.list():
            if saved["id"] not in loaded:
                loaded[saved["id"]] = {"id": saved["id"], "title": saved["title"],
                                       "model": saved["model"], "cwd": saved.get("cwd", ""),
                                       "running": False, "open": False,
                                       "instance": saved.get("instance")}
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

    def release_all_locks(self) -> None:
        """Release the advisory workspace lock of every open session (shutdown)."""
        for session in self.sessions.values():
            try:
                release(session.workspace.root, session.lock_owner)
            except Exception:
                pass

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

    def _ask_via_gui(self, prompt: str, session_id: int | None = None,
                     pm: "PermissionManager | None" = None) -> str:
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        self.pending[req_id] = {"event": ev, "decision": "no", "prompt": prompt, "pm": pm}
        self.broadcast({"type": "permission_request", "id": req_id, "prompt": prompt,
                        "session": session_id})
        ev.wait(PERMISSION_TIMEOUT)
        return self.pending.pop(req_id)["decision"]

    def answer_permission(self, req_id: str, decision: str) -> bool:
        entry = self.pending.get(req_id)
        if entry is None:
            return False
        if decision == "all":
            # "Yes to all": approve this request and every later one this session.
            entry["decision"] = "yes"
            pm = entry.get("pm")
            if pm is not None:
                pm.allow_all()
        elif decision not in ("yes", "no", "always"):
            decision = "no"
        else:
            entry["decision"] = decision
        entry["event"].set()
        return True

    # ---- agent turns ------------------------------------------------------

    def start_turn(self, text: str, session_id: int | None = None) -> bool:
        session = self.get_session(session_id)
        with self.lock:
            if session.running:
                return False
            if self.swarms.get(session.id, {}).get("running"):
                return False  # the swarm owns this workspace right now
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
                    pushed = push_if_needed(session.workspace)
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
        # record the resolved provider/model (as the CLI does), so a bare spec
        # like "deepseek" doesn't merge different models in the usage view
        session.data["model"] = (session.spec if "/" in session.spec
                                 else f"{session.provider_name}/{session.model}")
        session.data["mode"] = session.permissions.mode
        session.data["usage"] = {
            "prompt_tokens": session.agent.usage.prompt_tokens,
            "completion_tokens": session.agent.usage.completion_tokens,
        }
        self.store.save(session.data)

    # ---- queries / mutations ----------------------------------------------

    def state(self, session_id: int | None = None) -> dict:
        from .. import __version__
        session = self.get_session(session_id)
        return {
            "version": __version__,
            "model": f"{session.provider_name}/{session.model}",
            "spec": session.spec,
            "mode": session.permissions.mode,
            "cwd": str(session.workspace.root),
            "session_id": session.id,
            "running": session.running,
            "swarm_running": bool(self.swarms.get(session.id, {}).get("running")),
            "auto_push": self.auto_push,
            "usage": session.usage_dict(),
            "sessions": self.sessions_summary(),
            "lock_conflict": session.lock_conflict,
            "lock": lock_state(session.workspace.root),
        }

    def stop(self, session_id: int | None = None) -> None:
        self.get_session(session_id).agent.request_stop()

    def close_session(self, session_id: int | None = None) -> dict:
        """Stop and close a session: release its workspace lock and drop it from
        memory so another session can take over the project. The conversation
        stays saved on disk and can be resumed later. Closing the last open
        session leaves the daemon with a fresh session on its project (the
        daemon always has a current session)."""
        sid = session_id if session_id is not None else self.default_session_id
        session = self.sessions.get(sid)
        if session is None:
            raise ToolError(f"session #{sid} is not open in this daemon")
        if self.swarms.get(sid, {}).get("running"):
            raise ToolError("a swarm is running in this session; stop it first")
        if session.running:
            session.agent.request_stop()
            deadline = time.time() + 8.0
            while time.time() < deadline and session.running:
                time.sleep(0.05)  # let the running turn wind down
        try:
            release(session.workspace.root, session.lock_owner)
        except Exception:
            pass  # releasing is best-effort; the lock goes stale on its own
        del self.sessions[sid]
        if self.default_session_id == sid:
            self.default_session_id = max(self.sessions) if self.sessions else self.new_session().id
        self._mark_active(self.default_session_id)
        self.broadcast({"type": "session_closed", "session": sid})
        return {"ok": True, "session_id": self.default_session_id}

    def takeover_lock(self, session_id: int | None = None) -> dict:
        """Re-try acquiring the session's workspace lock. Succeeds when the
        previous owner's process is dead (liveness check) or the lock went
        stale; raises ToolError when another live session still holds it."""
        session = self.get_session(session_id)
        try:
            acquire(session.workspace.root, session.lock_owner)
        except LockError as exc:
            raise ToolError(str(exc)) from exc
        session.lock_conflict = None
        notice = "Workspace lock taken over — edits are enabled again."
        session.transcript.append({"kind": "notice", "text": notice})
        self.broadcast({"type": "notice", "text": notice, "session": session.id})
        return {"ok": True, "lock_conflict": None}

    def switch_model(self, spec: str, session_id: int | None = None) -> None:
        session = self.get_session(session_id)
        provider_name, provider_cfg, model = self.config.resolve_model(spec)
        provider = build_provider(provider_name, provider_cfg,
                                  api_key=self.config.api_key_for(provider_cfg))
        session.agent.provider = provider
        session.agent.model = model
        session.provider_name, session.spec = provider_name, spec
        session.data["model"] = spec
        self._mark_active(session.id)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for session in self.sessions.values():
            session.permissions.mode = mode

    # ---- improvement swarm (silkcode.swarm) --------------------------------

    def swarm_status(self, session_id: int | None = None) -> dict:
        """Public swarm status for a session (never includes internal fields)."""
        session = self.get_session(session_id)
        status = self.swarms.get(session.id)
        if not status:
            return {"running": False, "log": [], "result": None, "error": None}
        return {
            "running": status["running"],
            "log": list(status["log"]),
            "result": status["result"],
            "error": status["error"],
        }

    def start_swarm(self, params: dict, session_id: int | None = None) -> dict:
        """Launch the tester/critic/worker swarm on a session's workspace.

        Runs in a background thread; progress is streamed to the browser via
        SSE events (swarm_progress / swarm_done / swarm_error)."""
        session = self.get_session(session_id)
        with self.lock:
            if session.running:
                raise ToolError("the agent is running in this session; stop it first")
            if self.swarms.get(session.id, {}).get("running"):
                raise ToolError("a swarm is already running in this session")

        worker_spec = str(params.get("model") or session.spec or self.spec)
        critic_spec = str(params["critic_model"]).strip() if params.get("critic_model") else None
        tester_spec = str(params["tester_model"]).strip() if params.get("tester_model") else None
        for spec in (worker_spec, critic_spec, tester_spec):
            if spec:
                self.config.resolve_model(spec)  # validate before starting
        try:
            target = float(params.get("target") or 10.0)
            max_iterations = int(params.get("max_iterations") or 0)
            stall_limit = int(params.get("stall_limit") or 3)
            max_tokens = int(params.get("max_tokens") or 0)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"bad swarm option: {exc}") from exc
        test_command = str(params.get("test_command") or "").strip() or None

        status = {
            "running": True,
            "session": session.id,
            "started": time.time(),
            "log": [],
            "result": None,
            "error": None,
            "_stop": threading.Event(),
        }
        self.swarms[session.id] = status

        def run() -> None:
            from ..swarm import run_swarm

            def on_progress(line: str) -> None:
                status["log"].append(line)
                self.broadcast({"type": "swarm_progress", "session": session.id, "line": line})

            def on_event(kind: str, data: dict) -> None:
                if kind == "score":
                    self.broadcast({"type": "swarm_score", "session": session.id, **data})
                elif kind == "phase":
                    self.broadcast({"type": "swarm_phase", "session": session.id,
                                    "role": data.get("role")})

            try:
                result = run_swarm(
                    session.workspace,
                    worker_spec=worker_spec,
                    critic_spec=critic_spec,
                    tester_spec=tester_spec,
                    target=target,
                    max_iterations=max_iterations,
                    stall_limit=stall_limit,
                    max_tokens=max_tokens,
                    test_command=test_command,
                    on_progress=on_progress,
                    on_event=on_event,
                    should_stop=lambda: status["_stop"].is_set(),
                    # the worker shares the session's permission manager, so it
                    # asks the user (same modal) and honors "yes to all"; it
                    # also writes under the session's workspace lock
                    worker_permissions=session.permissions,
                    worker_owner=session.lock_owner,
                )
                status["result"] = asdict(result)
                self.broadcast({"type": "swarm_done", "session": session.id,
                                "result": status["result"]})
            except Exception as exc:  # keep the daemon alive, report to the UI
                status["error"] = str(exc)
                self.broadcast({"type": "swarm_error", "session": session.id,
                                "message": str(exc)})
            finally:
                status["running"] = False
                status["_stop"] = None

        threading.Thread(target=run, daemon=True).start()
        return self.swarm_status(session.id)

    def stop_swarm(self, session_id: int | None = None) -> dict:
        session = self.get_session(session_id)
        status = self.swarms.get(session.id)
        if status and status.get("running"):
            stop_event = status.get("_stop")
            if stop_event is not None:
                stop_event.set()
            return {"ok": True}
        return {"ok": False, "error": "no swarm running in this session"}

    # ---- self-update / hot reload -----------------------------------------

    def start_auto_reload(self, restart_args: list[str] | None, interval: float = 3.0) -> bool:
        """Watch the installed checkout's HEAD; when new code lands (a pull
        by `silkcode update`, the Update button, or the swarm editing Silk
        Code itself) and the daemon is idle, re-exec with the same args so
        the new code goes live without a manual restart.

        Only active when the package is a git checkout and restart args were
        provided (i.e. the real `silkcode gui` daemon, not tests)."""
        if not restart_args:
            return False
        from ..update import git_repo_root, head_changed, head_commit, restart_argv
        repo = git_repo_root()
        if repo is None:
            return False
        baseline = head_commit(repo)

        def busy() -> bool:
            with self.lock:
                if any(s.running for s in self.sessions.values()):
                    return True
                return any(sw.get("running") for sw in self.swarms.values())

        def loop() -> None:
            while not self._restarting:
                time.sleep(interval)
                if self._restarting:
                    break
                try:
                    if not head_changed(repo, baseline):
                        continue
                    if busy():
                        continue  # let the agent/swarm finish first
                except Exception:
                    continue
                self._restarting = True
                self.save_all_sessions()  # reopen the active session after re-exec
                self.broadcast({"type": "restarting"})
                time.sleep(0.5)  # let the browser see the event
                os.execv(sys.executable, restart_argv(self._restart_args or []))

        threading.Thread(target=loop, daemon=True).start()
        return True

    def update_service(self, params: dict | None = None) -> dict:
        """Pull the latest Silk Code from git (fast-forward only).

        The running daemon then notices the new HEAD and restarts itself
        with the new code (see start_auto_reload). Refuses while a swarm is
        running so an in-flight run is never interrupted."""
        from ..update import git_repo_root, update_installation
        repo = git_repo_root()
        if repo is None:
            raise ToolError("not a git checkout; update with: pip install -U silkcode")
        with self.lock:
            if any(sw.get("running") for sw in self.swarms.values()):
                raise ToolError("a swarm is running in this session; stop it before updating")
        branch = str(params.get("branch") or "").strip() or None if params else None

        def on_progress(line: str) -> None:
            self.broadcast({"type": "update_progress", "line": line})

        result = update_installation(repo=repo, branch=branch, on_progress=on_progress)
        return result

    # ---- GitHub authorization (SRS sections 30-31, 60) ---------------------

    def github_status(self, session_id: int | None = None) -> dict:
        from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, get_token
        from ..github_oauth import client_id_from
        github_cfg = self.config.data.get("github") or {}
        workspace = self.get_session(session_id).workspace
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
            owner, repo = detect_repo(workspace)
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

    # ---- environment: credentials, usage, live sessions (SRS 19, 48) -------

    def live_sessions(self) -> list[dict]:
        """Sessions open in this daemon right now, with their live spend."""
        out = []
        for session in self.sessions.values():
            out.append({
                "id": session.id,
                "title": session.data.get("title", "") or "(new)",
                "model": f"{session.provider_name}/{session.model}",
                "project": str(session.workspace.root),
                "running": session.running,
                "swarm": bool((self.swarms.get(session.id) or {}).get("running")),
                "locked_out": bool(session.lock_conflict),
                "usage": session.usage_dict(),
            })
        return sorted(out, key=lambda s: s["id"])

    def environment(self) -> dict:
        from ..environment import overview
        data = overview(self.config, self.store, self.live_sessions())
        data["instance"] = self.instance
        data["running_count"] = sum(1 for s in data["live"] if s["running"])
        return data

    def set_provider_key(self, provider: str, key: str) -> dict:
        from ..environment import set_key
        try:
            set_key(self.config, provider, key)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return self.environment()

    def clear_provider_key(self, provider: str) -> dict:
        from ..environment import clear_key
        try:
            clear_key(self.config, provider)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return self.environment()

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

    def tree(self, session_id: int | None = None) -> list[dict]:
        from ..remotews import RemoteWorkspace
        workspace = self.get_session(session_id).workspace
        entries: list[dict] = []
        if isinstance(workspace, RemoteWorkspace):
            # remote: build the tree from the sandbox file listing
            for f in workspace.list_files():
                if len(entries) >= MAX_TREE_ENTRIES:
                    break
                parts = Path(f).parts
                if any(p in IGNORED_DIRS or p.endswith(".egg-info") for p in parts):
                    continue
                entries.append({"path": f, "dir": False, "depth": len(parts) - 1})
                for i in range(1, len(parts)):
                    prefix = "/".join(parts[:i])
                    if not any(e["path"] == prefix and e["dir"] for e in entries):
                        entries.append({"path": prefix, "dir": True, "depth": i - 1})
            return entries

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
                entries.append({"path": workspace.relative(child), "dir": child.is_dir(),
                                "depth": depth})
                if child.is_dir():
                    walk(child, depth + 1)

        walk(workspace.root, 0)
        return entries

    def read_file(self, rel_path: str, session_id: int | None = None) -> dict:
        from ..remotews import RemoteWorkspace
        workspace = self.get_session(session_id).workspace
        if isinstance(workspace, RemoteWorkspace):
            rel = rel_path.lstrip("/")
            if not workspace.is_file(rel):
                raise ToolError(f"Not a file: {rel_path}")
            return {"path": rel_path, "content": workspace.read_text(rel)}
        p = workspace.resolve(rel_path)
        if not p.is_file():
            raise ToolError(f"Not a file: {rel_path}")
        if p.stat().st_size > 1_000_000:
            return {"path": rel_path, "content": "(file too large to display)"}
        return {"path": rel_path, "content": p.read_text(errors="replace")}

    def diff(self, session_id: int | None = None) -> dict:
        workspace = self.get_session(session_id).workspace
        return {"diff": git_diff(workspace), "status": git_status(workspace)}

    def projects_info(self) -> list[dict]:
        from ..project import available_projects
        return available_projects()


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.1.1"}


def is_loopback(host: str) -> bool:
    """Whether `host` addresses this machine and only this machine.

    Must be decided on the parsed address, not the spelling. Matching a
    "127." prefix as text also accepts host *names* that merely begin that
    way, and an attacker can register one - 127.0.0.1.evil.example, the
    trick nip.io and sslip.io are built on - that resolves straight to
    loopback. Origin and Host then agree, and a name-based check would wave
    the request through, which is precisely the DNS-rebinding case the Host
    check exists to stop.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a name other than localhost: not something we serve


def _hostname(host_header: str) -> str:
    """The hostname part of a Host header, without the port ('[::1]:80' -> '::1')."""
    host = host_header.strip()
    if host.startswith("["):  # bracketed IPv6 literal
        return host[1:].partition("]")[0]
    return host.rpartition(":")[0] if ":" in host else host


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState = None  # type: ignore[assignment]
    html: bytes = b""
    # Shared secret required on every request when the daemon is reachable
    # beyond loopback. None (loopback only) keeps the local dev-server model.
    token: str | None = None

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        pass

    def _presented_token(self) -> str:
        """The token from the header, the cookie, or the query string."""
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "silk_token":
                return value
        return (parse_qs(urlparse(self.path).query).get("token") or [""])[0]

    def _same_origin(self) -> bool:
        """Reject requests a browser made on behalf of another site.

        A loopback daemon needs no token, which leaves it open to any page the
        user happens to visit: a cross-site POST with Content-Type text/plain
        is a CORS "simple request", so the browser sends it without a
        preflight, and the body is JSON either way. That is enough to start an
        agent turn - in agent mode, arbitrary code execution from a web page.
        The attacker cannot read the reply, but the side effect is the attack.

        Browsers always attach Origin to cross-origin requests, so an Origin
        that disagrees with the Host we were reached on is exactly that case.
        Non-browser clients (curl, the CLI, tests) send no Origin and are
        unaffected - they carry no ambient credentials to abuse.

        Checking Origin against Host rather than a fixed address keeps every
        legitimate way in working: 127.0.0.1, localhost, or a LAN IP all match
        themselves. DNS rebinding defeats that equality - the attacker
        controls the name, so Origin and Host agree - so the Host itself must
        also resolve to somewhere we would serve. Beyond loopback that is what
        the token is for, and it is already required.
        """
        origin = self.headers.get("Origin", "").strip()
        host_header = self.headers.get("Host", "").strip()
        if origin and origin.lower() != "null":
            parsed = urlparse(origin)
            if parsed.netloc.lower() != host_header.lower():
                return False
        # Rebinding guard: a loopback-only daemon may only be addressed by a
        # loopback name. With a token in play the token is the gate.
        if not self.token and host_header and not is_loopback(_hostname(host_header)):
            return False
        return True

    def _authorized(self) -> bool:
        """No token configured (loopback) means no token check; otherwise every
        request must carry it, compared in constant time. Either way the
        request must not have been made on another site's behalf."""
        if not self._same_origin():
            return False
        if not self.token:
            return True
        import hmac
        presented = self._presented_token()
        return bool(presented) and hmac.compare_digest(presented, self.token)

    def _deny(self) -> None:
        if not self._same_origin():
            body = (b"Forbidden. This request carries another site's origin, or "
                    b"reached this daemon under a host name it does not serve, "
                    b"so it was not made by the Silk Code page.")
            status = 403
        else:
            body = (b"Unauthorized. This Silk Code daemon is reachable beyond this "
                    b"machine and requires its access token; open the URL printed "
                    b"when it started.")
            status = 401
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The client went away before we could reply (e.g. it double-fired
            # a request and closed the connection). Nothing to deliver; just
            # let the request handler finish quietly instead of crashing the
            # server thread with a traceback.
            pass

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

    def handle_one_request(self):  # noqa: D102 - see BaseHTTPRequestHandler
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The client vanished mid-request (RST before the request line,
            # RST while uploading the body, or a write to a dead socket).
            # BaseHTTPRequestHandler only swallows TimeoutError here, so any
            # other socket error would escape and make socketserver print an
            # "Exception occurred during processing of request" traceback.
            # Just close the connection quietly instead.
            self.close_connection = True

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            return self._deny()
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        st = self.state
        sid = self._session_of(params)
        try:
            if route == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")  # never serve a stale UI
                if self.token:
                    # the page was opened with ?token=...; keep it for the
                    # fetches and the event stream that follow
                    self.send_header(
                        "Set-Cookie",
                        f"silk_token={self.token}; Path=/; SameSite=Strict; HttpOnly")
                self.send_header("Content-Length", str(len(self.html)))
                self.end_headers()
                self.wfile.write(self.html)
            elif route == "/api/state":
                self._json(st.state(sid))
            elif route == "/api/transcript":
                self._json(st.get_session(sid).transcript_snapshot())
            elif route == "/api/tree":
                self._json(st.tree(sid))
            elif route == "/api/file":
                path = (params.get("path") or [""])[0]
                self._json(st.read_file(path, sid))
            elif route == "/api/diff":
                self._json(st.diff(sid))
            elif route == "/api/projects":
                self._json(st.projects_info())
            elif route == "/api/providers":
                self._json(st.providers_info())
            elif route == "/api/sessions":
                self._json(st.sessions_summary())
            elif route == "/api/environment":
                self._json(st.environment())
            elif route == "/api/github/status":
                self._json(st.github_status(sid))
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
        if not self._authorized():
            return self._deny()
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
                self._json({"result": git_push(st.get_session(sid).workspace)})
            elif route == "/api/autopush":
                st.auto_push = bool(body.get("enabled"))
                if st.auto_push:
                    st.shared_grants.add("push")
                self._json(st.state(sid))
            elif route == "/api/stop":
                st.stop(sid)
                self._json({"ok": True})
            elif route == "/api/session/new":
                project = str(body.get("project", "")).strip() or None
                session = st.new_session(project=project)
                self._json(st.state(session.id))
            elif route == "/api/session":
                session = st.load_session(int(body.get("id", 0)))
                self._json(st.state(session.id))
            elif route == "/api/session/close":
                self._json(st.close_session(sid))
            elif route == "/api/lock/takeover":
                self._json(st.takeover_lock(sid))
            elif route == "/api/swarm/start":
                self._json(st.start_swarm(body, sid))
            elif route == "/api/swarm/stop":
                self._json(st.stop_swarm(sid))
            elif route == "/api/swarm/status":
                self._json(st.swarm_status(sid))
            elif route == "/api/update":
                self._json(st.update_service(body))
            elif route == "/api/environment/key":
                provider = str(body.get("provider", ""))
                if body.get("clear"):
                    self._json(st.clear_provider_key(provider))
                else:
                    self._json(st.set_provider_key(provider, str(body.get("key", ""))))
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
            use_sandbox: bool = False, auto_push: bool = False,
            restart_args: list[str] | None = None, token: str | None = None,
            remote: str | None = None) -> int:
    # Bind first so we know the real address (--port 0 picks an ephemeral one)
    # and can label this daemon's sessions with it; a busy address fails fast
    # with a clear message before any state is created.
    try:
        server = ThreadingHTTPServer((host, port), GuiHandler)
    except OSError as exc:
        print(f"error: cannot listen on {host}:{port} ({exc}).")
        print("Another Silk Code GUI may already be running on this address - every")
        print("instance needs its own address. Start this one on another port:")
        print(f"    silkcode gui --port {port + 1} <path>")
        return 1
    bound_host, bound_port = server.server_address[:2]
    try:
        state = GuiState(path, model_spec, mode, grants=grants, use_sandbox=use_sandbox,
                         auto_push=auto_push, instance=f"{bound_host}:{bound_port}",
                         remote=remote)
    except (ToolError, ConfigError) as exc:
        print(f"error: {exc}")
        server.server_close()
        return 1
    GuiHandler.state = state
    GuiHandler.html = (Path(__file__).parent / "app.html").read_bytes()
    # The daemon drives an agent that reads and writes files, runs commands and
    # holds API keys. On loopback that is the usual local-dev trust model; the
    # moment it is reachable from elsewhere it needs a credential, so one is
    # required (and generated when not supplied).
    if not is_loopback(host) and not token:
        import secrets
        token = secrets.token_urlsafe(24)
        print("This daemon is reachable beyond this machine, so it requires an "
              "access token.\nOpen the URL below (it carries the token); share it "
              "with nobody you would not\ngive a shell on this machine.\n")
    GuiHandler.token = token
    if restart_args:
        state.start_auto_reload(restart_args)
    display_host = "localhost" if bound_host in ("0.0.0.0", "::") else bound_host
    url = f"http://{display_host}:{bound_port}" + (f"/?token={token}" if token else "")
    print(f"Silk Code GUI: {url}")
    print(f"workspace: {state.workspace.root}")
    first = state.get_session()
    print(f"model: {first.provider_name}/{first.model}   session: #{first.id}")
    print(f"instance: {state.instance}   (run another on a different --host/--port "
          "for more sessions on this machine)")
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
        state.release_all_locks()
    return 0
