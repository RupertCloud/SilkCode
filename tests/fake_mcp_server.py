"""A minimal MCP server over stdio, used by the MCP client tests."""

import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOLS = [{
    "name": "echo",
    "description": "Echo text back",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") == "echo":
            text = (params.get("arguments") or {}).get("text", "")
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": f"echo: {text}"}],
            }})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "unknown tool"}})
    elif "id" in msg:
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}})
