"""Search and symbol extraction — the tools the model leans on to navigate.

Their failure mode is not an exception, it is a bad answer: the right match
crowded out of a truncated result set, or a symbol list that misses half a
file. So these tests check what comes back, not just that something did.
"""

from __future__ import annotations

import pytest

from silkcode.tools.search import MAX_RESULTS, glob_files, grep
from silkcode.tools.symbols import symbols
from silkcode.workspace import ToolError, Workspace


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "import os\n\n\nclass Widget:\n    def render(self, ctx):\n        return 'needle'\n\n\n"
        "def build(a, b):\n    return Widget()\n\n\nasync def fetch(url):\n    return url\n")
    (root / "src" / "util.js").write_text(
        "export function helper(x) { return x }\n"
        "export const arrow = (a) => a\n"
        "class Thing {}\n")
    (root / "README.md").write_text("nothing to see\n")
    return Workspace(root)


@pytest.fixture
def with_node_modules(project):
    root = project.root
    vendor = root / "node_modules" / "left-pad"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("// needle\n" * 300)
    build = root / "dist"
    build.mkdir()
    (build / "bundle.js").write_text("// needle\n" * 300)
    return project


# --------------------------------------------------------------------------
# grep
# --------------------------------------------------------------------------

def test_grep_finds_a_match_with_file_and_line(project):
    out = grep(project, "needle")
    assert out.splitlines() == ["src/app.py:6:return 'needle'"]


def test_grep_reports_no_matches_plainly(project):
    assert grep(project, "definitely-not-here") == "No matches found."


def test_grep_rejects_a_bad_regex(project):
    with pytest.raises(ToolError, match="Invalid regex"):
        grep(project, "unclosed(")


def test_grep_supports_regex_not_just_literals(project):
    out = grep(project, r"^def \w+\(")
    assert "src/app.py:9:def build(a, b):" in out


def test_grep_can_be_scoped_to_a_path(project):
    assert "No matches found." == grep(project, "needle", path="src/util.js")
    assert "app.py" in grep(project, "needle", path="src/app.py")


def test_grep_skips_dependency_directories_by_default(with_node_modules):
    """Results are sorted, so node_modules/... sorts ahead of src/... and
    would spend the whole result budget before reaching the project's own
    code — the one match that mattered would never be shown."""
    out = grep(with_node_modules, "needle")
    assert out.splitlines() == ["src/app.py:6:return 'needle'"]


def test_grep_skips_build_output_too(with_node_modules):
    assert "dist/" not in grep(with_node_modules, "needle")


def test_grep_searches_vendored_code_when_asked_directly(with_node_modules):
    """Skipping node_modules is a default, not a rule."""
    assert "node_modules" in grep(with_node_modules, "needle", path="node_modules")
    assert "node_modules" in grep(with_node_modules, "needle",
                                  glob="node_modules/**/*.js")


def test_grep_truncates_a_huge_result_set(project):
    (project.root / "many.txt").write_text("needle\n" * (MAX_RESULTS + 50))
    out = grep(project, "needle")
    assert "truncated" in out
    assert len(out.splitlines()) <= MAX_RESULTS + 1


def test_grep_ignores_binary_files(project):
    (project.root / "blob.bin").write_bytes(b"needle\x00\x01\x02needle")
    assert "blob.bin" not in grep(project, "needle")


def test_grep_skips_files_over_the_size_limit(project, monkeypatch):
    monkeypatch.setattr("silkcode.tools.search.MAX_FILE_BYTES", 10)
    assert "app.py" not in grep(project, "needle")


def test_grep_never_leaves_the_workspace(project):
    with pytest.raises(ToolError):
        grep(project, "needle", path="../..")


# --------------------------------------------------------------------------
# glob
# --------------------------------------------------------------------------

def test_glob_lists_matching_files(project):
    assert glob_files(project, "**/*.py").splitlines() == ["src/app.py"]


def test_glob_reports_nothing_matched(project):
    assert glob_files(project, "**/*.rs") == "No files matched."


def test_glob_skips_dependency_directories_by_default(with_node_modules):
    assert glob_files(with_node_modules, "**/*.js").splitlines() == ["src/util.js"]


def test_glob_lists_vendored_files_when_asked_directly(with_node_modules):
    out = glob_files(with_node_modules, "node_modules/**/*.js")
    assert "node_modules/left-pad/index.js" in out


def test_glob_truncates_a_huge_listing(project):
    big = project.root / "gen"
    big.mkdir()
    for i in range(MAX_RESULTS + 20):
        (big / f"f{i}.txt").write_text("")
    out = glob_files(project, "gen/*.txt")
    assert "more matches" in out


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------

def test_symbols_finds_python_classes_and_functions(project):
    out = symbols(project)
    assert "src/app.py:4: class Widget" in out
    assert "src/app.py:5: def render(self, ctx)" in out
    assert "src/app.py:9: def build(a, b)" in out


def test_symbols_includes_async_definitions(project):
    assert "def fetch(url)" in symbols(project)


def test_symbols_reads_javascript(project):
    out = symbols(project)
    assert "src/util.js:1: function helper" in out
    assert "src/util.js:2: function arrow" in out
    assert "src/util.js:3: class Thing" in out


def test_symbols_can_target_one_file(project):
    out = symbols(project, path="src/util.js")
    assert "util.js" in out and "app.py" not in out


def test_symbols_skips_dependency_directories(with_node_modules):
    assert "node_modules" not in symbols(with_node_modules)


def test_symbols_survives_a_file_that_does_not_parse(project):
    (project.root / "src" / "broken.py").write_text("def oops(:\n")
    out = symbols(project)
    assert "class Widget" in out, "one bad file must not lose the rest"


def test_symbols_reports_nothing_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("no code")
    assert symbols(Workspace(empty)) == "No symbols found."


def test_symbols_handles_go_and_rust(tmp_path):
    root = tmp_path / "polyglot"
    root.mkdir()
    (root / "main.go").write_text("func Serve(addr string) {}\ntype Server struct {}\n")
    (root / "lib.rs").write_text(
        "pub fn run() {}\nstruct Config {}\nenum Mode {}\ntrait Handler {}\n")
    out = symbols(Workspace(root))
    assert "function Serve" in out and "class Server" in out
    assert "function run" in out and "class Config" in out
    assert "enum Mode" in out and "trait Handler" in out


def test_symbols_truncates_a_huge_index(tmp_path):
    from silkcode.tools.symbols import MAX_LINES
    root = tmp_path / "many"
    root.mkdir()
    (root / "big.py").write_text("".join(f"def f{i}():\n    pass\n"
                                         for i in range(MAX_LINES + 100)))
    out = symbols(Workspace(root))
    assert "truncated" in out
