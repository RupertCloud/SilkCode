"""Local GUI server: the Silk Code daemon plus a browser front end.

Serves the GUI (SRS section 9) and exposes the agent over a small JSON/SSE API
(SRS 68-69). Several sessions can be open at once (SRS 44), each with its own
agent, conversation, checkpoints and project; events carry their session id.
Sessions are shared with the CLI (`silkcode resume <id>`, SRS 47).

Several daemons can run on one machine, each on its own --host/--port. Session
ids are allocated atomically across processes (silkcode.sessions) and tagged
with the creating instance; a project open in two instances is read-only in
the second until the per-workspace lock is released.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..tools.images import IMAGE_MARKER, IMAGE_SUFFIXES

from ..agent import Agent
from ..agent.loop import DEFAULT_CONTEXT_TOKENS
from ..config import Config, ConfigError, config_dir
from ..connections import ConnectionMonitor
from ..context import assemble
from ..lock import LockError, acquire, lock_state, release
from ..permissions import PermissionManager
from ..providers import build_provider
from ..providers.base import AuthError, ProviderError
from ..lightmodel import checkpoint_summarizer as _checkpoint_summarizer
from ..project import record_recent_project, remember_workspace, resolve_project
from ..repomap import IGNORED_DIRS
from ..sessions import SessionStore, fork_session, new_session
from ..tools.git import git_diff, git_status
from ..workspace import ToolError, Workspace

MAX_TREE_ENTRIES = 2000
PERMISSION_TIMEOUT = 600  # seconds; deny if the browser never answers
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}


def _flag(params: dict, name: str) -> bool:
    """A query flag: ?all=1, ?all=true, or a bare ?all."""
    if name not in params:
        return False
    return (params[name] or [""])[0].strip().lower() in ("", "1", "true", "yes", "on")


def _normalized(path: str) -> str:
    """A directory in a comparable form. Sessions record whatever spelling they
    were opened with (~/app, /home/me/app/), and a deleted project's sessions
    must still group together, so the directory need not exist."""
    if not path:
        return ""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path)).rstrip("/\\")


def _same_project(cwd: str | None, project: str) -> bool:
    """A session with no recorded directory predates per-project workspaces;
    showing it in the wrong list beats losing it from every list."""
    return not cwd or _normalized(cwd) == _normalized(project)


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _transcript_from_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "user":
            out.append({"kind": "user", "text": m.get("content", "")})
        elif m.get("role") == "assistant":
            if m.get("content"):
                out.append({"kind": "assistant", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                out.append({"kind": "tool", "name": fn.get("name", "?"),
                            "args": _clip(fn.get("arguments", ""))})
        elif m.get("role") == "tool" and str(m.get("content", "")).startswith(IMAGE_MARKER):
            out.append({"kind": "image",
                        "path": str(m["content"]).splitlines()[0][len(IMAGE_MARKER):]})
    return out


def _lock_notice(holder: str) -> str:
    return (f"⚠ This project is already open in {holder} — "
            "edits are refused until that session closes or the lock goes stale.")


class AgentSession:
    """One conversation: its own agent, transcript, checkpoints and project."""

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

        self.workspace = state._resolve_workspace(data.get("cwd") or str(state.workspace.root))
        launcher = state.is_launcher_workspace(self.workspace)
        if not launcher:
            remember_workspace(self.workspace.root)

        # Advisory per-workspace lock: one writer per project. Notices are
        # emitted after the transcript is built, or resuming would drop them.
        self.lock_owner = f"session-{self.id}"
        self.lock_conflict: str | None = None
        notices: list[str] = []
        if not launcher:
            try:
                acquire(self.workspace.root, self.lock_owner)
            except LockError as exc:
                self.lock_conflict = str(exc)
                notices.append(_lock_notice(exc.holder()))
            except OSError:
                pass  # cannot create the lock file (e.g. read-only fs): locking is off

        # read self.permissions at call time: it does not exist yet here
        self.permissions = PermissionManager(
            mode=data.get("mode") or state.mode,
            asker=lambda prompt, sid=self.id: state._ask_via_gui(prompt, sid, self.permissions))
        self.permissions.grants = state.shared_grants  # one grant set for the whole app

        project = assemble(self.workspace)
        notices.extend(project.warnings)
        self.agent = Agent(
            provider, model, self.workspace, self.permissions,
            on_event=lambda kind, payload: state._on_agent_event(self, kind, payload),
            context=project.text, mcp=state.mcp,
            max_context_tokens=provider_cfg.get("context_tokens") or DEFAULT_CONTEXT_TOKENS,
            session_id=self.id,
            attribution=state.config.data.get("attribution", True),
            lock_owner=self.lock_owner,
            summarizer=_checkpoint_summarizer(state.config),
        )
        if data.get("messages"):
            self.agent.messages = data["messages"]
            self.transcript = _transcript_from_messages(data["messages"])
        for notice in notices:
            state.notify(self, notice)
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
        """Including text still streaming, so a running session shows its output."""
        snapshot = list(self.transcript)
        if self._text_buffer:
            snapshot.append({"kind": "assistant", "text": "".join(self._text_buffer)})
        return snapshot

    def usage_dict(self) -> dict:
        u = self.agent.usage
        return {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens}


class GuiState:
    def __init__(self, path: str, model_spec: str | None, mode: str,
                 grants: list[str] | None = None, use_sandbox: bool = False,
                 auto_push: bool = False, instance: str | None = None,
                 remote: str | None = None, projectless: bool = False):
        self.projectless = bool(projectless and not remote)
        self.launcher_session_id: int | None = None
        self.auto_push = auto_push
        if auto_push:
            grants = list(grants or []) + ["push"]
        self.instance = instance  # host:port; tags sessions so instances are told apart
        self.connections = ConnectionMonitor()  # on the state: a handler is per request
        self.config = Config.load()
        self.remote_spec = remote
        backend = None
        if remote or use_sandbox:
            from ..execbackend import remote_backend_from_config
            backend = remote_backend_from_config(self.config.data)
            if backend is None:
                raise ToolError("no sandbox configured; run 'silkcode sandbox connect <url>'")
            backend.health()
        if remote:
            # the repo lives entirely in the sandbox; this machine only sees the tab
            from ..remotews import RemoteWorkspace
            self.workspace = RemoteWorkspace(backend, remote)
        else:
            self.workspace = Workspace(path)
            if backend is not None:
                self.workspace.exec_backend = backend
        self.store = SessionStore()
        self.spec = model_spec or self.config.default_model
        self.config.resolve_model(self.spec)  # validate early
        self.mode = mode
        self.shared_grants: set[str] = set(grants or [])
        self.mcp = None
        if self.config.data.get("mcp_servers"):
            from ..mcp import McpManager
            self.mcp = McpManager(self.config.data["mcp_servers"])

        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.pending: dict[str, dict] = {}
        self.sessions: dict[int, AgentSession] = {}
        # closed from the drawer: hidden for this run; files and conversations stay
        self.closed_projects: set[str] = set()
        self.swarms: dict[int, dict] = {}  # session id -> swarm status
        self._restart_args: list[str] | None = None
        self._restarting = False
        if not self.projectless:
            self._restore_active_session()
        if not self.sessions:
            self.new_session()
        if self.projectless:
            self.launcher_session_id = self.default_session_id

    def is_launcher_workspace(self, workspace) -> bool:
        return self.projectless and str(workspace.root) == str(self.workspace.root)

    def _swarm_running(self, session_id: int) -> bool:
        return bool(self.swarms.get(session_id, {}).get("running"))

    def _busy(self) -> bool:
        with self.lock:
            return (any(s.running for s in self.sessions.values())
                    or any(sw.get("running") for sw in self.swarms.values()))

    def notify(self, session: AgentSession, text: str) -> None:
        session.transcript.append({"kind": "notice", "text": text})
        self.broadcast({"type": "notice", "text": text, "session": session.id})

    # ---- sessions ----------------------------------------------------------

    def _restore_active_session(self) -> None:
        """Reopen this instance's active session after a restart, so a
        self-update does not collapse the view to an empty conversation."""
        try:
            sid = self.store.active_session(self.instance)
            if sid is None or sid in self.sessions:
                return
            session = self.get_session(sid)
        except Exception:
            return  # keep the fresh default session
        self.sessions[session.id] = session
        self.default_session_id = session.id
        self.broadcast({"type": "reload", "session": session.id})

    def save_all_sessions(self) -> None:
        """Persist every open session (before a self-update re-exec)."""
        for session in self.sessions.values():
            try:
                self._save_session(session)
            except Exception:
                pass  # best-effort; the store may be read-only

    def _activate(self, session: AgentSession) -> AgentSession:
        self.sessions[session.id] = session
        self.default_session_id = session.id
        self.store.set_active(self.instance, session.id)
        return session

    def _resolve_workspace(self, cwd: str) -> Workspace:
        if self.remote_spec:
            return self.workspace  # remote mode: everything runs in the sandbox
        p = Path(cwd).expanduser().resolve()
        if str(p) in (str(self.workspace.root), ""):
            return self.workspace
        ws = Workspace(str(p))
        if getattr(self.workspace, "exec_backend", None) is not None:
            ws.exec_backend = self.workspace.exec_backend
        return ws

    def _next_id(self) -> int:
        # unsaved open sessions also occupy their ids, not just those on disk
        return max([self.store.new_id()] + [sid + 1 for sid in self.sessions])

    def new_session(self, project: str | None = None) -> AgentSession:
        """`project` is a 'github:owner/repo' spec or a local directory; None
        uses the default workspace (always, in remote mode)."""
        ws = self.workspace
        if project and not self.remote_spec:
            choice = resolve_project(project)
            ws = choice.workspace
            record_recent_project(choice.kind, project, choice.label)
        data = new_session(self._next_id(), title="", model=self.spec,
                           cwd=str(ws.root), mode=self.mode, instance=self.instance)
        session = AgentSession(self, data)
        self.closed_projects.discard(_normalized(str(ws.root)))
        return self._activate(session)

    def get_session(self, session_id: int | None = None) -> AgentSession:
        if session_id is None:
            session_id = self.default_session_id
        session = self.sessions.get(session_id)
        if session is None:
            session = AgentSession(self, self.store.load(session_id))  # FileNotFoundError
            self.sessions[session_id] = session
        return session

    def load_session(self, session_id: int) -> AgentSession:
        session = self._activate(self.get_session(session_id))
        self.closed_projects.discard(_normalized(str(session.workspace.root)))
        self.broadcast({"type": "reload", "session": session.id})
        return session

    def fork_session(self, session_id: int | None = None, at_message: int | None = None,
                     isolated: bool = True) -> AgentSession:
        """A new session that continues this one's history while the original
        stays as it is. With `isolated` the fork gets its own git worktree on a
        silk/<stamp> branch; outside a git repository it forks in place."""
        parent = self.get_session(session_id)
        if parent.running:
            raise ToolError("cannot fork while the agent is running in this session")
        self._save_session(parent)
        data = fork_session(parent.data, self._next_id(), at_message=at_message,
                            instance=self.instance)
        notice = f"⑂ Forked from session #{parent.id}"
        if isolated and not self.remote_spec:
            from ..worktree import create as create_worktree
            try:
                wt = create_worktree(parent.workspace.root)
                data["cwd"] = str(wt.root)
                data["forked_from"]["worktree"] = {
                    "branch": wt.branch, "base": wt.base, "repo": str(wt.repo_root)}
                notice += (f" into an isolated worktree on {wt.branch}. The original checkout "
                           f"and session are untouched; merge this line back with: "
                           f"git merge {wt.branch}")
            except ToolError as exc:
                notice += f" in place ({exc})"
        session = AgentSession(self, data)
        session.transcript.append({"kind": "notice", "text": notice})
        self._activate(session)
        self._save_session(session)
        self.broadcast({"type": "reload", "session": session.id})
        return session

    def sessions_summary(self, session_id: int | None = None,
                         all_projects: bool = False) -> list[dict]:
        """Sessions for the open project, most recent first. Session files are
        per machine, so unscoped this listed every project's work.
        `all_projects=True` returns everything."""
        rows = {
            s.id: {"id": s.id, "title": s.data.get("title", ""), "model": s.spec,
                   "cwd": str(s.workspace.root), "running": s.running, "open": True,
                   "instance": s.data.get("instance"), "lock_conflict": s.lock_conflict,
                   "lock": lock_state(s.workspace.root)}
            for s in self.sessions.values()
        }
        for saved in self.store.list():
            rows.setdefault(saved["id"], {
                "id": saved["id"], "title": saved["title"], "model": saved["model"],
                "cwd": saved.get("cwd", ""), "running": False, "open": False,
                "instance": saved.get("instance")})
        ordered = sorted(rows.values(), key=lambda s: s["id"], reverse=True)
        if all_projects:
            return ordered
        here = self.project_root(session_id)
        # the session being viewed always belongs in its own list
        return [s for s in ordered if _same_project(s.get("cwd"), here) or s["id"] == session_id]

    def project_root(self, session_id: int | None = None) -> str:
        """The project the given session is open on, not the launch workspace."""
        try:
            return str(self.get_session(session_id).workspace.root)
        except (FileNotFoundError, ToolError):
            return str(self.workspace.root)

    def known_projects(self, session_id: int | None = None) -> list[dict]:
        """The current project, then every project with sessions, then recent
        local ones; one entry per resolved path."""
        if self.projectless:
            return []
        from ..project import recent_projects
        here = self.project_root(session_id)
        counts: dict[str, dict[str, int]] = {}
        summaries = self.sessions_summary(session_id, all_projects=True)
        for s in summaries:
            if s.get("cwd"):
                c = counts.setdefault(_normalized(s["cwd"]), {"sessions": 0, "running": 0, "open": 0})
                c["sessions"] += 1
                c["running"] += int(bool(s.get("running")))
                c["open"] += int(bool(s.get("open")))

        rows: list[dict] = []
        seen: set[str] = set()

        def add(path: str, label: str, current: bool = False) -> None:
            key = _normalized(path)
            if not path or key in seen or (not current and key in self.closed_projects):
                return
            seen.add(key)
            rows.append({"path": path, "label": label, "current": current,
                         **counts.get(key, {"sessions": 0, "running": 0, "open": 0})})

        add(here, Path(here).name or here, current=True)
        for s in summaries:
            root = s.get("cwd") or ""
            add(root, Path(root).name or root)
        for recent in recent_projects():
            if recent.get("kind") == "local":
                path = recent.get("spec") or ""
                add(path, recent.get("label") or path)
        return rows

    def other_projects(self, session_id: int | None = None) -> list[dict]:
        """Projects with sessions this project's list does not show."""
        here = self.project_root(session_id)
        counts: dict[str, int] = {}
        for s in self.sessions_summary(session_id, all_projects=True):
            cwd = s.get("cwd") or ""
            if cwd and not _same_project(cwd, here):
                counts[cwd] = counts.get(cwd, 0) + 1
        return [{"path": path, "label": Path(path).name or path, "count": n}
                for path, n in sorted(counts.items(), key=lambda kv: -kv[1])]

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
            entry = {"kind": "tool", "name": data["name"],
                     "args": _clip(json.dumps(data["args"], ensure_ascii=False))}
            session.transcript.append(entry)
            self.broadcast({"type": "tool_start", "session": session.id, **entry})
        elif kind == "tool_result":
            output = str(data["output"])
            if output.startswith(IMAGE_MARKER):
                entry = {"kind": "image", "path": output.splitlines()[0][len(IMAGE_MARKER):]}
                session.transcript.append(entry)
                self.broadcast({"type": "image", "session": session.id, **entry})
            first = output.splitlines()[0] if output else ""
            self.broadcast({"type": "tool_result", "name": data["name"],
                            "output": first[:200], "session": session.id})

    def _ask_via_gui(self, prompt: str, session_id: int | None = None,
                     pm: "PermissionManager | None" = None) -> str:
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        self.pending[req_id] = {"event": ev, "decision": "no", "prompt": prompt,
                                "pm": pm, "session": session_id}
        self.broadcast({"type": "permission_request", "id": req_id, "prompt": prompt,
                        "session": session_id})
        answered = ev.wait(PERMISSION_TIMEOUT)
        entry = self.pending.pop(req_id)
        if not answered:  # every browser got the request; tell them all it expired
            self.broadcast({"type": "permission_resolved", "id": req_id,
                            "decision": "no", "session": session_id})
        return entry["decision"]

    def answer_permission(self, req_id: str, decision: str) -> bool:
        entry = self.pending.get(req_id)
        if entry is None:
            return False
        if decision == "all":  # approve this and every later request this session
            entry["decision"] = "yes"
            if entry.get("pm") is not None:
                entry["pm"].allow_all()
        elif decision in ("yes", "no", "always"):
            entry["decision"] = decision
        entry["event"].set()
        # dismiss the matching dialog on every other connected client
        self.broadcast({"type": "permission_resolved", "id": req_id,
                        "decision": entry["decision"], "session": entry.get("session")})
        return True

    # ---- agent turns ------------------------------------------------------

    def start_turn(self, text: str, session_id: int | None = None) -> bool:
        session = self.get_session(session_id)
        with self.lock:
            if session.running or self._swarm_running(session.id):
                return False  # the agent or a swarm owns this workspace right now
            session.running = True
        if not session.data.get("title"):
            session.data["title"] = text[:60]
        session.transcript.append({"kind": "user", "text": text})
        self.broadcast({"type": "user", "text": text, "session": session.id})
        threading.Thread(target=self._run_turn, args=(session, text), daemon=True).start()
        return True

    def _run_turn(self, session: AgentSession, text: str) -> None:
        def report(event: dict, message: str) -> None:
            self.broadcast({**event, "session": session.id})
            session.transcript.append({"kind": "error", "text": message})
        try:
            session.agent.run_turn(text)
        except AuthError as exc:  # fixable in place: the page asks for the key
            report({"type": "key_needed", "message": str(exc), "provider": exc.provider_name,
                    "hint": exc.hint}, str(exc))
        except ProviderError as exc:
            report({"type": "error", "message": str(exc)}, str(exc))
        except Exception as exc:  # keep the daemon alive
            report({"type": "error", "message": f"{type(exc).__name__}: {exc}"}, str(exc))
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
        # the resolved provider/model, so a bare "deepseek" does not merge models in usage
        session.data["model"] = (session.spec if "/" in session.spec
                                 else f"{session.provider_name}/{session.model}")
        session.data["mode"] = session.permissions.mode
        session.data["usage"] = {"prompt_tokens": session.agent.usage.prompt_tokens,
                                 "completion_tokens": session.agent.usage.completion_tokens}
        self.store.save(session.data)

    # ---- queries / mutations ----------------------------------------------

    def state(self, session_id: int | None = None, all_projects: bool = False) -> dict:
        from ..version import build_id
        session = self.get_session(session_id)
        return {
            "version": build_id(),  # the build, so the page notices it is stale
            "model": f"{session.provider_name}/{session.model}",
            "spec": session.spec,
            "mode": session.permissions.mode,
            "cwd": str(session.workspace.root),
            "session_id": session.id,
            "running": session.running,
            "swarm_running": self._swarm_running(session.id),
            "auto_push": self.auto_push,
            "usage": session.usage_dict(),
            "sessions": self.sessions_summary(session.id, all_projects),
            "other_projects": self.other_projects(session.id),
            "projects": self.known_projects(session.id),
            "project": str(session.workspace.root),
            "lock_conflict": session.lock_conflict,
            "lock": lock_state(session.workspace.root),
            "projectless": self.projectless,
        }

    def stop(self, session_id: int | None = None) -> None:
        self.get_session(session_id).agent.request_stop()

    def _remember(self, choice, project: str) -> None:
        if choice.kind == "local":
            remember_workspace(choice.workspace.root)  # labelled by directory name
        else:
            record_recent_project(choice.kind, project, choice.label)

    def move_session(self, session_id: int | None, project: str) -> dict:
        """Point an open session at another project, keeping the conversation,
        as `/project` does in the REPL. The lock moves with it."""
        session = self.get_session(session_id)
        if session.running:
            raise ToolError("this session is mid-turn; stop it before switching project")
        if self._swarm_running(session.id):
            raise ToolError("a swarm is running in this session; stop it first")
        if self.remote_spec:
            raise ToolError("this daemon runs against a remote workspace; "
                            "projects cannot be switched from here")
        choice = resolve_project(project)
        self.closed_projects.discard(_normalized(str(choice.workspace.root)))
        if choice.workspace.root == session.workspace.root:
            return self.state(session.id)

        # release before acquiring, so A->B and B->A between two sessions cannot deadlock
        try:
            release(session.workspace.root, session.lock_owner)
        except OSError:
            pass
        session.workspace = choice.workspace
        session.lock_conflict = None
        try:
            acquire(session.workspace.root, session.lock_owner)
        except LockError as exc:
            session.lock_conflict = str(exc)
        except OSError:
            pass

        context = assemble(session.workspace)
        session.agent.set_workspace(session.workspace, context.text)
        session.data["cwd"] = str(session.workspace.root)
        self._save_session(session)
        self._remember(choice, project)
        notices = list(context.warnings)
        if session.lock_conflict:
            notices.append(_lock_notice(session.lock_conflict))
        session.transcript.extend({"kind": "notice", "text": n} for n in notices)
        self.broadcast({"type": "reload", "session": session.id})
        return self.state(session.id)

    def open_project(self, project: str) -> dict:
        """Activate a project's most recent conversation (open, then saved),
        creating one only when the project has no history."""
        if self.remote_spec:
            raise ToolError("this daemon runs against a remote workspace; projects cannot be switched")
        choice = resolve_project(project)
        target = _normalized(str(choice.workspace.root))
        launcher_id = self.launcher_session_id
        self.projectless = False
        self.launcher_session_id = None
        self.closed_projects.discard(target)
        candidates = [s["id"] for s in self.sessions_summary(all_projects=True)
                      if _normalized(s.get("cwd") or "") == target]
        session = self.load_session(max(candidates)) if candidates else self.new_session(project=project)
        if launcher_id is not None and launcher_id != session.id:
            self.sessions.pop(launcher_id, None)
        self._remember(choice, project)
        return self.state(session.id)

    def create_project(self, params: dict) -> dict:
        """Scaffold, git-initialize, optionally publish, and activate a project."""
        if self.remote_spec:
            raise ToolError("this daemon runs against a remote workspace; projects cannot be created")
        from ..scaffold import DEFAULT_TEMPLATE, create_project, validate_name

        name = str(params.get("name") or "").strip()
        parent = str(params.get("parent") or "").strip() or str(Path(self.project_root()).parent)
        template = str(params.get("template") or DEFAULT_TEMPLATE).strip()
        description = str(params.get("description") or "").strip()
        if not name:
            raise ToolError("project name must not be empty")
        objective = str(params.get("objective") or description).strip()
        if params.get("start_swarm") and not objective:
            raise ToolError("describe what the Swarm should build")
        publish = bool(params.get("publish_github"))
        visibility = str(params.get("github_visibility") or "private").lower()
        if visibility not in {"private", "public"}:
            raise ToolError("GitHub visibility must be private or public")
        github_client = None
        if publish:
            from ..github import cli_client_for_repos
            github_client = cli_client_for_repos()
            github_client.whoami()  # fail before creating local files
        slug = validate_name(name)
        result = create_project(name, template=template, parent=parent,
                                description=description, git=True)
        github = None
        if publish:
            if result.git != "initialized" or not result.commit or result.commit.startswith("git error"):
                raise ToolError("the local project was created, but Git could not make its initial "
                                "commit; configure your Git name/email before publishing")
            from ..tools.git import git_add_remote, git_push
            repository = github_client.create_repository(
                slug, description=description or objective, private=visibility == "private")
            remote_url = repository.get("clone_url") or f"https://github.com/{repository['full_name']}.git"
            remote = git_add_remote(Workspace(result.path), "origin", remote_url)
            pushed = remote if remote.startswith("git error") else git_push(Workspace(result.path))
            github = {**repository, "push": pushed}
        state = self.open_project(str(result.path))
        swarm = None
        if params.get("start_swarm"):
            swarm = self.start_swarm({
                "team_mode": True, "objective": objective,
                "developer_count": int(params.get("developer_count") or 0),
                "max_iterations": int(params.get("max_iterations") or 0),
                "max_tokens": int(params.get("max_tokens") or 0),
                "target": float(params.get("target") or 10),
            }, state["session_id"])
        return {"state": state, "project": str(result.path), "git": result.git,
                "commit": result.commit, "files": result.files,
                "test_command": result.test_command, "swarm": swarm, "github": github}

    def close_session(self, session_id: int | None = None) -> dict:
        """Stop a session, release its lock and drop it from memory; the
        conversation stays saved. The daemon always keeps a current session."""
        sid = session_id if session_id is not None else self.default_session_id
        session = self.sessions.get(sid)
        if session is None:
            raise ToolError(f"session #{sid} is not open in this daemon")
        if self._swarm_running(sid):
            raise ToolError("a swarm is running in this session; stop it first")
        if session.running:
            session.agent.request_stop()
            deadline = time.time() + 8.0
            while time.time() < deadline and session.running:
                time.sleep(0.05)  # let the running turn wind down
        try:
            release(session.workspace.root, session.lock_owner)
        except Exception:
            pass  # best-effort; the lock goes stale on its own
        project = str(session.workspace.root)
        del self.sessions[sid]
        if self.default_session_id == sid:
            same = [s.id for s in self.sessions.values() if _same_project(str(s.workspace.root), project)]
            self.default_session_id = max(same) if same else self.new_session(project=project).id
        self.store.set_active(self.instance, self.default_session_id)
        self.broadcast({"type": "session_closed", "session": sid})
        return {"ok": True, "session_id": self.default_session_id}

    def close_project(self, project: str, session_id: int | None = None) -> dict:
        """Close every open session of a non-current project and hide its card."""
        target = _normalized(project)
        if target == _normalized(self.project_root(session_id)):
            raise ToolError("switch to another project before closing this one")
        matches = [s.id for s in self.sessions.values() if _normalized(str(s.workspace.root)) == target]
        if any(self._swarm_running(sid) for sid in matches):
            raise ToolError("a swarm is running in this project; stop it first")
        for sid in matches:
            self.close_session(sid)
        self.closed_projects.add(target)
        return {"ok": True, "closed": len(matches), "session_id": self.default_session_id,
                "projects": self.known_projects(self.default_session_id)}

    def takeover_lock(self, session_id: int | None = None) -> dict:
        """Retry the workspace lock: succeeds when the owner is dead or stale."""
        session = self.get_session(session_id)
        try:
            acquire(session.workspace.root, session.lock_owner)
        except LockError as exc:
            raise ToolError(str(exc)) from exc
        session.lock_conflict = None
        self.notify(session, "Workspace lock taken over — edits are enabled again.")
        return {"ok": True, "lock_conflict": None}

    def set_api_key(self, provider_name: str, key: str, session_id: int | None = None) -> dict:
        """Store a key a key_needed prompt collected (config.json, 0600) and
        rebuild every open session on that provider. Never echoed or logged."""
        from ..environment import set_key
        try:
            set_key(self.config, provider_name, key)  # same path as the ⚙ page
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        cfg = self.config.providers[provider_name]
        updated = 0
        for session in self.sessions.values():
            if session.provider_name == provider_name:
                session.agent.provider = build_provider(
                    provider_name, cfg, api_key=self.config.api_key_for(cfg))
                updated += 1
        self.notify(self.get_session(session_id), f"🔑 API key for '{provider_name}' saved to "
                    "config.json (owner-only). Send your message again.")
        return {"ok": True, "provider": provider_name, "sessions_updated": updated}

    def switch_model(self, spec: str, session_id: int | None = None) -> None:
        session = self.get_session(session_id)
        provider_name, provider_cfg, model = self.config.resolve_model(spec)
        session.agent.provider = build_provider(provider_name, provider_cfg,
                                                api_key=self.config.api_key_for(provider_cfg))
        session.agent.model = model
        session.provider_name, session.spec = provider_name, spec
        session.data["model"] = spec
        self.store.set_active(self.instance, session.id)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for session in self.sessions.values():
            session.permissions.mode = mode

    # ---- improvement swarm (silkcode.swarm) --------------------------------

    def swarm_status(self, session_id: int | None = None) -> dict:
        """Public swarm status (never the internal fields)."""
        status = self.swarms.get(self.get_session(session_id).id)
        if not status:
            return {"running": False, "log": [], "result": None, "error": None}
        return {"running": status["running"], "log": list(status["log"]),
                "result": status["result"], "error": status["error"],
                "artifacts": status.get("artifacts", {})}

    def start_swarm(self, params: dict, session_id: int | None = None) -> dict:
        """Run the swarm on a session's workspace in a background thread,
        streaming swarm_progress / swarm_done / swarm_error events."""
        session = self.get_session(session_id)
        with self.lock:
            if session.running:
                raise ToolError("the agent is running in this session; stop it first")
            if self._swarm_running(session.id):
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
        try:
            developer_count = int(params.get("developer_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"bad developer count: {exc}") from exc

        status = {"running": True, "session": session.id, "started": time.time(), "log": [],
                  "result": None, "error": None, "artifacts": {}, "_stop": threading.Event()}
        self.swarms[session.id] = status
        send = lambda event: self.broadcast({**event, "session": session.id})  # noqa: E731

        def on_progress(line: str) -> None:
            status["log"].append(line)
            send({"type": "swarm_progress", "line": line})

        def on_event(kind: str, data: dict) -> None:
            if kind == "score":
                send({"type": "swarm_score", **data})
            elif kind == "phase":
                send({"type": "swarm_phase", "role": data.get("role")})
            elif kind == "artifact":
                status["artifacts"] = data.get("artifacts") or {}
                send({"type": "swarm_artifact", "artifacts": status["artifacts"]})

        def run() -> None:
            from ..swarm import run_swarm
            try:
                result = run_swarm(
                    session.workspace, worker_spec=worker_spec, critic_spec=critic_spec,
                    tester_spec=tester_spec, target=target, max_iterations=max_iterations,
                    stall_limit=stall_limit, max_tokens=max_tokens,
                    test_command=str(params.get("test_command") or "").strip() or None,
                    on_progress=on_progress, on_event=on_event,
                    should_stop=lambda: status["_stop"].is_set(),
                    # the session's permission manager: same modal, same "yes to all",
                    # and writes go under the session's workspace lock
                    worker_permissions=session.permissions, worker_owner=session.lock_owner,
                    team_mode=bool(params.get("team_mode")),
                    objective=str(params.get("objective") or "").strip() or None,
                    developer_count=developer_count,
                )
                status["result"] = asdict(result)
                send({"type": "swarm_done", "result": status["result"]})
            except Exception as exc:  # keep the daemon alive, report to the UI
                status["error"] = str(exc)
                send({"type": "swarm_error", "message": str(exc)})
            finally:
                status["running"] = False
                status["_stop"] = None

        threading.Thread(target=run, daemon=True).start()
        return self.swarm_status(session.id)

    def stop_swarm(self, session_id: int | None = None) -> dict:
        status = self.swarms.get(self.get_session(session_id).id)
        if status and status.get("running"):
            if status.get("_stop") is not None:
                status["_stop"].set()
            return {"ok": True}
        return {"ok": False, "error": "no swarm running in this session"}

    # ---- self-update / hot reload -----------------------------------------

    def _reexec(self) -> None:
        from ..update import restart_argv
        self._restarting = True
        self.save_all_sessions()  # reopen the active session after re-exec
        self.broadcast({"type": "restarting"})
        time.sleep(0.5)  # let the browser see the event
        os.execv(sys.executable, restart_argv(self._restart_args or []))

    def start_auto_reload(self, restart_args: list[str] | None, interval: float = 3.0) -> bool:
        """When the checkout's HEAD moves and the daemon is idle, re-exec with
        the same args so new code goes live. Only for a git checkout started
        with restart args (the real daemon, not tests)."""
        if not restart_args:
            return False
        self._restart_args = restart_args  # the re-exec needs them, or it comes back as the REPL
        from ..update import git_repo_root, head_changed, head_commit
        repo = git_repo_root()
        if repo is None:
            return False
        baseline = head_commit(repo)

        def loop() -> None:
            while not self._restarting:
                time.sleep(interval)
                try:
                    ready = not self._restarting and head_changed(repo, baseline) and not self._busy()
                except Exception:
                    ready = False
                if ready:
                    self._reexec()

        threading.Thread(target=loop, daemon=True).start()
        return True

    def _restart_after_reinstall(self) -> str:
        """Re-exec so a pip-installed daemon runs the new code: "restarting",
        or "manual" when it cannot (no launch args, or a turn in flight)."""
        if not self._restart_args or self._busy():
            return "manual"
        threading.Thread(target=self._reexec, daemon=True).start()
        return "restarting"

    def update_service(self, params: dict | None = None) -> dict:
        """Pull the latest Silk Code (fast-forward only), or reinstall what pip
        recorded when this is not a checkout. Refused while a swarm runs."""
        from ..update import git_repo_root, update_installation
        repo = git_repo_root()
        with self.lock:
            if any(sw.get("running") for sw in self.swarms.values()):
                raise ToolError("a swarm is running in this session; stop it before updating")
        branch = str(params.get("branch") or "").strip() or None if params else None
        result = update_installation(
            repo=repo, branch=branch,
            on_progress=lambda line: self.broadcast({"type": "update_progress", "line": line}))
        if repo is None and result.get("status") == "updated":
            # no HEAD to watch, so nothing else would swap the running code
            result["restart"] = self._restart_after_reinstall()
        if repo is None and result.get("status") == "error":
            raise ToolError(result["detail"])  # nothing this endpoint can do
        return result

    # ---- GitHub authorization (SRS sections 30-31, 60) ---------------------

    def github_status(self, session_id: int | None = None) -> dict:
        from ..github import DEFAULT_API_URL, GitHubClient, detect_repo, get_token
        from ..github_oauth import client_id_from
        github_cfg = self.config.data.get("github") or {}
        status: dict = {
            "token_env": github_cfg.get("token_env", "GITHUB_TOKEN"),
            "token_stored": bool(github_cfg.get("token")),
            "device_flow_available": bool(client_id_from(self.config.data)),
            "connected": False, "login": None, "repo": None,
            "grants": sorted(self.shared_grants),
        }
        try:
            status["repo"] = "/".join(detect_repo(self.get_session(session_id).workspace))
        except ToolError:
            pass
        token = get_token(self.config)
        if token:
            try:
                status["login"] = GitHubClient(token, github_cfg.get("api_url", DEFAULT_API_URL)).whoami()
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
        GitHubClient(token, github_cfg.get("api_url", DEFAULT_API_URL)).whoami()  # verify first
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
                "missing. Maintainers: see docs/GITHUB_APP.md. You can also paste a token below.")
        flow = DeviceFlow(client_id)
        info = flow.start()

        def wait_for_authorization():
            try:
                store_token(self.config, flow.poll(info["device_code"], int(info.get("interval", 5)),
                                                   int(info.get("expires_in", 900))))
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
        self.shared_grants.update(grants)
        return self.github_status()

    def add_provider(self, body: dict) -> None:
        name = str(body.get("name", "")).strip()
        base_url = str(body.get("base_url", "")).strip()
        if not name or not base_url:
            raise ConfigError("Provider name and base URL are required")
        cfg = {"type": body.get("type", "openai_compat"), "base_url": base_url}
        cfg.update({k: body[k] for k in ("default_model", "api_key_env", "api_key") if body.get(k)})
        self.config.set_provider(name, cfg)
        self.config.save()

    # ---- environment: credentials, usage, live sessions (SRS 19, 48) -------

    def live_sessions(self) -> list[dict]:
        """Sessions open in this daemon right now, with their live spend."""
        return [{"id": s.id, "title": s.data.get("title", "") or "(new)",
                 "model": f"{s.provider_name}/{s.model}", "project": str(s.workspace.root),
                 "running": s.running, "swarm": self._swarm_running(s.id),
                 "locked_out": bool(s.lock_conflict), "usage": s.usage_dict()}
                for s in sorted(self.sessions.values(), key=lambda s: s.id)]

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
            out.append({
                "name": name, "base_url": cfg.get("base_url", ""),
                "type": cfg.get("type", "openai_compat"),
                "default_model": cfg.get("default_model"),
                "has_key": bool(self.config.api_key_for(cfg)),
                "needs_key": bool(cfg.get("api_key_env") or cfg.get("api_key")),
                "local_models": build_provider(name, cfg).list_models() if cfg.get("type") == "ollama" else [],
            })
        return out

    def tree(self, session_id: int | None = None) -> list[dict]:
        from ..remotews import RemoteWorkspace
        workspace = self.get_session(session_id).workspace
        entries: list[dict] = []
        ignored = lambda name: name in IGNORED_DIRS or name.endswith(".egg-info")  # noqa: E731
        if isinstance(workspace, RemoteWorkspace):
            # from the sandbox listing; a set of seen dirs keeps this linear
            seen_dirs: set[str] = set()
            for f in workspace.list_files():
                if len(entries) >= MAX_TREE_ENTRIES:
                    break
                parts = Path(f).parts
                if any(ignored(p) for p in parts):
                    continue
                entries.append({"path": f, "dir": False, "depth": len(parts) - 1})
                for i in range(1, len(parts)):
                    prefix = "/".join(parts[:i])
                    if prefix not in seen_dirs:
                        seen_dirs.add(prefix)
                        entries.append({"path": prefix, "dir": True, "depth": i - 1})
            return entries

        def walk(directory: Path, depth: int) -> None:
            try:
                children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_TREE_ENTRIES:
                    return
                if ignored(child.name):
                    continue
                entries.append({"path": workspace.relative(child), "dir": child.is_dir(), "depth": depth})
                if child.is_dir():
                    walk(child, depth + 1)

        walk(workspace.root, 0)
        return entries

    def pairing_info(self, port: int, token: str | None, bound_host: str = "") -> dict:
        """Everything needed to put another device on this daemon, on demand.

        `bound_host` decides it: a LAN interface means nothing if the listener
        is bound to 127.0.0.1, and a QR for an unreachable URL is a confident
        wrong answer. The token is in the URL, but this endpoint is behind the
        same token, so a caller only learns a credential it already presented.
        """
        from ..inference import reachable_addresses
        from ..qr import QRError, encode

        listening_everywhere = not bound_host or not is_loopback(bound_host)
        suffix = f"/?token={token}" if token else "/"
        magic_dns = None  # a MagicDNS name survives re-addressing and can be retyped
        try:
            from ..tailnet import status as tailnet_status
            tailnet = tailnet_status()
            if tailnet.running and tailnet.name:
                magic_dns = tailnet.name
        except Exception:
            pass
        addresses = []
        for address, label in (reachable_addresses() if listening_everywhere else []):
            host = magic_dns if (label == "Tailscale" and magic_dns) else address
            addresses.append({"address": address, "label": label, "host": host,
                              "url": f"http://{host}:{port}{suffix}"})
        info: dict = {"addresses": addresses, "reachable": bool(addresses),
                      "loopback_only": not listening_everywhere, "tokenless": token is None,
                      "qr": None, "qr_for": None}
        if addresses:
            try:
                info["qr"] = [[bool(v) for v in row] for row in encode(addresses[0]["url"])]
                info["qr_for"] = addresses[0]
            except QRError as exc:
                info["error"] = str(exc)
        return info

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

    def share_update(self, session_id: int | None = None) -> dict:
        from ..share_update import build_share_update
        return build_share_update(self.get_session(session_id).workspace)

    def graph_info(self, session_id: int | None = None) -> dict:
        """For the Graph panel: is graphify installed, is a graph built, and
        its size, hubs and extraction confidence."""
        from .. import graph as graphmod
        ok, hint = graphmod.available()
        if not ok:
            return {"available": False, "hint": hint}
        return {"available": True, **graphmod.stats(self.get_session(session_id).workspace)}

    def graph_build(self, session_id: int | None = None) -> dict:
        from .. import graph as graphmod
        ok, hint = graphmod.available()
        if not ok:
            raise ToolError(hint)
        graphmod.graph_build(self.get_session(session_id).workspace)
        return self.graph_info(session_id)

    def graph_page(self, session_id: int | None = None) -> bytes:
        from ..graph import OUT_DIRNAME
        page = self.get_session(session_id).workspace.root / OUT_DIRNAME / "graph.html"
        if not page.is_file():
            raise FileNotFoundError("graphify-out/graph.html is not built yet; build the graph first")
        return page.read_bytes()

    def projects_info(self) -> list[dict]:
        from ..project import available_projects
        return available_projects()


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "127.0.1.1"}
_DENIAL_NOTICE_AT = {1, 5, 25, 100}


