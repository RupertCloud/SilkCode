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
    ):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.permissions = permissions
        self.checkpoints = checkpoints or Checkpoints()
        self.on_event: EventHandler = on_event or (lambda kind, data: None)
        self.usage = Usage()
        self.stop_requested = False
        system = SYSTEM_PROMPT.format(root=workspace.root, platform=platform.platform())
        if context:
            system += "\n" + context
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

    def _call_model(self) -> ChatResult:
        result = None
        for kind, data in self.provider.stream(self.model, self.messages, tools=openai_schemas()):
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
        if tool is None:
            return f"Error: unknown tool '{call.name}'"
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"Error: invalid tool arguments (not JSON): {exc}"
        if not isinstance(args, dict):
            return "Error: tool arguments must be a JSON object"
        self.on_event("tool_start", {"name": call.name, "args": args})
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

    def _run_with_permissions(self, tool, args: dict) -> str:
        if tool.kind == "write":
            resolved = self.workspace.resolve(str(args.get("path", "")))
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
