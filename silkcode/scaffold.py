"""Create new projects from templates (`silkcode new`, REPL `/new`).

`project.py` answers "which existing project do I work on?" — clone a GitHub
repo or open a local directory. This module answers the other half: "there is
no project yet, make me one." It writes a small but *complete* starting point:
source, tests a runner can find (`silkcode test` auto-detects the command),
a README, a `.gitignore`, and a `SILKCODE.md` with project instructions the
agent reads on every turn (see `context.build_context`).

Everything here is plain text generation plus `git init` — no network, no
third-party dependencies, no cookiecutter-style template repos to fetch. A
template is a name, a description, and a function from project metadata to
{relative path: file content}.
"""

from __future__ import annotations

import json
import keyword
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .workspace import ToolError, Workspace

# Directory names we refuse to create: reserved on Windows, or too easy to
# type by accident in a way that would scaffold somewhere surprising.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", ".", "..",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_SLUG_STRIP = re.compile(r"[^a-z0-9._-]+")
_SLUG_EDGES = re.compile(r"^[-._]+|[-._]+$")


def slugify(name: str) -> str:
    """A directory-safe project name: 'My New App!' -> 'my-new-app'."""
    slug = _SLUG_STRIP.sub("-", name.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug)
    return _SLUG_EDGES.sub("", slug)


def package_name(slug: str) -> str:
    """A Python/JS-safe identifier derived from the slug."""
    ident = re.sub(r"[^a-z0-9_]", "_", slug.lower())
    ident = re.sub(r"_{2,}", "_", ident).strip("_")
    if not ident or not ident[0].isalpha():
        ident = f"app_{ident}" if ident else "app"
    if keyword.iskeyword(ident):
        ident += "_"
    return ident


def titleize(slug: str) -> str:
    return " ".join(word.capitalize() for word in re.split(r"[-_.]+", slug) if word) or slug


def validate_name(name: str) -> str:
    """Check a user-supplied project name and return its slug.

    Raises ToolError with a message meant for the person who typed it.
    """
    raw = name.strip()
    if not raw:
        raise ToolError("project name must not be empty")
    if any(sep in raw for sep in ("/", "\\")):
        raise ToolError(f"project name must not contain a path separator: {raw!r} "
                        "(use --dir to choose where it is created)")
    if raw.startswith("-"):
        raise ToolError(f"project name must not start with '-': {raw!r}")
    slug = slugify(raw)
    if not slug:
        raise ToolError(f"project name {raw!r} has no usable characters "
                        "(letters, digits, '-', '_' and '.')")
    if slug in _RESERVED_NAMES or slug.split(".")[0] in _RESERVED_NAMES:
        raise ToolError(f"'{slug}' is a reserved directory name; pick another")
    return slug


@dataclass(frozen=True)
class ProjectMeta:
    """What the templates get to work with."""

    slug: str            # directory name, e.g. "todo-cli"
    package: str         # import-safe identifier, e.g. "todo_cli"
    title: str           # human title, e.g. "Todo Cli"
    description: str     # one-line description (may be empty)

    @property
    def summary(self) -> str:
        return self.description or f"{self.title} — a new project created with Silk Code."


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    build: Callable[[ProjectMeta], dict[str, str]]
    test_command: str | None = None
    next_steps: tuple[str, ...] = ()


@dataclass
class ScaffoldResult:
    path: Path
    template: str
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    git: str = "skipped"          # "initialized" | "skipped" | "failed: ..."
    commit: str = ""
    test_command: str | None = None
    next_steps: list[str] = field(default_factory=list)

    @property
    def workspace(self) -> Workspace:
        return Workspace(self.path)


# ---- shared file fragments --------------------------------------------------

def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _readme(meta: ProjectMeta, usage: str, test_command: str | None) -> str:
    parts = [f"# {meta.title}\n", f"{meta.summary}\n", "## Usage\n", usage.rstrip() + "\n"]
    if test_command:
        parts.append(f"## Tests\n\n```bash\n{test_command}\n```\n")
    parts.append("---\n\nCreated with [Silk Code](https://silkcode.web.app) "
                 "(`silkcode new`). Work on it with `silkcode .`.\n")
    return "\n".join(parts)


