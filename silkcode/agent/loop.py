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
    ):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.permissions = permissions
        self.checkpoints = checkpoints or Checkpoints()
        self.on_event: EventHandler = on_event or (lambda kind, data: None)
        self.mcp = mcp
        self.usage = Usage()
        self.stop_requested = False
        self.max_context_tokens = max_context_tokens
        self.trimmed_messages = 0
        system = SYSTEM_PROMPT.format(root=workspace.root, platform=platform.platform())
        if context:
            system += "\n" + context
        self._base_system = system
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def run_turn(self, user_input: str) -> str:
        self.stop_requested = False
        self.checkpoints.begin()
        self.messages.append({"role": "user", "content": user_input})
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
                    "content": output,
                })
            if self.stop_requested:
                return "Stopped by user."
        return "Stopped: reached the maximum number of agent steps for one turn."

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
        self.on_event("tool_result", {"name": call.name, "output": output})
        return output

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
        return tool.func(self.workspace, **args)