def _warn_about_denial(denial: dict) -> None:
    """Note a refused request on the terminal, only at 1, 5, 25 and 100
    refusals per source: visible probing, without a scanner flooding it."""
    count = denial.get("count", 1)
    if count not in _DENIAL_NOTICE_AT:
        return
    agent = denial.get("agent") or "no user-agent"
    times = "once" if count == 1 else f"{count} times"
    print(f"refused {denial['address']} ({times}): {denial['reason']} "
          f"on {denial['path']} [{agent[:60]}]", file=sys.stderr)


def is_loopback(host: str) -> bool:
    """Whether `host` addresses only this machine, decided on the parsed
    address: a name like 127.0.0.1.evil.example resolves to loopback too,
    which is the DNS-rebinding case the Host check exists to stop."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(host_header: str) -> str:
    """The hostname part of a Host header ('[::1]:80' -> '::1')."""
    host = host_header.strip()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.rpartition(":")[0] if ":" in host else host


def _send(handler, status: int, body: bytes, content_type: str, *headers: tuple[str, str]) -> None:
    """Write one complete response; a client that already left is not an error."""
    try:
        handler.send_response(status)
        handler._security_headers()
        handler.send_header("Content-Type", content_type)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


NO_STORE = ("Cache-Control", "no-store")


def _required(body: dict, key: str) -> str:
    value = str(body.get(key, "")).strip()
    if not value:
        raise ToolError(f"no {key} given")
    return value


class GuiHandler(BaseHTTPRequestHandler):
    state: GuiState = None  # type: ignore[assignment]
    html: bytes = b""
    # Required on every request when reachable beyond loopback; None keeps
    # the local dev-server model.
    token: str | None = None

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        pass

    def _security_headers(self) -> None:
        # The page is opened as /?token=... and links out, so never send a
        # referrer; never sniff types; never allow framing.
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _presented_token(self) -> str:
        """From the header, the query string (a pairing URL beats a stale
        cookie), or the cookie."""
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        query_token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if query_token:
            return query_token
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "silk_token":
                return value
        return ""

    def _same_origin(self) -> bool:
        """Reject requests a browser made on another site's behalf.

        A cross-site text/plain POST needs no preflight and is enough to start
        an agent turn, so an Origin that disagrees with Host is refused. DNS
        rebinding makes those agree, so a tokenless daemon also requires a
        loopback Host. Non-browser clients send no Origin and are unaffected.
        """
        origin = self.headers.get("Origin", "").strip()
        host_header = self.headers.get("Host", "").strip()
        if origin and origin.lower() != "null" and urlparse(origin).netloc.lower() != host_header.lower():
            return False
        return bool(self.token) or not host_header or is_loopback(_hostname(host_header))

    def _authorized(self) -> bool:
        allowed, reason = self._decide()
        self._note(allowed, reason)  # bookkeeping: a refusal nobody sees helps nobody
        return allowed

    def _decide(self) -> tuple[bool, str]:
        if not self._same_origin():
            return False, "cross-origin or unserved host"
        if not self.token:
            return True, ""
        import hmac
        presented = self._presented_token()
        if not presented:
            return False, "no token presented"
        # the token itself is never recorded, right or wrong
        if not hmac.compare_digest(presented, self.token):
            return False, "token did not match"
        return True, ""

    def _note(self, allowed: bool, reason: str) -> None:
        monitor = getattr(self.state, "connections", None)
        if monitor is None:
            return
        denial = monitor.record(
            address=self.client_address[0] if self.client_address else "?",
            path=urlparse(self.path).path, agent=self.headers.get("User-Agent", ""),
            allowed=allowed, reason=reason)
        if denial:
            _warn_about_denial(denial)

    def _deny(self) -> None:
        if not self._same_origin():
            status, body = 403, (b"Forbidden. This request carries another site's origin, or "
                                 b"reached this daemon under a host name it does not serve, "
                                 b"so it was not made by the Silk Code page.")
        else:
            status, body = 401, (b"Unauthorized. This Silk Code daemon is reachable beyond this "
                                 b"machine and requires its access token; open the URL printed "
                                 b"when it started.")
        _send(self, status, body, "text/plain; charset=utf-8")

    def _json(self, data, status: int = 200) -> None:
        _send(self, status, json.dumps(data).encode(), "application/json")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    @staticmethod
    def _session_of(params: dict | None, body: dict | None = None) -> int | None:
        raw = None
        if body and body.get("session_id") is not None:
            raw = body["session_id"]
        elif params and params.get("session"):
            raw = params["session"][0]
        try:
            return None if raw is None else int(raw)
        except (TypeError, ValueError):
            return None

    def handle_one_request(self):  # noqa: D102 - see BaseHTTPRequestHandler
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # the client vanished mid-request; close quietly, no traceback
            self.close_connection = True

    def _dispatch(self, run) -> None:
        try:
            run()
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (ToolError, ConfigError, ProviderError) as exc:
            self._error(str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            return self._deny()
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        self._dispatch(lambda: self._get(parsed.path, params, self.state, self._session_of(params)))

    def _get(self, route: str, params: dict, st: GuiState, sid: int | None) -> None:
        if route == "/":
            # keep the token from ?token=... for the fetches and event stream
            cookie = [("Set-Cookie", f"silk_token={self.token}; Path=/; SameSite=Strict; HttpOnly")] if self.token else []
            return _send(self, 200, self.html, "text/html; charset=utf-8", NO_STORE, *cookie)
        if route == "/api/image":
            image = st.get_session(sid).workspace.resolve((params.get("path") or [""])[0])
            if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
                return self._error("image not found", 404)
            body = image.read_bytes()
            if len(body) > 20 * 1024 * 1024:
                return self._error("image is too large", 413)
            return _send(self, 200, body, IMAGE_TYPES[image.suffix.lower()], NO_STORE)
        if route == "/graph-view":
            return _send(self, 200, st.graph_page(sid), "text/html; charset=utf-8", NO_STORE)
        if route == "/api/events":
            return self._sse()
        routes = {
            "/api/state": lambda: st.state(sid, _flag(params, "all")),
            "/api/transcript": lambda: st.get_session(sid).transcript_snapshot(),
            "/api/tree": lambda: st.tree(sid),
            "/api/file": lambda: st.read_file((params.get("path") or [""])[0], sid),
            "/api/diff": lambda: st.diff(sid),
            "/api/share-update": lambda: st.share_update(sid),
            "/api/graph": lambda: st.graph_info(sid),
            "/api/projects": st.projects_info,
            "/api/providers": st.providers_info,
            "/api/sessions": lambda: st.sessions_summary(sid, _flag(params, "all")),
            "/api/environment": st.environment,
            "/api/connections": st.connections.snapshot,
            "/api/pairing": lambda: st.pairing_info(self.server.server_address[1], self.token,
                                                    str(self.server.server_address[0])),
            "/api/github/status": lambda: st.github_status(sid),
        }
        if route not in routes:
            return self._error("not found", 404)
        self._json(routes[route]())

    def _sse(self) -> None:
        st = self.state
        q = st.subscribe()
        monitor = getattr(st, "connections", None)
        if monitor:
            monitor.stream_opened()
        try:
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    self.wfile.write(f"data: {json.dumps(q.get(timeout=15))}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            st.unsubscribe(q)
            if monitor:
                monitor.stream_closed()

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            return self._deny()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error("invalid JSON body")
        route = urlparse(self.path).path
        self._dispatch(lambda: self._post(route, body, self.state, self._session_of(None, body)))

    def _post(self, route: str, body: dict, st: GuiState, sid: int | None) -> None:
        idle_only = {"/api/model": "switch models", "/api/revert": "revert", "/api/push": "push"}
        if route in idle_only and st.get_session(sid).running:
            return self._error(f"cannot {idle_only[route]} while the agent is running", 409)
        if route == "/api/message":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._error("empty message")
            if not st.start_turn(text, sid):
                return self._error("agent is already running in this session", 409)
            return self._json({"ok": True})
        if route == "/api/permission":
            if not st.answer_permission(str(body.get("id", "")), str(body.get("decision", "no"))):
                return self._error("unknown permission request", 404)
            return self._json({"ok": True})
        if route == "/api/mode" and str(body.get("mode", "")) not in PermissionManager.MODES:
            return self._error(f"unknown mode '{body.get('mode', '')}'")
        if route == "/api/github/grants" and not isinstance(body.get("grants"), list):
            return self._error("'grants' must be a list")

        def model():
            st.switch_model(str(body.get("spec", "")), sid)
            return st.state(sid)

        def mode():
            st.set_mode(str(body["mode"]))
            return st.state(sid)

        def push():
            from ..tools.git import git_push
            return {"result": git_push(st.get_session(sid).workspace)}

        def autopush():
            st.auto_push = bool(body.get("enabled"))
            if st.auto_push:
                st.shared_grants.add("push")
            return st.state(sid)

        def stop():
            st.stop(sid)
            return {"ok": True}

        def fork():
            at = body.get("at_message")
            return st.state(st.fork_session(sid, at_message=int(at) if at is not None else None,
                                            isolated=bool(body.get("isolated", True))).id)

        def provider_key():
            provider = str(body.get("provider", ""))
            if body.get("clear"):
                return st.clear_provider_key(provider)
            return st.set_provider_key(provider, str(body.get("key", "")))

        def add_provider():
            st.add_provider(body)
            return st.providers_info()

        routes = {
            "/api/model": model,
            "/api/mode": mode,
            "/api/graph/build": lambda: st.graph_build(sid),
            "/api/revert": lambda: {"restored": st.get_session(sid).agent.checkpoints.revert_last()},
            "/api/push": push,
            "/api/autopush": autopush,
            "/api/stop": stop,
            "/api/session/new": lambda: st.state(st.new_session(
                project=str(body.get("project", "")).strip() or None).id),
            "/api/session": lambda: st.state(st.load_session(int(body.get("id", 0))).id),
            "/api/keys": lambda: st.set_api_key(str(body.get("provider", "")), str(body.get("key", "")), sid),
            "/api/session/fork": fork,
            "/api/session/project": lambda: st.move_session(sid, _required(body, "project")),
            "/api/project/open": lambda: st.open_project(_required(body, "project")),
            "/api/project/create": lambda: st.create_project(body),
            "/api/session/close": lambda: st.close_session(sid),
            "/api/project/close": lambda: st.close_project(_required(body, "project"), sid),
            "/api/lock/takeover": lambda: st.takeover_lock(sid),
            "/api/swarm/start": lambda: st.start_swarm(body, sid),
            "/api/swarm/stop": lambda: st.stop_swarm(sid),
            "/api/swarm/status": lambda: st.swarm_status(sid),
            "/api/update": lambda: st.update_service(body),
            "/api/environment/key": provider_key,
            "/api/github/token": lambda: st.set_github_token(str(body.get("token", ""))),
            "/api/github/device": st.github_device_start,
            "/api/github/grants": lambda: st.set_grants(body["grants"]),
            "/api/providers": add_provider,
        }
        if route not in routes:
            return self._error("not found", 404)
        self._json(routes[route]())


_UI_VERSION_LITERAL = re.compile(rb'(const UI_VERSION = ")[^"]*(")')


def _stamped_app_html() -> bytes:
    """The GUI page stamped with the serving daemon's build, so a tab left
    open across `silkcode update` notices it came from a different build.
    If the literal is ever renamed the page is served as is: a staleness hint
    is not worth a blank GUI."""
    raw = (Path(__file__).parent / "app.html").read_bytes()
    from ..version import build_id
    stamped, count = _UI_VERSION_LITERAL.subn(
        lambda m: m.group(1) + build_id().encode() + m.group(2), raw, count=1)
    return stamped if count else raw


def _print_pairing(port: int, token: str | None) -> None:
    """The addresses another device can open, and a QR for the best of them
    (Tailscale first: it keeps working away from home)."""
    from ..inference import reachable_addresses
    from ..qr import QRError, terminal_qr

    addresses = reachable_addresses()
    if not addresses:
        print("\nNo network address found for this machine - it cannot be reached "
              "from another device yet.")
        return
    suffix = f"/?token={token}" if token else "/"
    print("\nReachable from another device:")
    for address, label in addresses:
        note = "works from anywhere on your tailnet" if label == "Tailscale" else "same network only"
        print(f"  http://{address}:{port}{suffix}")
        print(f"      {label} - {note}")

    best, best_label = addresses[0]
    try:
        print(f"\nPoint a phone camera at this to open it ({best_label}):\n")
        print(terminal_qr(f"http://{best}:{port}{suffix}"))
    except QRError as exc:
        print(f"(no QR: {exc})")  # the URL above is the actual answer
    has_tailnet = any(label == "Tailscale" for _, label in addresses)
    try:
        from ..tailnet import advice
        hint = advice(has_tailnet)
    except Exception:
        hint = "" if has_tailnet else (
            "\nOn the same Wi-Fi only. To reach this from anywhere, put both "
            "machines on a\nTailscale tailnet (https://tailscale.com).")
    if hint:
        print("\n" + hint)


def _token_path(host: str, port: int) -> "Path":
    return config_dir() / "gui-tokens" / f"{host.replace(':', '_')}-{port}.token"


def remembered_token(host: str, port: int) -> str | None:
    """The token this daemon used last time on this address.

    A self-update re-execs with the launch argv, which never carries the
    token, so without this every update locked out the tab already open (on a
    phone, far from the terminal that printed the new URL). Kept in the
    config directory, not argv (visible in `ps`) or the environment
    (inherited by every command the agent runs).
    """
    try:
        text = _token_path(host, port).read_text().strip()
    except OSError:
        return None
    return text or None


def remember_token(host: str, port: int, token: str) -> None:
    """Owner-only; failure only costs a new token on the next start."""
    path = _token_path(host, port)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text(token + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def run_gui(path: str | None, model_spec: str | None, mode: str, host: str = "127.0.0.1",
            port: int = 8377, grants: list[str] | None = None,
            use_sandbox: bool = False, auto_push: bool = False,
            restart_args: list[str] | None = None, token: str | None = None,
            remote: str | None = None) -> int:
    # Bind first: --port 0 picks a port, and a busy address fails before any state exists.
    try:
        server = ThreadingHTTPServer((host, port), GuiHandler)
    except OSError as exc:
        print(f"error: cannot listen on {host}:{port} ({exc}).")
        print("Another Silk Code GUI may already be running on this address - every")
        print("instance needs its own address. Start this one on another port:")
        print(f"    silkcode gui --port {port + 1} <path>")
        return 1
    bound_host, bound_port = server.server_address[:2]
    projectless = path is None and remote is None
    if projectless:
        launcher = config_dir() / "launcher"
        launcher.mkdir(parents=True, exist_ok=True)
        path = str(launcher)
    try:
        state = GuiState(path, model_spec, mode, grants=grants, use_sandbox=use_sandbox,
                         auto_push=auto_push, instance=f"{bound_host}:{bound_port}",
                         remote=remote, projectless=projectless)
    except (ToolError, ConfigError) as exc:
        print(f"error: {exc}")
        server.server_close()
        return 1
    GuiHandler.state = state
    GuiHandler.html = _stamped_app_html()
    # The agent reads, writes and runs commands: reachable beyond loopback it
    # needs a credential, so one is required (and generated when not given).
    if not is_loopback(host) and not token:
        token = remembered_token(host, port)
        if token:
            print("This daemon is reachable beyond this machine, so it requires an "
                  "access token.\nReusing the one from last time, so a URL you "
                  "already have still works.\n")
        else:
            import secrets
            token = secrets.token_urlsafe(24)
            print("This daemon is reachable beyond this machine, so it requires an "
                  "access token.\nOpen the URL below (it carries the token); share it "
                  "with nobody you would not\ngive a shell on this machine.\n")
    if token and not is_loopback(host):
        remember_token(host, port, token)  # an explicit --token too: restarts drop it
    GuiHandler.token = token
    if restart_args:
        state.start_auto_reload(restart_args)
    display_host = "localhost" if bound_host in ("0.0.0.0", "::") else bound_host
    url = f"http://{display_host}:{bound_port}" + (f"/?token={token}" if token else "")
    print(f"Silk Code GUI: {url}")
    if not is_loopback(host):
        _print_pairing(bound_port, token)
    print("workspace: choose a project in the GUI" if projectless
          else f"workspace: {state.workspace.root}")
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
