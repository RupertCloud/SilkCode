"""Remote-workspace integration tests: a repo that lives entirely in a sandbox.

These spin up the reference sandbox server (silkcode.sandbox_server) and point
a RemoteWorkspace at a git repo, then exercise the tools stack end to end. The
guarantee under test: the repo files never land on the local machine.
"""

import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from silkcode.execbackend import RemoteBackend
from silkcode.gui.server import GuiState
from silkcode.remotews import RemoteWorkspace, normalize_repo_url
from silkcode.sandbox_server import SandboxHandler, SandboxState


def test_normalize_repo_url():
    assert normalize_repo_url("github:owner/repo") == "https://github.com/owner/repo.git"
    assert normalize_repo_url("github:owner/repo.git") == "https://github.com/owner/repo.git"
    assert normalize_repo_url("https://github.com/owner/repo") == "https://github.com/owner/repo"
    assert normalize_repo_url("/tmp/foo") == "/tmp/foo"
    assert normalize_repo_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"


@pytest.fixture
def sandbox_and_repo(tmp_path):
    """A git repo with a commit, a bare 'remote' to clone from, and a running
    sandbox server. Returns (backend, repo_url, base)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("# demo project\n")
    (src / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (src / "lib").mkdir()
    (src / "lib" / "util.py").write_text("def helper():\n    return 42\n")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "initial"], check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    state = SandboxState(tmp_path / "sandbox", "secret-token")
    handler = type("Handler", (SandboxHandler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    backend = RemoteBackend(f"http://127.0.0.1:{httpd.server_address[1]}", token="secret-token")
    yield backend, str(bare), tmp_path
    httpd.shutdown()
    httpd.server_close()


def test_remote_clone_lists_reads_writes_git_exec(sandbox_and_repo):
    backend, repo_url, _base = sandbox_and_repo
    ws = RemoteWorkspace(backend, repo_url)

    assert ws.list_files() == ["README.md", "app.py", "lib/util.py"]
    assert "return a + b" in ws.read_text("app.py")
    assert backend.grep(ws.workspace_id, "def add") == ["app.py:1:def add(a, b):"]

    ws.write_text("new.py", "x = 1\n")
    status = ws.git("status", "--short")
    assert "?? new.py" in status

    out = ws.exec("pwd").strip()
    assert "sandbox" in out  # ran inside the sandbox's clone

    # the local scratch root holds no repo files at all
    assert list(Path(ws.root).rglob("*")) == []


def test_remote_tools_stack(sandbox_and_repo):
    from silkcode.context import load_project_instructions
    from silkcode.repomap import repo_map
    from silkcode.tools.files import edit_file, read_file, write_file
    from silkcode.tools.git import git_diff, git_status
    from silkcode.tools.search import glob_files, grep
    from silkcode.tools.symbols import symbols

    backend, repo_url, _base = sandbox_and_repo
    ws = RemoteWorkspace(backend, repo_url)

    assert "def add" in read_file(ws, "app.py")
    write_file(ws, "hello.py", "print('hi')\n")
    edit_file(ws, "app.py", "return a + b", "return a + b + 1")
    assert "hello.py" in glob_files(ws, "**/*.py")
    assert grep(ws, "def add") == "app.py:1:def add(a, b):"
    assert any("def add" in s for s in symbols(ws).splitlines())
    assert "?? hello.py" in git_status(ws)
    assert "+" in git_diff(ws)
    assert "Python" in repo_map(ws)
    assert load_project_instructions(ws) == ""


def test_remote_gui_state(sandbox_and_repo, monkeypatch):
    backend, repo_url, base = sandbox_and_repo
    home = base / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": "http://stub.invalid/v1",
                               "default_model": "stub-model"}},
        "sandbox": {"url": backend.url, "token": "secret-token"},
    }))

    st = GuiState(str(base / "local"), None, "ask", instance="127.0.0.1:9999", remote=repo_url)
    assert isinstance(st.workspace, RemoteWorkspace)

    tree = st.tree()
    assert {e["path"] for e in tree} == {"README.md", "app.py", "lib", "lib/util.py"}
    assert "def add" in st.read_file("app.py")["content"]
    assert st.diff()["status"]  # git status over the sandbox clone

    from silkcode.tools.files import write_file
    write_file(st.workspace, "new.py", "x = 1\n", _owner=f"session-{st.default_session_id}")
    assert "?? new.py" in st.diff()["status"]

    # the guarantee: nothing but the scratch .silkcode dir on this machine
    local = {str(p.relative_to(Path(st.workspace.root))) for p in Path(st.workspace.root).rglob("*")}
    assert all(f.startswith(".silkcode") for f in local)


def test_memory_written_in_a_remote_session_can_be_read_back(sandbox_and_repo):
    """remember() and load_memory() must agree on where memory lives.

    The local root of a remote workspace is a scratch directory that stays
    empty by design, so appending there writes the note somewhere
    load_memory never looks and the session discards - the agent is told it
    remembered something that was already gone.
    """
    from silkcode.memory import load_memory, remember

    backend, repo_url, _base = sandbox_and_repo
    ws = RemoteWorkspace(backend, repo_url)

    assert load_memory(ws) == ""
    remember(ws, "the parser is generated, do not edit it by hand")
    assert "do not edit it by hand" in load_memory(ws)

    # a second note must not replace the first
    remember(ws, "deploys run from the release branch")
    text = load_memory(ws)
    assert "do not edit it by hand" in text and "release branch" in text

    # and it lives in the sandbox, not on this machine
    assert list(Path(ws.root).rglob("*")) == []


def test_the_remote_tree_is_built_without_rescanning_itself(sandbox_and_repo):
    """The tree is rebuilt after every turn, so its cost is paid constantly.

    Deduplicating directories by scanning the entries list is quadratic in
    the number of distinct directories - a packages/pkg*/src layout took
    120ms where a set takes 6ms. This pins the behaviour (correct tree, each
    directory once) and puts a ceiling on the time.
    """
    import time

    from silkcode.gui.server import GuiState

    backend, repo_url, base = sandbox_and_repo
    ws = RemoteWorkspace(backend, repo_url)
    ws._files_cache = {f"packages/pkg{i // 5}/src/file{i}.ts" for i in range(2000)}

    state = GuiState.__new__(GuiState)
    session = type("S", (), {"workspace": ws})()
    state.get_session = lambda sid=None: session

    started = time.perf_counter()
    entries = GuiState.tree(state)
    elapsed = time.perf_counter() - started

    paths = [e["path"] for e in entries]
    assert len(paths) == len(set(paths)), "a directory was emitted more than once"
    dirs = [e["path"] for e in entries if e["dir"]]
    assert "packages" in dirs and any(d.startswith("packages/pkg") for d in dirs)
    for entry in entries:
        assert entry["depth"] == entry["path"].count("/")
    assert elapsed < 1.0, f"tree build took {elapsed:.2f}s"
