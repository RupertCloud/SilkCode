"""Letting the agent look at the page it just wrote.

`live_server` serves the workspace so a person can watch it reload. This is
the other half: what a browser knows, in words, because the provider layer
carries strings and a screenshot handed to a model is a file it cannot read.

`review_url` is the tool; `silkcode.browser` is the engine underneath it. The
report is the product here, so most of these are about what it says — and
about the way it must not misbehave: opening something it should not.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from silkcode import browser
from silkcode.tools.images import IMAGE_MARKER, review_url
from silkcode.workspace import ToolError, Workspace

BROKEN_PAGE = """<!doctype html><title>Pricing</title>
<link rel="stylesheet" href="/styles/main.css">
<body style="margin:0">
  <div style="width:1600px">a very wide table</div>
  <script>document.getElementById('by').addEventListener('click', () => {});</script>
</body>
"""

CLEAN_PAGE = """<!doctype html><title>Fine</title>
<body style="margin:0"><p>nothing wrong here</p></body>
"""


@pytest.fixture
def site(tmp_path):
    """A served directory, and its base URL."""
    root = tmp_path / "site"
    root.mkdir()
    (root / "broken.html").write_text(BROKEN_PAGE)
    (root / "clean.html").write_text(CLEAN_PAGE)
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    return Workspace(str(root))


def needs_browser():
    ok, why = browser.available()
    if not ok or browser.chromium_path() is None:
        pytest.skip(f"no browser here: {why or 'chromium not found'}")


# ---- what counts as this machine -------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/", "http://localhost:3000/x", "http://[::1]:8000/",
    "http://0.0.0.0:8377/", "127.0.0.1:8000/",
])
def test_a_page_on_this_machine_needs_no_permission(url):
    """Checking what the agent is building is the ordinary case, and putting a
    prompt in front of it would make the prompt meaningless."""
    assert browser.permission_command({"url": url}, None) == ""


@pytest.mark.parametrize("url", [
    "https://example.com/", "http://192.168.1.20:8000/", "http://10.0.0.5/",
    # the nip.io trick: a hostname that merely begins like a loopback address
    "http://127.0.0.1.evil.example/", "http://localhost.evil.example/",
])
def test_anything_that_leaves_the_machine_is_gated(url):
    assert not browser.is_local(url), f"{url} was treated as this machine"
    assert browser.permission_command({"url": url}, None), \
        f"{url} would have been fetched with no permission check"


def test_the_gate_classifies_an_outward_fetch_like_any_other():
    """Consistency with `curl`, which is what this is: MEDIUM, so `ask` and
    `edit` prompt and `agent` mode does not."""
    from silkcode.permissions import Risk, classify_command
    command = browser.permission_command({"url": "https://example.com"}, None)
    assert classify_command(command) == Risk.MEDIUM


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "data:text/html,<b>x", "ftp://example.com/x"])
def test_only_http_urls_are_opened(ws, url):
    """A browser will happily read the filesystem. This one will not: the file
    tools are the way to read files, and they are confined to the workspace.

    Checking for `://` alone was not enough — `javascript:alert(1)` contains
    none, so it was quietly turned into `http://javascript:alert(1)`.
    """
    with pytest.raises(ToolError):
        review_url(ws, url)


def test_an_empty_url_is_refused(ws):
    with pytest.raises(ToolError):
        review_url(ws, "")


def test_a_link_carrying_credentials_is_refused(ws):
    with pytest.raises(ToolError):
        review_url(ws, "http://user:secret@example.com/")


# ---- the report ------------------------------------------------------------

def test_a_broken_page_reports_what_a_browser_would_see(site, ws):
    """None of these are visible in the source: a mistyped id, a stylesheet
    that 404s, and a layout that scrolls sideways on a phone."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/broken.html", mobile=True)
    assert "-> 200" in report and "Pricing" in report
    assert "scrolls sideways" in report, report
    assert "390px wide" in report
    assert "addEventListener" in report, "the uncaught exception is missing"
    assert "main.css" in report, "the failed request is missing"