def _instructions(meta: ProjectMeta, layout: str, test_command: str | None) -> str:
    """SILKCODE.md — read by the agent on every turn (context.build_context)."""
    lines = [
        f"# {meta.title}",
        "",
        meta.summary,
        "",
        "## Layout",
        "",
        layout.strip(),
        "",
        "## Conventions",
        "",
        "- Keep changes small and focused; explain anything non-obvious in a comment.",
        "- Add or update a test for every behavior change.",
    ]
    if test_command:
        lines.append(f"- Run `{test_command}` before committing; the suite must be green.")
    lines += [
        "",
        "## Notes",
        "",
        "(Replace this section with anything the agent should know: architecture",
        "decisions, external services, deployment steps.)",
        "",
    ]
    return "\n".join(lines)


_GITIGNORE_COMMON = """.DS_Store
*.log
.env
.env.*
.idea/
.vscode/
"""

_GITIGNORE_PYTHON = """__pycache__/
*.py[cod]
.venv/
venv/
build/
dist/
*.egg-info/
.pytest_cache/
.coverage
""" + _GITIGNORE_COMMON

_GITIGNORE_NODE = """node_modules/
dist/
build/
coverage/
npm-debug.log*
""" + _GITIGNORE_COMMON


# ---- templates --------------------------------------------------------------

def _python_pyproject(meta: ProjectMeta, *, script: str | None = None) -> str:
    script_block = ""
    if script:
        script_block = f'\n[project.scripts]\n{meta.slug} = {_toml_str(script)}\n'
    return f"""[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = {_toml_str(meta.slug)}
version = "0.1.0"
description = {_toml_str(meta.summary)}
readme = "README.md"
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8"]
{script_block}
[tool.setuptools.packages.find]
include = [{_toml_str(meta.package + "*")}]
"""


# An empty conftest.py at the root puts the project directory on sys.path when
# pytest runs, so `pytest` works before `pip install -e .` - a fresh project
# whose own test suite cannot import it is not a working starting point.
_CONFTEST = ('"""Makes the project root importable so `pytest` works before an '
             'editable install."""\n')


def _build_python(meta: ProjectMeta) -> dict[str, str]:
    pkg = meta.package
    return {
        "pyproject.toml": _python_pyproject(meta),
        "conftest.py": _CONFTEST,
        ".gitignore": _GITIGNORE_PYTHON,
        "README.md": _readme(
            meta,
            f"```bash\npip install -e '.[dev]'\npython -c "
            f"\"import {pkg}; print({pkg}.greet('world'))\"\n```",
            "pytest",
        ),
        "SILKCODE.md": _instructions(
            meta,
            f"- `{pkg}/` — the package\n"
            f"- `{pkg}/core.py` — the logic\n"
            f"- `tests/` — pytest suite, one module per source module",
            "pytest",
        ),
        f"{pkg}/__init__.py": f'"""{meta.summary}"""\n\n'
                              f'from .core import greet\n\n'
                              f'__version__ = "0.1.0"\n'
                              f'__all__ = ["greet"]\n',
        f"{pkg}/core.py": f'"""Core logic for {meta.title}."""\n\n'
                          f'from __future__ import annotations\n\n\n'
                          f'def greet(name: str) -> str:\n'
                          f'    """Return a greeting for `name`."""\n'
                          f'    if not name.strip():\n'
                          f'        raise ValueError("name must not be empty")\n'
                          f'    return f"Hello, {{name}}!"\n',
        "tests/test_core.py": f'import pytest\n\n'
                              f'from {pkg} import greet\n\n\n'
                              f'def test_greet():\n'
                              f'    assert greet("world") == "Hello, world!"\n\n\n'
                              f'def test_greet_rejects_empty_name():\n'
                              f'    with pytest.raises(ValueError):\n'
                              f'        greet("   ")\n',
    }


