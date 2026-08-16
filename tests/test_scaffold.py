"""Project creation: `silkcode new` and REPL `/new`.

A scaffold that produces a project you cannot immediately run is worse than
no scaffold, so these tests do more than check that files appeared: generated
Python is compiled, generated JSON/TOML is parsed, the pytest suite each
Python template ships is actually executed, and `detect_test_command` is asked
whether it recognizes what we wrote.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from silkcode.scaffold import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    create_project,
    format_result,
    get_template,
    package_name,
    prompt_for_new_project,
    slugify,
    template_names,
    titleize,
    validate_name,
)
from silkcode.tools.testing import detect_test_command
from silkcode.workspace import ToolError, Workspace

try:  # tomllib is 3.11+; the package targets 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    tomllib = None


# ---- names ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("My New App!", "my-new-app"),
    ("  Spaced  Out  ", "spaced-out"),
    ("already-fine", "already-fine"),
    ("Weird***Chars", "weird-chars"),
    ("--leading-dashes--", "leading-dashes"),
    ("UPPER_case", "upper_case"),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected


@pytest.mark.parametrize("slug,expected", [
    ("todo-cli", "todo_cli"),
    ("2fast", "app_2fast"),
    ("class", "class_"),
    ("...", "app"),
])
def test_package_name_is_a_usable_identifier(slug, expected):
    assert package_name(slug) == expected
    assert package_name(slug).isidentifier()


def test_titleize():
    assert titleize("todo-cli") == "Todo Cli"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "--flag", "***", "con"])
def test_validate_name_rejects(bad):
    with pytest.raises(ToolError):
        validate_name(bad)


def test_validate_name_returns_slug():
    assert validate_name("My App") == "my-app"


# ---- templates --------------------------------------------------------------

def test_get_template_unknown_lists_the_real_ones():
    with pytest.raises(ToolError) as exc:
        get_template("cobol")
    for name in template_names():
        assert name in str(exc.value)


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_every_template_creates_a_documented_project(tmp_path, template):
    result = create_project("demo-app", template=template, parent=tmp_path, git=False)
    assert result.path == (tmp_path / "demo-app").resolve()
    assert "README.md" in result.files
    assert "SILKCODE.md" in result.files      # the agent's project instructions
    assert ".gitignore" in result.files
    assert not result.skipped
    for rel in result.files:
        assert (result.path / rel).read_text(), f"{rel} is empty"
    assert result.workspace.root == result.path


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_description_reaches_the_generated_docs(tmp_path, template):
    description = "A tiny widget sorter."
    result = create_project("demo", template=template, parent=tmp_path, git=False,
                            description=description)
    assert description in (result.path / "README.md").read_text()
    assert description in (result.path / "SILKCODE.md").read_text()


@pytest.mark.parametrize("template", ["python", "python-cli"])
def test_python_templates_generate_valid_source(tmp_path, template):
    result = create_project("Todo CLI", template=template, parent=tmp_path, git=False)
    assert result.path.name == "todo-cli"
    sources = [f for f in result.files if f.endswith(".py")]
    assert sources
    for rel in sources:
        path = result.path / rel
        compile(path.read_text(), str(path), "exec")  # SyntaxError fails the test


@pytest.mark.parametrize("template", ["python", "python-cli"])
def test_python_templates_have_a_parseable_pyproject(tmp_path, template):
    result = create_project("Todo CLI", template=template, parent=tmp_path, git=False)
    text = (result.path / "pyproject.toml").read_text()
    if tomllib is None:
        pytest.skip("tomllib requires Python 3.11+")
    data = tomllib.loads(text)
    assert data["project"]["name"] == "todo-cli"
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == ["todo_cli*"]
    if template == "python-cli":
        assert data["project"]["scripts"]["todo-cli"] == "todo_cli.cli:main"


def test_pyproject_description_with_quotes_stays_valid_toml(tmp_path):
    result = create_project("quoted", parent=tmp_path, git=False,
                            description='A "quoted" back\\slash tool')
    text = (result.path / "pyproject.toml").read_text()
    if tomllib is None:
        pytest.skip("tomllib requires Python 3.11+")
    assert tomllib.loads(text)["project"]["description"] == 'A "quoted" back\\slash tool'


@pytest.mark.parametrize("template", ["python", "python-cli"])
def test_generated_pytest_suite_passes(tmp_path, template):
    """The point of the template: `pytest` is green the moment it exists.

    Run from *outside* the project so nothing but the generated conftest.py
    puts the package on sys.path - i.e. the suite passes without the editable
    install too, which is what someone typing `pytest` first will do.
    """
    result = create_project("demo", template=template, parent=tmp_path, git=False)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", str(result.path)],
                          cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_node_template_package_json_is_valid(tmp_path):
    result = create_project("My Node App", template="node", parent=tmp_path, git=False)
    data = json.loads((result.path / "package.json").read_text())
    assert data["name"] == "my-node-app"
    assert data["scripts"]["test"] == "node --test"


def test_web_template_has_no_test_command(tmp_path):
    result = create_project("site", template="web", parent=tmp_path, git=False)
    assert result.test_command is None
    assert (result.path / "index.html").read_text().startswith("<!doctype html>")


@pytest.mark.parametrize("template,expected", [
    ("python", "pytest"),
    ("python-cli", "pytest"),
    ("node", "npm test"),
])
def test_detect_test_command_recognizes_the_scaffold(tmp_path, template, expected):
    result = create_project("demo", template=template, parent=tmp_path, git=False)
    detected = detect_test_command(Workspace(result.path))
    assert detected is not None and expected.split()[0] in detected


# ---- safety -----------------------------------------------------------------

def test_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "important.txt").write_text("do not lose me\n")
    with pytest.raises(ToolError, match="not empty"):
        create_project("taken", parent=tmp_path, git=False)
    assert (tmp_path / "taken" / "important.txt").read_text() == "do not lose me\n"


def test_force_scaffolds_into_a_non_empty_directory_without_overwriting(tmp_path):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "README.md").write_text("mine\n")
    result = create_project("taken", parent=tmp_path, git=False, force=True)
    assert "README.md" in result.skipped
    assert "README.md" not in result.files
    assert (tmp_path / "taken" / "README.md").read_text() == "mine\n"
    assert (tmp_path / "taken" / "SILKCODE.md").is_file()


def test_empty_existing_directory_is_fine(tmp_path):
    (tmp_path / "empty").mkdir()
    result = create_project("empty", parent=tmp_path, git=False)
    assert (result.path / "README.md").is_file()


def test_refuses_when_a_file_occupies_the_path(tmp_path):
    (tmp_path / "taken").write_text("a file\n")
    with pytest.raises(ToolError, match="file already exists"):
        create_project("taken", parent=tmp_path, git=False)


def test_unknown_template_creates_nothing(tmp_path):
    with pytest.raises(ToolError):
        create_project("demo", template="cobol", parent=tmp_path, git=False)
    assert not (tmp_path / "demo").exists()


# ---- git --------------------------------------------------------------------

def _git_env(monkeypatch):
    """A committer identity, so the test does not depend on the machine's."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def test_git_init_and_initial_commit(tmp_path, monkeypatch):
    _git_env(monkeypatch)
    result = create_project("demo", parent=tmp_path, git=True)
    assert result.git == "initialized"
    assert (result.path / ".git").is_dir()
    tracked = subprocess.run(["git", "ls-files"], cwd=result.path,
                             capture_output=True, text=True, timeout=60).stdout.split()
    assert "README.md" in tracked and "pyproject.toml" in tracked
    assert "git error" not in result.commit


