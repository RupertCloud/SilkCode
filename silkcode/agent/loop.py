"""The agent loop: model calls, tool dispatch, permissions, checkpoints."""

from __future__ import annotations

import json
import platform
from typing import Callable

from ..checkpoints import Checkpoints
from ..permissions import PermissionManager
from ..providers.base import ChatResult, ModelProvider, ProviderError, ToolCall, Usage
from ..tools import TOOLS, openai_schemas
from ..workspace import ToolError, Workspace
from .prompts import SYSTEM_PROMPT

MAX_STEPS = 40

# Context compaction (SRS section 24). Token counts are estimated at ~4
# characters per token; compaction triggers when the estimate exceeds the
# budget and never touches the most recent turns.
DEFAULT_CONTEXT_TOKENS = 100_000
KEEP_RECENT_TOOL_RESULTS = 6
TRUNCATED_TOOL_CHARS = 500

# on_event(kind, data): kind in {"text", "tool_start", "tool_result"}
EventHandler = Callable[[str, object], None]


class Agent:
    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        workspace: Workspace,
        permissions: PermissionManager,
        checkpoints: Checkpoints | None = None,
        on_event: EventHandler | None = None,
        context: str | None = None,
        mcp=None,
        max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        session_id: int | None = None,
        attribution: bool = True,
        lock_owner: str | None = None,
        redact_output: bool = True,
    ):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.permissions = permissions
        self.checkpoints = checkpoints or Checkpoints()
        self.on_event: EventHandler = on_event or (lambda kind, data: None)
        self.mcp = mcp
        self.session_id = session_id
        self.attribution = attribution
        # Owner identity for the advisory per-workspace lock (e.g. "session-3").
        # None means the agent does not hold/refresh the workspace lock.
        self._lock_owner = lock_owner
        self.usage = Usage()
        self.stop_requested = False
        self.max_context_tokens = max_context_tokens
        self.trimmed_messages = 0
        # Per-owner optimistic-concurrency registry for the file tools: this
        # agent's reads/writes never mask another session's stale base.
        self._fp_registry: dict = {}
        # Where this turn's instructions came from, so the permission gate
        # can tell the human whether an outward-facing action traces back to
        # their request or to something the agent read.
        from ..provenance import TurnProvenance
        self.provenance = TurnProvenance()
        if hasattr(permissions, "watch"):
            permissions.watch(self.provenance)
        # Literal credentials this installation holds, so tool output that
        # happens to print one is scrubbed before it reaches the provider.
        # Resolved once per agent: it reads the config and the environment.
        self._known_secrets: tuple[str, ...] = ()
        if redact_output:
            try:
                from ..config import Config
                from ..redact import known_secrets
                self._known_secrets = known_secrets(Config.load())
            except Exception:
                pass  # never let redaction setup stop an agent from starting
        self.redact_output = redact_output
        system = SYSTEM_PROMPT.format(root=workspace.root, platform=platform.platform())
        if context:
            system += "\n" + context
        self._base_system = system
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def set_workspace(self, workspace: "Workspace", context: str) -> None:
        """Point this agent at a different project. The conversation (including
        checkpoints and attribution) is kept; the system prompt is regenerated
        with the new root and context so the model orients on the new project."""
        self.workspace = workspace
        system = SYSTEM_PROMPT.format(root=workspace.root, platform=platform.platform())
        if context:
            system += "\n" + context
        self._base_system = system
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = system
        self.trimmed_messages = 0  # stale trim note refers to the old project
        self.checkpoints.begin()

    def run_turn(self, user_input: str) -> str:
        from ..tools.git import clear_attribution, set_attribution
        if self.attribution:
            set_attribution(model=f"{self.provider.name}/{self.model}", session=self.session_id)
        self.stop_requested = False
        self.checkpoints.begin()
        self.provenance.begin(user_input)
        self.messages.append({"role": "user", "content": user_input})
        try:
            for _ in range(MAX_STEPS):
                result = self._call_model()
                self.usage.add(result.usage)
                self._append_assistant(result)
                if not result.tool_calls:
                    return result.content
                for call in result.tool_calls:
                    if self.stop_requested:
                        output = "Cancelled: the user stopped this turn."
                    else:
                        output = self._execute_tool(call)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": self._scrub(output),
                    })
                if self.stop_requested:
                    return "Stopped by user."
            return "Stopped: reached the maximum number of agent steps for one turn."
        finally:
            clear_attribution()  # attribution never outlives the turn
            self.provenance.end()  # nor does what this turn read

    def _scrub(self, output: str) -> str:
        """Remove credentials from tool output before it joins the
        conversation - which is to say, before it is sent to the provider.

        A backstop for the ordinary case: a `printenv`, a `cat .env`, a stack
        trace carrying a connection string. It is not a boundary and cannot
        be one; the boundaries are the sandbox never holding a credential as
        a string, an owner-only config file, and the permission gate.
        """
        if not self.redact_output:
            return output
        try:
            from ..redact import redact
            return redact(output, extra=self._known_secrets)
        except Exception:
            # A failure here must never lose the tool's result: the agent
            # needs it to make progress, and redaction is the backstop.
            return output

    def request_stop(self) -> None:
        """Stop after the current model call or tool finishes."""
        self.stop_requested = True

    def repair_dangling_tool_calls(self) -> None:
        """Append cancelled tool results if a turn was interrupted mid-call,
        so the message history stays valid for the next request."""
        if not self.messages:
            return
        last = self.messages[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            for tc in last["tool_calls"]:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "Cancelled: the user interrupted this turn.",
                })

    # ---- context compaction (SRS section 24) -------------------------------

    def context_tokens(self) -> int:
        """Rough token estimate for the current conversation."""
        chars = 0
        for m in self.messages:
            chars += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                chars += len(tc["function"]["name"]) + len(tc["function"]["arguments"])
        return chars // 4

    def _compact(self) -> None:
        if self.context_tokens() <= self.max_context_tokens:
            return
        # Stage 1: truncate all but the most recent tool results.
        tool_messages = [m for m in self.messages if m.get("role") == "tool"]
        for m in tool_messages[:-KEEP_RECENT_TOOL_RESULTS]:
            content = str(m.get("content") or "")
            if len(content) > TRUNCATED_TOOL_CHARS:
                m["content"] = content[:TRUNCATED_TOOL_CHARS] + "\n...[old output truncated to save context]"
        # Stage 2: drop the oldest turns, always cutting at a user-message
        # boundary so assistant/tool pairs stay intact.
        while self.context_tokens() > self.max_context_tokens:
            user_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "user"]
            if len(user_indices) < 2:
                break  # only the current turn remains; nothing left to drop
            start, end = user_indices[0], user_indices[1]
            self.trimmed_messages += end - start
            del self.messages[start:end]
        if self.trimmed_messages:
            self.messages[0]["content"] = (
                self._base_system
                + f"\n[Context note: {self.trimmed_messages} earlier messages were trimmed to fit "
                "the context window. Re-read files or re-run searches if you need that information.]"
            )

    def _call_model(self) -> ChatResult:
        self._compact()
        schemas = openai_schemas()
        if self.mcp is not None:
            schemas = schemas + self.mcp.tool_schemas()
        result = None
        for kind, data in self.provider.stream(self.model, self.messages, tools=schemas):
            if kind == "text":
                self.on_event("text", data)
            elif kind == "result":
                result = data
        if result is None:
            raise ProviderError(f"{self.provider.name}: stream ended without a result")
        return result

    def _append_assistant(self, result: ChatResult) -> None:
        message: dict = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in result.tool_calls
            ]
        self.messages.append(message)

    def _execute_tool(self, call: ToolCall) -> str:
        tool = TOOLS.get(call.name)
        if tool is None and not (self.mcp is not None and self.mcp.has_tool(call.name)):
            return f"Error: unknown tool '{call.name}'"
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"Error: invalid tool arguments (not JSON): {exc}"
        if not isinstance(args, dict):
            return "Error: tool arguments must be a JSON object"
        self.on_event("tool_start", {"name": call.name, "args": args})
        if tool is None:
            output = self._execute_mcp(call.name, args)
            # An MCP result is the least trusted content the agent sees: it
            # came off the network, through a server this process does not own.
            self._note_source(call.name, args, output)
            self.on_event("tool_result", {"name": call.name, "output": output})
            return output
        try:
            output = self._run_with_permissions(tool, args)
        except ToolError as exc:
            output = f"Error: {exc}"
        except TypeError as exc:
            output = f"Error: bad arguments for {call.name}: {exc}"
        except Exception as exc:  # surface unexpected failures to the model
            output = f"Error: {type(exc).__name__}: {exc}"
        self._note_source(call.name, args, output)
        self.on_event("tool_result", {"name": call.name, "output": output})
        return output

    def _note_source(self, name: str, args: dict, output: str) -> None:
        """Record what this turn read. Tool output describes the world; it
        never carries the authority to approve an action."""
        try:
            detail = args.get("path") or args.get("command") or args.get("url") or ""
            label = f"{name}({str(detail)[:60]})" if detail else name
            self.provenance.record(label, str(output), kind="tool")
        except Exception:
            pass  # provenance is context for a human, never a failure path

    def _execute_mcp(self, qualified: str, args: dict) -> str:
        if not self.permissions.check_mcp(qualified):
            return "User denied permission to call this MCP tool."
        try:
            return self.mcp.call(qualified, args)
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    def _run_with_permissions(self, tool, args: dict) -> str:
        if tool.kind == "write":
            if tool.path_of is not None:
                raw_path = str(tool.path_of(args, self.workspace))
            else:
                raw_path = str(args.get("path", ""))
            resolved = self.workspace.resolve(raw_path)
            if not self.permissions.check_write(self.workspace.relative(resolved)):
                return "User denied permission to modify this file."
            self.checkpoints.snapshot(resolved)
        elif tool.kind == "command":
            if tool.command_of is not None:
                command = str(tool.command_of(args, self.workspace) or "")
            else:
                command = str(args.get("command") or "")
            if command and not self.permissions.check_command(command):
                return "User denied permission to run this command."
        if getattr(tool, "owner_aware", False):
            return tool.func(self.workspace, _registry=self._fp_registry,
                             _owner=self._lock_owner, **args)
        return tool.func(self.workspace, **args)
