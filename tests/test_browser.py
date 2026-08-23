"""Letting the agent look at the page it just wrote.

`live_server` serves the workspace so a person can watch it reload. This is
the other half: what a browser knows, in words, because the provider layer
carries strings and a screenshot handed to a model is a file it cannot read.

The report is the product here, so most of these are about what it says — and
about the two ways it must not misbehave: opening something it should not, and
becoming a dependency.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from silkcode import browser
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


def needs_browser():
    ok, why = browser.available()
    if not ok or browser.chromium_path() is None:
        pytest.skip(f"no browser here: {why or 'chromium not found'}")


# ---- what counts as this machine -------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/", "http://localhost:3000/x", "http://[::1]:8000/",
    "http://0.0.0.0:8377/",
])
def test_a_page_on_this_machine_needs_no_permission(url):
    """Checking what the agent is building is the ordinary case, and putting a
    prompt in front of it would make the prompt meaningless."""
    assert browser.is_local(url)
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


def test_the_gate_classifies_an_outward_fetch_like_any_other(tmp_path):
    """Consistency with `curl`, which is what this is: MEDIUM, so `ask` and
    `edit` prompt and `agent` mode does not."""
    from silkcode.permissions import Risk, classify_command
    command = browser.permission_command({"url": "https://example.com"}, None)
    assert classify_command(command) == Risk.MEDIUM


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "data:text/html,<b>x", "ftp://example.com/x"])
def test_only_http_urls_are_opened(tmp_path, url):
    """A browser will happily read the filesystem. This one will not: the file
    tools are the way to read files, and they are confined to the workspace."""
    ws = Workspace(str(tmp_path))
    with pytest.raises(ToolError):
        browser.browser_check(ws, url)


def test_an_empty_url_is_refused(tmp_path):
    with pytest.raises(ToolError):
        browser.browser_check(Workspace(str(tmp_path)), "")


# ---- the report ------------------------------------------------------------

def test_a_broken_page_reports_what_a_browser_would_see(site, tmp_path):
    """None of these are visible in the source: a mistyped id, a stylesheet
    that 404s, and a layout that scrolls sideways on a phone."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/broken.html", width=390, height=844)
    assert "-> 200" in report and "Pricing" in report
    assert "scrolls sideways" in report, report
    assert "390px wide" in report
    assert "addEventListener" in report, "the uncaught exception is missing"
    assert "main.css" in report, "the failed request is missing"


def test_a_clean_page_says_so_plainly(site, tmp_path):
    """A tool that always finds something is a tool nobody believes."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/clean.html", screenshot=False)
    assert "No console errors" in report, report
    assert "problems" not in report


def test_the_same_fault_is_not_reported_twice(site, tmp_path):
    """One missing stylesheet produces both a failed request and a console
    error. Saying it twice makes a short list look like a crisis."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/broken.html", screenshot=False)
    assert report.count("console error: Failed to load resource") <= 1, report


def test_a_missing_page_is_reported_not_raised(site, tmp_path):
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))
    assert "-> 404" in browser.browser_check(ws, f"{base}/nope.html", screenshot=False)


def test_a_bare_host_is_read_as_http(site, tmp_path):
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))
    hostport = base.removeprefix("http://")
    assert "-> 200" in browser.browser_check(ws, f"{hostport}/clean.html",
                                             screenshot=False)


def test_the_screenshot_lands_in_the_workspace_state_dir(site, tmp_path):
    """It is for the person reading, and the report says as much rather than
    implying the model looked at it."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/clean.html")
    line = [l for l in report.splitlines() if l.startswith("screenshot saved:")]
    assert line, report
    path = Path(line[0].split(": ", 1)[1])
    assert path.is_file() and path.stat().st_size > 0
    assert ".silkcode" in str(path), "a screenshot was dropped into the user's tree"
    assert "for the person reading this" in report


# ---- and it is not a dependency --------------------------------------------

def test_no_playwright_explains_itself_instead_of_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "available",
                        lambda: (False, "Playwright is not installed, so no "
                                        "browser is available.\n" + browser.INSTALL_HINT))
    report = browser.check("http://127.0.0.1:1/")
    assert "not installed" in report.render()
    assert "pip install playwright" in report.render()
    assert "only runtime dependency is httpx" in report.render()


def test_a_launch_failure_is_a_report_not_a_traceback(monkeypatch):
    """The agent has to keep working. A browser that will not start is a
    result, not a crash."""
    monkeypatch.setattr(browser, "available", lambda: (True, ""))
    monkeypatch.setattr(browser, "chromium_path", lambda: "/nowhere/chromium")
    rendered = browser.check("http://127.0.0.1:1/", timeout=2.0).render()
    assert rendered.startswith("Could not load")


def test_silk_code_still_declares_one_runtime_dependency():
    """Playwright is optional and must stay that way."""
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "playwright" not in dependencies
    assert "httpx" in dependencies


# ---- the report has to be believable ----------------------------------------

def test_a_missing_favicon_is_not_a_problem(site, tmp_path):
    """Every page load asks for one and almost no page being built has one.
    Reporting it would put a fault on essentially every report, which is how
    a tool stops being read."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/clean.html", screenshot=False)
    assert "favicon" not in report, report
    assert "No console errors" in report


def test_one_missing_file_is_one_problem(site, tmp_path):
    """A bad resource arrives twice — a 404 response, then an aborted request.
    Listing it twice makes two faults look like four."""
    needs_browser()
    base, _root = site
    (tmp_path / "ws").mkdir(exist_ok=True)
    ws = Workspace(str(tmp_path / "ws"))

    report = browser.browser_check(ws, f"{base}/broken.html", screenshot=False)
    assert report.count("main.css") == 1, report
    # the overflow, the one missing stylesheet, the uncaught exception: three
    assert "problems (3)" in report, report
