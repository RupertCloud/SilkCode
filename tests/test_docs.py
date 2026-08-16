"""The published pages: the landing page and the Projects how-to.

Documentation rots silently - a renamed template or a dropped flag stays
"documented" until a user copies a command that no longer works. These tests
read the real pages, pull the commands and template names out of them, and
check them against the code that has to back them up: `scaffold.TEMPLATES` and
`silkcode new --help`. They also check the pages hold together as pages:
in-page links resolve, and nothing is fetched from a third-party host.
"""

from __future__ import annotations

import html
import re
import shlex
from html.parser import HTMLParser
from pathlib import Path

import pytest

from silkcode.scaffold import TEMPLATES

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "docs" / "index.html"
HOWTO = ROOT / "docs" / "projects.html"
APP = ROOT / "silkcode" / "gui" / "app.html"


class _Collector(HTMLParser):
    """Ids, in-page anchor targets, and externally-loaded resources."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.anchors: set[str] = set()
        self.external: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if a.get("id"):
            self.ids.add(a["id"])
        href = a.get("href", "")
        if tag == "a" and href.startswith("#"):
            self.anchors.add(href[1:])
        if tag == "script" and a.get("src", "").startswith(("http://", "https://", "//")):
            self.external.append(a["src"])
        if tag == "link" and a.get("rel") == "stylesheet" and href.startswith(("http", "//")):
            self.external.append(href)


def parse(path: Path) -> _Collector:
    collector = _Collector()
    collector.feed(path.read_text(encoding="utf-8"))
    return collector


def new_command_help() -> str:
    """`silkcode new --help`, straight from the parser that defines it."""
    import contextlib
    import io

    from silkcode.cli.main import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        main(["new", "--help"])
    return buf.getvalue()


def documented_commands(text: str) -> list[str]:
    """Every `silkcode …` command line shown in a code block.

    Only code blocks: the reference tables pair a command fragment with prose,
    and a shell would never be handed those two halves joined together.
    """
    commands = []
    for block in re.findall(r"<pre class=\"code\">(.*?)</pre>", text, re.S):
        for line in html.unescape(re.sub(r"<[^>]+>", "", block)).splitlines():
            line = line.strip().removeprefix("$ ").strip()
            if line.startswith("silkcode "):
                commands.append(line.split("#")[0].strip())
    return commands


# ---- the pages exist and are wired together ---------------------------------

def test_the_landing_page_links_to_the_projects_howto():
    assert HOWTO.is_file(), "docs/projects.html is the page the landing page links to"
    assert 'href="projects.html"' in LANDING.read_text(encoding="utf-8")


def test_every_in_page_link_resolves():
    for path in (LANDING, HOWTO):
        page = parse(path)
        missing = sorted(page.anchors - page.ids)
        assert not missing, f"{path.name} links to missing anchors: {missing}"


def test_the_howto_navigation_covers_its_sections():
    page = parse(HOWTO)
    # the sticky nav is the page's table of contents; every entry must land
    assert {"create", "open", "switch", "reference"} <= page.anchors <= page.ids


def test_the_pages_load_nothing_from_a_third_party():
    """These are shipped as static files (and the GUI page is served offline)."""
    for path in (LANDING, HOWTO, APP):
        assert not parse(path).external, f"{path.name} loads external resources"


def test_the_howto_declares_a_title_and_a_description():
    text = HOWTO.read_text(encoding="utf-8")
    assert "<title>Projects · Silk Code</title>" in text
    assert 'name="description"' in text
    assert 'rel="canonical"' in text


# ---- the pages match the code they describe ---------------------------------

@pytest.mark.parametrize("path", [HOWTO, APP], ids=["howto", "gui"])
def test_every_template_is_documented(path):
    text = path.read_text(encoding="utf-8")
    for name in TEMPLATES:
        assert re.search(rf"<td>{re.escape(name)}</td>", text), \
            f"{path.name} does not list the '{name}' template"


@pytest.mark.parametrize("path", [HOWTO, APP], ids=["howto", "gui"])
def test_no_invented_templates(path):
    """A template table row naming something scaffold.py does not build."""
    text = path.read_text(encoding="utf-8")
    listed = set(re.findall(r"<tr><td>([a-z][a-z-]*)</td><td>", text))
    invented = {n for n in listed if n.startswith("python") or n in ("node", "web", "blank")}
    assert invented <= set(TEMPLATES), f"{path.name} lists unknown templates: {invented - set(TEMPLATES)}"


@pytest.mark.parametrize("path", [LANDING, HOWTO], ids=["landing", "howto"])
def test_documented_new_flags_exist(path):
    """Every flag shown for `silkcode new` is a flag the command accepts."""
    help_text = new_command_help()
    for command in documented_commands(path.read_text(encoding="utf-8")):
        parts = shlex.split(command)
        if parts[1:2] != ["new"]:
            continue
        for token in parts[2:]:
            if token.startswith("-"):
                flag = token.split("=")[0]
                assert flag in help_text, f"{path.name}: `{command}` uses unknown flag {flag}"


def test_the_gui_howto_only_offers_commands_that_exist():
    """The GUI's copy buttons hand the user a command to paste - it has to run."""
    help_text = new_command_help()
    commands = re.findall(r"data-copy=['\"]([^'\"]+)['\"]", APP.read_text(encoding="utf-8"))
    assert commands, "the GUI how-to should offer copyable commands"
    for command in commands:
        parts = shlex.split(html.unescape(command))
        assert parts[0] == "silkcode"
        assert parts[1] == "new"
        for token in parts[2:]:
            if token.startswith("-"):
                assert token in help_text, f"GUI offers `{command}` with unknown flag {token}"


def test_the_gui_exposes_the_howto_and_can_open_a_project_from_it():
    text = APP.read_text(encoding="utf-8")
    assert 'id="projects-help-btn"' in text          # the entry point in the PROJECT pane
    assert 'id="projects-help-modal"' in text
    assert 'id="projects-help-open"' in text         # hands over to the project picker
    assert "openProjectModal()" in text.split('id="projects-help-open"')[1]


def test_the_gui_howto_points_at_the_published_page():
    assert "silkcode.web.app/projects.html" in APP.read_text(encoding="utf-8")