def _build_python_cli(meta: ProjectMeta) -> dict[str, str]:
    pkg = meta.package
    return {
        "pyproject.toml": _python_pyproject(meta, script=f"{pkg}.cli:main"),
        "conftest.py": _CONFTEST,
        ".gitignore": _GITIGNORE_PYTHON,
        "README.md": _readme(
            meta,
            f"```bash\npip install -e '.[dev]'\n{meta.slug} --help\n{meta.slug} world\n```",
            "pytest",
        ),
        "SILKCODE.md": _instructions(
            meta,
            f"- `{pkg}/cli.py` — argument parsing and the `main()` entry point\n"
            f"- `{pkg}/core.py` — the logic the CLI calls (keep it importable and testable)\n"
            f"- `tests/` — pytest suite; drive the CLI through `main([...])`",
            "pytest",
        ),
        f"{pkg}/__init__.py": f'"""{meta.summary}"""\n\n__version__ = "0.1.0"\n',
        f"{pkg}/core.py": f'"""Core logic for {meta.title} (kept separate from the CLI)."""\n\n'
                          f'from __future__ import annotations\n\n\n'
                          f'def greet(name: str, *, shout: bool = False) -> str:\n'
                          f'    """Return a greeting for `name`."""\n'
                          f'    if not name.strip():\n'
                          f'        raise ValueError("name must not be empty")\n'
                          f'    message = f"Hello, {{name}}!"\n'
                          f'    return message.upper() if shout else message\n',
        f"{pkg}/cli.py": f'"""Command line entry point for {meta.title}."""\n\n'
                         f'from __future__ import annotations\n\n'
                         f'import argparse\n'
                         f'import sys\n\n'
                         f'from . import __version__\n'
                         f'from .core import greet\n\n\n'
                         f'def build_parser() -> argparse.ArgumentParser:\n'
                         f'    parser = argparse.ArgumentParser(\n'
                         f'        prog="{meta.slug}", description={_py_str(meta.summary)})\n'
                         f'    parser.add_argument("name", help="who to greet")\n'
                         f'    parser.add_argument("--shout", action="store_true",\n'
                         f'                        help="greet in upper case")\n'
                         f'    parser.add_argument("--version", action="version",\n'
                         f'                        version=f"{meta.slug} {{__version__}}")\n'
                         f'    return parser\n\n\n'
                         f'def main(argv: list[str] | None = None) -> int:\n'
                         f'    args = build_parser().parse_args(argv)\n'
                         f'    try:\n'
                         f'        print(greet(args.name, shout=args.shout))\n'
                         f'    except ValueError as exc:\n'
                         f'        print(f"error: {{exc}}", file=sys.stderr)\n'
                         f'        return 1\n'
                         f'    return 0\n\n\n'
                         f'if __name__ == "__main__":\n'
                         f'    sys.exit(main())\n',
        f"{pkg}/__main__.py": f'import sys\n\nfrom .cli import main\n\n'
                              f'sys.exit(main())\n',
        "tests/test_cli.py": f'import pytest\n\n'
                             f'from {pkg}.cli import main\n\n\n'
                             f'def test_greets(capsys):\n'
                             f'    assert main(["world"]) == 0\n'
                             f'    assert capsys.readouterr().out.strip() == "Hello, world!"\n\n\n'
                             f'def test_shout(capsys):\n'
                             f'    assert main(["world", "--shout"]) == 0\n'
                             f'    assert capsys.readouterr().out.strip() == "HELLO, WORLD!"\n\n\n'
                             f'def test_rejects_blank_name(capsys):\n'
                             f'    assert main(["   "]) == 1\n'
                             f'    assert "error:" in capsys.readouterr().err\n',
    }


def _build_node(meta: ProjectMeta) -> dict[str, str]:
    package_json = {
        "name": meta.slug,
        "version": "0.1.0",
        "description": meta.summary,
        "type": "module",
        "main": "src/index.js",
        "scripts": {
            "start": "node src/index.js",
            "test": "node --test",
        },
        "license": "MIT",
    }
    return {
        "package.json": json.dumps(package_json, indent=2) + "\n",
        ".gitignore": _GITIGNORE_NODE,
        "README.md": _readme(meta, "```bash\nnpm start\n```", "npm test"),
        "SILKCODE.md": _instructions(
            meta,
            "- `src/index.js` — entry point and exported functions (ES modules)\n"
            "- `test/` — `node:test` suite; no test framework to install",
            "npm test",
        ),
        "src/index.js": f"""// {meta.summary}

export function greet(name) {{
  if (!name || !name.trim()) {{
    throw new Error('name must not be empty');
  }}
  return `Hello, ${{name}}!`;
}}

if (import.meta.url === `file://${{process.argv[1]}}`) {{
  console.log(greet(process.argv[2] ?? 'world'));
}}
""",
        "test/index.test.js": """import assert from 'node:assert/strict';
import test from 'node:test';

import { greet } from '../src/index.js';

test('greets by name', () => {
  assert.equal(greet('world'), 'Hello, world!');
});

test('rejects an empty name', () => {
  assert.throws(() => greet('  '), /must not be empty/);
});
""",
    }


