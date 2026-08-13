"""Minimal MCP (Model Context Protocol) client over stdio (SRS section 41).

Servers are configured in config.json:

    "mcp_servers": {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "env": {}}
    }

Each server's tools are exposed to the model as `mcp__<server>__<tool>`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading

from . import __version__

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0


class McpError(RuntimeError):
    pass


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
    return cleaned.replace("__", "-")


class McpServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None):
        self.name = sanitize_name(name)
        if args is None:
            parts = shlex.split(command)
            command, args = parts[0], parts[1:]
        self.command = command
        self.args = args
        self.env = env or {}
        self.tools: list[dict] = []
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._next_id = 0

    def start(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        env = dict(os.environ)
        env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise McpError(f"{self.name}: cannot start '{self.command}': {exc}") from exc
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "silkcode", "version": __version__},
        }, timeout)
        self._notify("notifications/initialized", {})
        listed = self._request("tools/list", {}, timeout)
        self.tools = listed.get("tools", [])

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                slot = self._pending.get(msg.get("id"))
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
        with self._lock:
            for slot in self._pending.values():
                if slot["msg"] is None:
                    slot["msg"] = {"error": {"message": f"MCP server '{self.name}' exited"}}
                slot["event"].set()

    def _send(self, msg: dict) -> None:
        if self._proc is None or self._proc.poll() is not None or self._proc.stdin is None:
            raise McpError(f"{self.name}: server is not running")
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            slot = {"event": threading.Event(), "msg": None}
            self._pending[msg_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            if not slot["event"].wait(timeout):
                raise McpError(f"{self.name}: '{method}' timed out after {timeout}s")
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)
        msg = slot["msg"] or {}
        if msg.get("error"):
            raise McpError(f"{self.name}: {msg['error'].get('message', 'unknown error')}")
        return msg.get("result") or {}

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, tool: str, arguments: dict, timeout: float = DEFAULT_TIMEOUT) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout)
        parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p) or "(no output)"
        if result.get("isError"):
            return f"MCP tool error: {text}"
        return text

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class McpManager:
    def __init__(self, servers_cfg: dict):
        self.servers: dict[str, McpServer] = {}
        self.errors: dict[str, str] = {}
        self._tool_map: dict[str, tuple[McpServer, str]] = {}
        for name, cfg in servers_cfg.items():
            server = McpServer(name, cfg.get("command", ""), cfg.get("args"), cfg.get("env"))
            try:
                server.start()
            except McpError as exc:
                self.errors[server.name] = str(exc)
                continue
            self.servers[server.name] = server
            for tool in server.tools:
                qualified = f"mcp__{server.name}__{tool['name']}"
                self._tool_map[qualified] = (server, tool["name"])

    def has_tool(self, qualified: str) -> bool:
        return qualified in self._tool_map

    def tool_schemas(self) -> list[dict]:
        schemas = []
        for qualified, (server, tool_name) in self._tool_map.items():
            tool = next(t for t in server.tools if t["name"] == tool_name)
            schemas.append({
                "type": "function",
                "function": {
                    "name": qualified,
                    "description": f"[MCP: {server.name}] {tool.get('description', '')}",
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return schemas

    def call(self, qualified: str, arguments: dict) -> str:
        entry = self._tool_map.get(qualified)
        if entry is None:
            raise McpError(f"unknown MCP tool '{qualified}'")
        server, tool_name = entry
        return server.call(tool_name, arguments)

    def close(self) -> None:
        for server in self.servers.values():
            server.stop()
