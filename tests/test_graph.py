"""The graphify adoption: graph tools, GUI panel numbers, and the boundaries.

Unit tests run against a fake `graphify` executable and hand-written
graph.json files, so CI needs nothing installed. Tests marked needs_graphify
drive the real CLI end-to-end and skip cleanly where it is absent.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from silkcode import graph as graphmod
from silkcode.graph import (
    GRAPH_RELPATH,
    OUT_DIRNAME,
    graph_build,
    graph_explain,
    graph_impact,
    graph_query,
    permission_command,
    render_stats,
    stats,
)
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def ws(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    root = tmp_path / "repo"
    root.mkdir()
    return Workspace(root)


def fake_cli(tmp_path, monkeypatch, script):
    """A `graphify` on PATH that runs `script` (sh) and records its argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    cli = bindir / "graphify"
    cli.write_text("#!/bin/sh\n"
                   f'echo "$@" >> {log}\n' + script)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(graphmod, "cli_path", lambda: str(cli))
    return log


def write_graph(ws, nodes=None, links=None):
    path = ws.root / GRAPH_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "nodes": nodes if nodes is not None else [
            {"id": "workspace", "label": "Workspace", "source_file": "workspace.py",
             "community": 0},
            {"id": "toolerror", "label": "ToolError", "source_file": "workspace.py",
             "community": 0},
            {"id": "agent", "label": "Agent", "source_file": "agent/loop.py",
             "community": 1},
        ],
        "links": links if links is not None else [
            {"source": "agent", "target": "workspace", "relation": "uses",
             "confidence": "INFERRED"},
            {"source": "agent", "target": "toolerror", "relation": "imports",
             "confidence": "EXTRACTED"},
            {"source": "workspace", "target": "toolerror", "relation": "uses",
             "confidence": "EXTRACTED"},
        ],
        "directed": False,
    }))
    return path


# ---- availability is a message, not a crash ---------------------------------

def test_without_graphify_every_tool_explains_the_one_command(ws, monkeypatch):
    monkeypatch.setattr(graphmod, "cli_path", lambda: None)
    for result in (graph_build(ws), ):
        assert "pip install graphifyy" in result
    write_graph(ws)
    assert "pip install graphifyy" in graph_query(ws, "Workspace")


# ---- the permission boundary -------------------------------------------------

def test_building_is_judged_like_the_command_it_runs():
    from silkcode.permissions import Risk, classify_command
    assert classify_command(permission_command({}, None)) == Risk.MEDIUM


def test_queries_never_auto_build(ws, tmp_path, monkeypatch):
    """A read-only tool that quietly writes on first use has lied about what
    it is - so with no graph, the query tools point at graph_build instead
    of running anything."""
    log = fake_cli(tmp_path, monkeypatch, "exit 0\n")
    for tool, arg in ((graph_query, "x"), (graph_explain, "x"), (graph_impact, "x")):
        result = tool(ws, arg)
        assert "graph_build" in result
    assert not log.exists(), "a query executed graphify with no graph present"


def test_remote_workspaces_are_refused_with_the_reason(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))

    class FakeRemote:
        pass

    from silkcode import remotews
    monkeypatch.setattr(remotews, "RemoteWorkspace", FakeRemote)
    with pytest.raises(ToolError) as caught:
        graph_query(FakeRemote(), "x")
    assert "sandbox" in str(caught.value)


# ---- wrapping the CLI ---------------------------------------------------------

def test_queries_run_in_the_workspace_and_return_output(ws, tmp_path, monkeypatch):
    write_graph(ws)
    log = fake_cli(tmp_path, monkeypatch, 'echo "Node: Workspace (47 connections)"\n')
    result = graph_explain(ws, "Workspace")
    assert "47 connections" in result
    assert log.read_text().strip() == "explain Workspace"


def test_impact_clamps_depth(ws, tmp_path, monkeypatch):
    write_graph(ws)
    log = fake_cli(tmp_path, monkeypatch, "echo ok\n")
    graph_impact(ws, "Agent", depth=99)
    assert "--depth 4" in log.read_text()


def test_a_hung_cli_times_out_with_a_message_not_a_traceback(ws, tmp_path, monkeypatch):
    write_graph(ws)
    fake_cli(tmp_path, monkeypatch, "sleep 60\n")
    monkeypatch.setattr(graphmod, "QUERY_TIMEOUT", 1)
    assert "timed out" in graph_query(ws, "x")