def _build_web(meta: ProjectMeta) -> dict[str, str]:
    return {
        ".gitignore": _GITIGNORE_NODE,
        "README.md": _readme(
            meta,
            "Open `index.html` in a browser, or serve the folder:\n\n"
            "```bash\npython -m http.server 8000\n```",
            None,
        ),
        "SILKCODE.md": _instructions(
            meta,
            "- `index.html` — markup\n"
            "- `styles.css` — styling (no build step, no framework)\n"
            "- `main.js` — behavior, loaded as an ES module",
            None,
        ),
        "index.html": f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{meta.title}</title>
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <main>
      <h1>{meta.title}</h1>
      <p>{meta.summary}</p>
      <button id="greet">Say hello</button>
      <p id="output" role="status"></p>
    </main>
    <script type="module" src="main.js"></script>
  </body>
</html>
""",
        "styles.css": """:root {
  color-scheme: light dark;
  --fg: #14161a;
  --bg: #ffffff;
  --accent: #4f46e5;
}

@media (prefers-color-scheme: dark) {
  :root {
    --fg: #f2f3f5;
    --bg: #14161a;
  }
}

body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  font: 16px/1.5 system-ui, sans-serif;
  color: var(--fg);
  background: var(--bg);
}

main {
  max-width: 40rem;
  padding: 2rem;
  text-align: center;
}

button {
  font: inherit;
  padding: 0.6rem 1.2rem;
  border: 0;
  border-radius: 0.5rem;
  color: #fff;
  background: var(--accent);
  cursor: pointer;
}
""",
        "main.js": """const output = document.querySelector('#output');

