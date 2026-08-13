from silkcode.tools.symbols import symbols
from silkcode.workspace import Workspace


def test_python_symbols(tmp_path):
    (tmp_path / "app.py").write_text(
        "class Auth:\n"
        "    def login(self, user):\n"
        "        pass\n"
        "\n"
        "def helper(a, b):\n"
        "    return a\n"
    )
    out = symbols(Workspace(tmp_path))
    assert "app.py:1: class Auth" in out
    assert "app.py:2: def login(self, user)" in out
    assert "app.py:5: def helper(a, b)" in out


def test_js_ts_symbols(tmp_path):
    (tmp_path / "web.ts").write_text(
        "export class Router {}\n"
        "export async function fetchData(url) {}\n"
        "const handler = async (req) => {}\n"
    )
    out = symbols(Workspace(tmp_path))
    assert "web.ts:1: class Router" in out
    assert "web.ts:2: function fetchData" in out
    assert "web.ts:3: function handler" in out


def test_symbols_bounded_on_wide_trees(tmp_path):
    from silkcode.tools.symbols import MAX_FILES, _collect_source_files
    for i in range(40):
        d = tmp_path / f"pkg{i:02}"
        d.mkdir()
        for j in range(10):
            (d / f"m{j}.py").write_text("def f(): pass\n")
    files = _collect_source_files(tmp_path)
    assert len(files) == MAX_FILES


def test_symbols_skips_ignored_and_handles_empty(tmp_path):
    hidden = tmp_path / "node_modules"
    hidden.mkdir()
    (hidden / "dep.js").write_text("function secret() {}")
    out = symbols(Workspace(tmp_path))
    assert out == "No symbols found."