def test_empty_arguments_are_refused(ws):
    for tool in (graph_query, graph_explain, graph_impact):
        with pytest.raises(ToolError):
            tool(ws, "   ")


def test_build_reports_failure_instead_of_pretending(ws, tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, 'echo "boom" >&2; exit 1\n')
    result = graph_build(ws)
    assert "was not built" in result
    assert "boom" in result


# ---- the numbers behind the GUI panel ----------------------------------------

def test_stats_reads_the_panel_numbers_from_the_graph(ws):
    write_graph(ws)
    data = stats(ws)
    assert data["built"] is True
    assert data["nodes"] == 3 and data["edges"] == 3
    assert data["files"] == 2
    assert data["communities"] == 2
    assert data["extracted_pct"] == 67
    assert data["hubs"][0]["name"] in ("Agent", "Workspace", "ToolError")
    assert data["hubs"][0]["degree"] == 2
    assert data["has_viz"] is False


def test_no_graph_means_built_false_not_an_error(ws):
    assert stats(ws) == {"built": False}
    assert "No graph built" in render_stats(stats(ws))


def test_corrupt_graph_json_is_reported_gently(ws):
    path = ws.root / GRAPH_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    data = stats(ws)
    assert data["built"] is False and "JSON" in data["error"]


def test_render_stats_reads_like_a_report(ws):
    write_graph(ws)
    (ws.root / OUT_DIRNAME / "graph.html").write_text("<html></html>")
    text = render_stats(stats(ws))
    assert "3 nodes, 3 edges" in text
    assert "flows through" in text
    assert "graph.html" in text


# ---- the GUI endpoints --------------------------------------------------------

def _gui(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    from silkcode.gui.server import GuiHandler, GuiState, _stamped_app_html

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": "http://127.0.0.1:1",
                                "default_model": "m"}},
    }))
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    state = GuiState(str(workspace), None, "edit")

    class Handler(GuiHandler):
        pass

    Handler.state = state
    Handler.html = _stamped_app_html()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", workspace, httpd


def test_api_graph_says_not_installed_when_it_is_not(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(graphmod, "cli_path", lambda: None)
    base, _ws, httpd = _gui(tmp_path, monkeypatch)
    try:
        info = httpx.get(f"{base}/api/graph").json()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert info["available"] is False
    assert "pip install graphifyy" in info["hint"]


def test_api_graph_returns_the_panel_numbers(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(graphmod, "cli_path", lambda: "/usr/bin/true")
    base, workspace, httpd = _gui(tmp_path, monkeypatch)
    try:
        write_graph(Workspace(workspace))
        info = httpx.get(f"{base}/api/graph").json()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert info["available"] is True and info["built"] is True
    assert info["nodes"] == 3 and info["hubs"]


def test_graph_view_serves_the_map_and_404s_before_a_build(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(graphmod, "cli_path", lambda: "/usr/bin/true")
    base, workspace, httpd = _gui(tmp_path, monkeypatch)
    try:
        assert httpx.get(f"{base}/graph-view").status_code == 404
        page_dir = workspace / OUT_DIRNAME
        page_dir.mkdir()
        (page_dir / "graph.html").write_text("<html><title>map</title></html>")
        response = httpx.get(f"{base}/graph-view")
        assert response.status_code == 200
        assert "map" in response.text
        assert response.headers["content-type"].startswith("text/html")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_gui_page_carries_the_graph_panel():
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_text()
    assert '<button data-detail="graph">Graph</button>' in app
    assert 'id="graph-details"' in app
    assert "/api/graph/build" in app
    assert "/graph-view" in app


# ---- against the real thing, where it exists ---------------------------------

needs_graphify = pytest.mark.skipif(graphmod.cli_path() is None,
                                    reason="graphify is not installed here")


@needs_graphify
def test_end_to_end_on_a_real_project(ws):
    (ws.root / "app.py").write_text(
        "from helper import greet\n\ndef main():\n    return greet('x')\n")
    (ws.root / "helper.py").write_text("def greet(name):\n    return 'hi ' + name\n")

    report = graph_build(ws)
    assert "nodes" in report and "flows through" in report
    assert (ws.root / GRAPH_RELPATH).is_file()

    explained = graph_explain(ws, "greet()")
    assert "helper.py" in explained

    impact = graph_impact(ws, "greet()")
    assert "app.py" in impact or "main" in impact