def test_a_clean_page_says_so_plainly(site, ws):
    """A tool that always finds something is a tool nobody believes."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/clean.html")
    assert "No console errors" in report, report
    assert "problems" not in report


def test_the_visible_text_is_in_the_report(site, ws):
    """What the page *says* is the other half of what a browser knows, and the
    model cannot get it from the screenshot."""
    needs_browser()
    base, _root = site
    report = review_url(ws, f"{base}/clean.html")
    assert "visible text:" in report
    assert "nothing wrong here" in report


def test_the_same_fault_is_not_reported_twice(site, ws):
    """One missing stylesheet produces both a failed request and a console
    error. Saying it twice makes a short list look like a crisis."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/broken.html")
    assert report.count("console error: Failed to load resource") <= 1, report


def test_a_missing_page_is_reported_not_raised(site, ws):
    """A 404 is a fact about the page, not a failure of the tool."""
    needs_browser()
    base, _root = site
    assert "-> 404" in review_url(ws, f"{base}/nope.html")


def test_a_bare_host_is_read_as_http(site, ws):
    needs_browser()
    base, _root = site
    hostport = base.removeprefix("http://")
    assert "-> 200" in review_url(ws, f"{hostport}/clean.html")


def test_the_screenshot_is_shown_to_the_person_and_described_to_the_model(site, ws):
    """The marker on the first line is how the GUI finds the picture; the
    report below it is what the model can actually read."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/clean.html")
    assert report.startswith(IMAGE_MARKER), report[:200]
    relative = report.splitlines()[0][len(IMAGE_MARKER):]
    assert not Path(relative).is_absolute(), "the GUI is handed a path outside the tree"
    saved = ws.resolve(relative)
    assert saved.is_file() and saved.stat().st_size > 0
    assert ".silkcode" in relative, "a screenshot was dropped into the user's tree"
    assert "for the person reading this" in report


# ---- and it explains itself when it cannot run ------------------------------

def test_no_playwright_explains_itself_instead_of_failing(monkeypatch):
    monkeypatch.setattr(browser, "available", lambda: (False, "Playwright is missing"))
    assert "Playwright is missing" in browser.check("http://127.0.0.1:1/").render()


def test_a_launch_failure_is_a_report_not_a_traceback(monkeypatch):
    """The engine has to keep working. A browser that will not start is a
    result, not a crash — `review_url` turns it into a ToolError, which the
    agent loop hands back as text rather than dying on."""
    monkeypatch.setattr(browser, "available", lambda: (True, ""))
    monkeypatch.setattr(browser, "chromium_path", lambda: "/nowhere/chromium")
    rendered = browser.check("http://127.0.0.1:1/", timeout=2.0).render()
    assert rendered.startswith("Could not load")


def test_a_missing_chromium_says_how_to_get_one(monkeypatch, ws):
    """Playwright is a dependency; its browser is a separate download, so a
    fresh install has the import and nothing to launch."""
    monkeypatch.setattr(browser, "check", lambda *a, **k: browser.PageReport(
        url="http://x/", error="Executable doesn't exist at /nowhere\n  " + browser.INSTALL_HINT))
    with pytest.raises(ToolError) as caught:
        review_url(ws, "http://127.0.0.1:1/")
    assert "playwright install chromium" in str(caught.value)


# ---- the report has to be believable ----------------------------------------

def test_a_missing_favicon_is_not_a_problem(site, ws):
    """Every page load asks for one and almost no page being built has one.
    Reporting it would put a fault on essentially every report, which is how
    a tool stops being read."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/clean.html")
    assert "favicon" not in report, report
    assert "No console errors" in report


def test_one_missing_file_is_one_problem(site, ws):
    """A bad resource arrives twice — a 404 response, then an aborted request.
    Listing it twice makes two faults look like four."""
    needs_browser()
    base, _root = site

    report = review_url(ws, f"{base}/broken.html")
    assert report.count("main.css") == 1, report
    # the overflow, the one missing stylesheet, the uncaught exception: three
    assert "problems (3)" in report, report


# ---- one tool, not two -------------------------------------------------------

def test_there_is_a_single_way_to_open_a_page():
    """Two tools that both launch Chromium at a URL is a choice the model has
    to make on every call, and it will get it wrong."""
    from silkcode.tools import TOOLS
    assert "review_url" in TOOLS
    assert "browser_check" not in TOOLS
