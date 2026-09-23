"""The project as a graph, via graphify.

Graphify (github.com/Graphify-Labs/graphify) parses a repository with
tree-sitter into a knowledge graph - deterministic, local, no model call for
code - and answers structural questions against it: what connects to what,
what everything flows through, what breaks if this changes. We verified it on
this repository before adopting it: 3,273 nodes in 6.5 seconds, and its
"affected" answers matched our own tests' imports exactly.

The adoption is native but the package stays optional: `pip install
graphifyy` puts the `graphify` CLI on PATH and everything here lights up;
without it, every entry point explains that one command instead of failing -
the Tailscale pattern. It is not a wheel dependency: fifteen tree-sitter
grammars is a lot to charge every install for a feature that degrades this
gracefully (install.py, the recommended path, sets it up alongside Chromium).

Two boundaries are deliberate:

- Building writes `graphify-out/` into the project tree (their convention,
  kept for interoperability with their own tooling - graph.html is *for the
  user* and their skill expects it there). A write into the tree goes
  through the permission gate; the query tools therefore never auto-build,
  because a read-only tool that quietly writes on first use has lied about
  what it is.
- Query output is derived entirely from the user's own files by a local
  parser, so it re-enters the conversation as ordinary tool output - and
  still passes the provenance scan, like everything else, because the files
  it summarizes were never trusted to begin with.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .workspace import ToolError, Workspace

OUT_DIRNAME = "graphify-out"
GRAPH_RELPATH = f"{OUT_DIRNAME}/graph.json"
BUILD_TIMEOUT = 600          # a ~1M-LOC repository builds in minutes, not hours
QUERY_TIMEOUT = 60
TOP_HUBS = 8

INSTALL_HINT = ("Graphify is not installed. Install it with:\n"
                "  pip install graphifyy       (the CLI command is `graphify`)\n"
                "Code parsing is local tree-sitter - no model call, nothing "
                "leaves the machine.")


def cli_path() -> str | None:
    """The graphify executable, wherever this install put it.

    Beside the running interpreter first: install.py sets Silk Code up in an
    isolated environment whose bin/ is not on PATH, and that is exactly the
    install most people have.
    """
    import sys
    beside = Path(sys.executable).parent / ("graphify.exe" if sys.platform == "win32"
                                            else "graphify")
    if beside.exists():
        return str(beside)
    return shutil.which("graphify")


def available() -> tuple[bool, str]:
    if cli_path():
        return True, ""
    return False, INSTALL_HINT


def graph_path(ws: Workspace) -> Path:
    return ws.root / GRAPH_RELPATH


def _local_only(ws: Workspace) -> None:
    from .remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        raise ToolError("The graph tools need the repository on this machine; "
                        "a remote workspace lives in the sandbox.")


def _run(ws: Workspace, *args: str, timeout: int = QUERY_TIMEOUT) -> str:
    ok, why = available()
    if not ok:
        return why
    try:
        proc = subprocess.run([cli_path() or "graphify", *args], cwd=ws.root,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"graphify {args[0]} timed out after {timeout}s."
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    return output.strip() or f"graphify {args[0]} produced no output (exit {proc.returncode})."


def _needs_graph(ws: Workspace) -> str | None:
    if graph_path(ws).is_file():
        return None
    return ("No graph yet for this project. Build it first with graph_build "
            f"(writes {OUT_DIRNAME}/ into the project).")


# ---- the agent-facing tools -------------------------------------------------

def graph_build(ws: Workspace) -> str:
    """Parse the project into graphify-out/graph.json and summarize it."""
    _local_only(ws)
    ok, why = available()
    if not ok:
        return why
    built = _run(ws, "update", ".", timeout=BUILD_TIMEOUT)
    if not graph_path(ws).is_file():
        return f"The graph was not built:\n{built}"
    # communities without an LLM: --no-label keeps placeholders, --no-viz can
    # be regenerated later; clustering is what makes the stats say something
    _run(ws, "cluster-only", ".", "--no-label", timeout=BUILD_TIMEOUT)
    return render_stats(stats(ws))


def graph_query(ws: Workspace, question: str) -> str:
    """Traverse the graph for a question. Works best with concept names."""
    _local_only(ws)
    question = " ".join((question or "").split())
    if not question:
        raise ToolError("graph_query needs a question")
    missing = _needs_graph(ws)
    if missing:
        return missing
    return _run(ws, "query", question)


def graph_explain(ws: Workspace, name: str) -> str:
    """One node - a class, function, or file - and everything touching it."""
    _local_only(ws)
    name = (name or "").strip()
    if not name:
        raise ToolError("graph_explain needs a node name")
    missing = _needs_graph(ws)
    if missing:
        return missing
    return _run(ws, "explain", name)


def graph_impact(ws: Workspace, name: str, depth: int = 2) -> str:
    """What is affected if `name` changes - reverse dependency traversal.
    Use it before a refactor: the blast radius, with file:line anchors."""
    _local_only(ws)
    name = (name or "").strip()
    if not name:
        raise ToolError("graph_impact needs a node name")
    missing = _needs_graph(ws)
    if missing:
        return missing
    depth = min(max(int(depth), 1), 4)
    return _run(ws, "affected", name, "--depth", str(depth))


def permission_command(args: dict, ws) -> str:
    """graph_build writes graphify-out/ into the user's tree, so it is judged
    like the command it runs. The query tools are read-only over local files
    and never prompt (they refuse to auto-build for exactly that reason)."""
    return "graphify update ."


# ---- the numbers behind the GUI panel ---------------------------------------

def stats(ws: Workspace) -> dict:
    """What the graph says about the project, as plain numbers.

    This is what the GUI's Graph panel shows a user about the platform they
    built: how big the structure is, what everything flows through, and how
    much of the map was read directly from source versus inferred.
    """
    path = graph_path(ws)
    if not path.is_file():
        return {"built": False}
    try:
        data = json.loads(path.read_text(errors="replace"))
    except ValueError:
        return {"built": False, "error": "graph.json is not valid JSON"}
    nodes = data.get("nodes") or []
    links = data.get("links") or data.get("edges") or []

    degree: dict[str, int] = {}
    extracted = 0
    for link in links:
        degree[str(link.get("source"))] = degree.get(str(link.get("source")), 0) + 1
        degree[str(link.get("target"))] = degree.get(str(link.get("target")), 0) + 1
        if link.get("confidence") == "EXTRACTED":
            extracted += 1
    labels = {str(n.get("id")): str(n.get("label") or n.get("id")) for n in nodes}
    hubs = [{"name": labels.get(node_id, node_id), "degree": count}
            for node_id, count in sorted(degree.items(), key=lambda kv: -kv[1])
            if node_id in labels][:TOP_HUBS]
    communities = {n.get("community") for n in nodes if n.get("community") is not None}
    files = {n.get("source_file") for n in nodes if n.get("source_file")}

    return {
        "built": True,
        "nodes": len(nodes),
        "edges": len(links),
        "files": len(files),
        "communities": len(communities),
        "extracted_pct": round(100 * extracted / len(links)) if links else 0,
        "hubs": hubs,
        "has_viz": (ws.root / OUT_DIRNAME / "graph.html").is_file(),
    }


def render_stats(data: dict) -> str:
    if not data.get("built"):
        return "No graph built." + (f" ({data['error']})" if data.get("error") else "")
    lines = [
        f"Graph built: {data['nodes']} nodes, {data['edges']} edges across "
        f"{data['files']} files"
        + (f", {data['communities']} communities" if data["communities"] else "")
        + f"; {data['extracted_pct']}% of edges read directly from source.",
        "",
        "What everything flows through:",
    ]
    lines += [f"  {hub['name']} - {hub['degree']} connections" for hub in data["hubs"]]
    lines.append("")
    lines.append("Query it with graph_query / graph_explain / graph_impact; "
                 f"{OUT_DIRNAME}/graph.html is the clickable map for the user.")
    return "\n".join(lines)