document.querySelector('#greet').addEventListener('click', () => {
  output.textContent = `Hello, world! It is ${new Date().toLocaleTimeString()}.`;
});
""",
    }


def _build_blank(meta: ProjectMeta) -> dict[str, str]:
    return {
        ".gitignore": _GITIGNORE_COMMON,
        "README.md": _readme(meta, "```bash\nsilkcode .\n```", None),
        "SILKCODE.md": _instructions(
            meta,
            "(Empty project — describe the layout here as it takes shape.)",
            None,
        ),
    }


def _py_str(value: str) -> str:
    """A Python string literal for generated source."""
    return json.dumps(value)


TEMPLATES: dict[str, Template] = {
    "python": Template(
        name="python",
        description="Python package with a pytest suite and pyproject.toml",
        build=_build_python,
        test_command="pytest",
        next_steps=("pip install -e '.[dev]'", "pytest"),
    ),
    "python-cli": Template(
        name="python-cli",
        description="Python command line tool (argparse entry point + tests)",
        build=_build_python_cli,
        test_command="pytest",
        next_steps=("pip install -e '.[dev]'", "pytest"),
    ),
    "node": Template(
        name="node",
        description="Node.js ES-module package with a node:test suite",
        build=_build_node,
        test_command="npm test",
        next_steps=("npm test", "npm start"),
    ),
    "web": Template(
        name="web",
        description="Static web page (HTML + CSS + JS, no build step)",
        build=_build_web,
        next_steps=("python -m http.server 8000",),
    ),
    "blank": Template(
        name="blank",
        description="README, .gitignore and SILKCODE.md only",
        build=_build_blank,
    ),
}

DEFAULT_TEMPLATE = "python"


def template_names() -> list[str]:
    return list(TEMPLATES)


def get_template(name: str) -> Template:
    try:
        return TEMPLATES[name]
    except KeyError:
        raise ToolError(f"unknown template '{name}'; available: "
                        f"{', '.join(template_names())}") from None


# ---- creation ---------------------------------------------------------------

def target_path(slug: str, parent: str | Path = ".") -> Path:
    return (Path(parent).expanduser() / slug).resolve()


def create_project(name: str, template: str = DEFAULT_TEMPLATE, parent: str | Path = ".",
                   description: str = "", git: bool = True,
                   force: bool = False) -> ScaffoldResult:
    """Create `<parent>/<slug>` from a template and return what was written.

    Existing files are never overwritten - they are reported in
    `ScaffoldResult.skipped`. A non-empty target directory is refused unless
    `force` is set, so `silkcode new` cannot quietly rain files into an
    unrelated tree.
    """
    slug = validate_name(name)
    tpl = get_template(template)

    parent_dir = Path(parent).expanduser()
    if parent_dir.exists() and not parent_dir.is_dir():
        raise ToolError(f"not a directory: {parent_dir}")
    root = target_path(slug, parent_dir)
    if root.exists():
        if not root.is_dir():
            raise ToolError(f"a file already exists at {root}")
        if any(root.iterdir()) and not force:
            raise ToolError(f"{root} already exists and is not empty "
                            "(pass --force to scaffold into it anyway)")

    meta = ProjectMeta(slug=slug, package=package_name(slug), title=titleize(slug),
                       description=description.strip())
    files = tpl.build(meta)

    result = ScaffoldResult(path=root, template=tpl.name, test_command=tpl.test_command,
                            next_steps=list(tpl.next_steps))
    try:
        root.mkdir(parents=True, exist_ok=True)
        for rel in sorted(files):
            dest = root / rel
            if dest.exists():
                result.skipped.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(files[rel], encoding="utf-8")
            result.files.append(rel)
    except OSError as exc:
        raise ToolError(f"could not create {root}: {exc}") from exc

    if git:
        _init_git(result)
    return result


def _inside_work_tree(path: Path) -> bool:
    try:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _init_git(result: ScaffoldResult) -> None:
    """`git init` + an initial commit, best effort.

    A missing git, or a machine with no committer identity configured, must not
    turn a successfully scaffolded project into a failure - the files are on
    disk either way, so we record what happened and let the caller print it.
    """
    from .tools.git import git_commit

    if (result.path / ".git").is_dir():
        result.git = "already a git repository"
        return
    if _inside_work_tree(result.path):
        # Scaffolding into a checkout (`silkcode new examples/demo`) should add
        # files to that repository, not bury a second one inside it.
        result.git = "skipped (already inside a git repository)"
        return
    try:
        proc = subprocess.run(["git", "init", "-q"], cwd=result.path,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        result.git = f"failed: {exc}"
        return
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        result.git = f"failed: {detail[0] if detail else 'git init failed'}"
        return
    result.git = "initialized"

    outcome = git_commit(Workspace(result.path), f"Initial commit ({result.template} template)")
    if outcome.startswith("git error"):
        result.commit = outcome
    else:
        result.commit = outcome.removeprefix("Committed: ").strip()


def format_result(result: ScaffoldResult) -> str:
    """Human-readable summary printed by `silkcode new` and REPL `/new`."""
    lines = [f"Created {result.path}  ({result.template} template)"]
    for rel in result.files:
        lines.append(f"  + {rel}")
    for rel in result.skipped:
        lines.append(f"  = {rel} (kept, already existed)")
    commit_failed = result.commit.startswith("git error")
    if result.git == "initialized":
        lines.append("  git: repository initialized"
                     + (f", {result.commit}" if result.commit and not commit_failed else ""))
        if commit_failed:
            detail = result.commit.removeprefix("git error").lstrip(": ")
            lines.append(f"  git: no initial commit - {detail}")
            lines.append("       (commit it yourself once git knows who you are: "
                         "git config user.email ...)")
    elif result.git.startswith("failed"):
        lines.append(f"  git: {result.git} (files are still on disk)")
    elif result.git != "skipped":
        lines.append(f"  git: {result.git}")
    steps = list(result.next_steps)
    if steps:
        lines.append("\nNext:")
        lines += [f"  cd {result.path.name}"] + [f"  {s}" for s in steps]
        lines.append("  silkcode .          # work on it with the agent")
    else:
        lines.append(f"\nNext:\n  cd {result.path.name}\n  silkcode .")
    return "\n".join(lines)


def prompt_for_new_project(asker=None, parent: str | Path = ".") -> ScaffoldResult:
    """Interactive `silkcode new` / `/new`: ask for a name and a template."""

    def _ask(prompt: str) -> str:
        return (asker(prompt) if asker else input(prompt)).strip()

    names = template_names()
    print("\nCreate a new project.\n")
    print("Templates:")
    for i, name in enumerate(names, 1):
        marker = " (default)" if name == DEFAULT_TEMPLATE else ""
        print(f"  {i:>2}. {name:<12} {TEMPLATES[name].description}{marker}")

    while True:
        try:
            raw_name = _ask("\nProject name (q to cancel): ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise ToolError("cancelled")
        if raw_name in ("q", "quit", "cancel"):
            raise ToolError("cancelled")
        if not raw_name:
            continue
        try:
            slug = validate_name(raw_name)
        except ToolError as exc:
            print(f"  {exc}")
            continue
        break

    while True:
        try:
            choice = _ask(f"Template [{DEFAULT_TEMPLATE}]: ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise ToolError("cancelled")
        if not choice:
            template = DEFAULT_TEMPLATE
            break
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            template = names[int(choice) - 1]
            break
        if choice in TEMPLATES:
            template = choice
            break
        print(f"  unknown template '{choice}'; pick a number or one of: {', '.join(names)}")

    try:
        description = _ask("One-line description (optional): ")
    except (EOFError, KeyboardInterrupt):
        print()
        description = ""

    print(f"\nCreating {target_path(slug, parent)} ...")
    return create_project(slug, template=template, parent=parent, description=description)