def test_no_nested_repository_inside_an_existing_checkout(tmp_path, monkeypatch):
    """A project scaffolded inside a checkout belongs to that repository."""
    _git_env(monkeypatch)
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True, timeout=60)
    result = create_project("inner", parent=outer, git=True)
    assert result.git == "skipped (already inside a git repository)"
    assert not (result.path / ".git").exists()
    assert "already inside a git repository" in format_result(result)


def test_git_can_be_skipped(tmp_path):
    result = create_project("demo", parent=tmp_path, git=False)
    assert result.git == "skipped"
    assert not (result.path / ".git").exists()


def test_no_committer_identity_reports_it_and_keeps_the_repository(tmp_path, monkeypatch):
    """A machine with no `git config user.email` must still get its project."""
    monkeypatch.setattr("silkcode.tools.git.git_commit",
                        lambda ws, message: "git error (exit 128): fatal: empty ident name")
    result = create_project("demo", parent=tmp_path, git=True)
    assert result.git == "initialized"
    assert (result.path / ".git").is_dir()
    text = format_result(result)
    assert "no initial commit" in text and "empty ident name" in text
    assert "git config user.email" in text


def test_git_failure_still_leaves_the_project_on_disk(tmp_path, monkeypatch):
    """No git on PATH is a degraded outcome, not a failed creation."""
    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("silkcode.scaffold.subprocess.run", boom)
    result = create_project("demo", parent=tmp_path, git=True)
    assert result.git.startswith("failed")
    assert (result.path / "README.md").is_file()
    assert "files are still on disk" in format_result(result)


# ---- reporting & prompting --------------------------------------------------

def test_format_result_lists_files_and_next_steps(tmp_path):
    result = create_project("demo", parent=tmp_path, git=False)
    text = format_result(result)
    assert str(result.path) in text
    assert "+ README.md" in text
    assert "pytest" in text          # template next steps
    assert "silkcode ." in text


def test_prompt_for_new_project_uses_the_answers(tmp_path, capsys):
    answers = iter(["My Prompted App", "python-cli", "Built by prompting."])
    result = prompt_for_new_project(asker=lambda _: next(answers), parent=tmp_path)
    assert result.path == (tmp_path / "my-prompted-app").resolve()
    assert result.template == "python-cli"
    assert "Built by prompting." in (result.path / "README.md").read_text()


def test_prompt_for_new_project_defaults_the_template(tmp_path):
    answers = iter(["demo", "", ""])
    result = prompt_for_new_project(asker=lambda _: next(answers), parent=tmp_path)
    assert result.template == DEFAULT_TEMPLATE


def test_prompt_for_new_project_accepts_a_template_number(tmp_path):
    answers = iter(["demo", "2", ""])
    result = prompt_for_new_project(asker=lambda _: next(answers), parent=tmp_path)
    assert result.template == template_names()[1]


def test_prompt_for_new_project_reprompts_on_bad_input(tmp_path, capsys):
    answers = iter(["***", "demo", "cobol", "web", ""])
    result = prompt_for_new_project(asker=lambda _: next(answers), parent=tmp_path)
    assert result.template == "web"
    out = capsys.readouterr().out
    assert "unknown template 'cobol'" in out


def test_prompt_for_new_project_cancels(tmp_path):
    with pytest.raises(ToolError, match="cancelled"):
        prompt_for_new_project(asker=lambda _: "q", parent=tmp_path)
