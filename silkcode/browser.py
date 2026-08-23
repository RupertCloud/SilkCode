"""Looking at a page the way a browser does.

An agent that writes a web page cannot tell whether it works. Reading the
source shows you what was written, not what happens: a mistyped id, a
stylesheet that 404s, a table that pushes the layout sideways on a phone are
all invisible in the file and obvious the moment something renders it.

This is the half `live_server` was missing. That tool serves the workspace so
a *person* can watch the page reload; this one lets the agent look.

What it returns is deliberately text. The provider layer carries strings —
there is no image path into a conversation, and the default model has no
vision anyway — so a screenshot handed to the model would be a file it cannot
read. Everything below is what a browser knows *in words*: the status, the
uncaught exceptions, the console errors, the requests that failed, and
whether the page fits the viewport. That set is what actually finds bugs;
the picture is for the human, saved beside the workspace and named in the
report.

Playwright is not a dependency of Silk Code and will not become one — the
runtime requirement is httpx and nothing else. This detects what is installed
and says how to get it when it is not, the same way the Tailscale check does.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Where a bundled Chromium tends to live. PLAYWRIGHT_BROWSERS_PATH wins when
# it is set; these are the ordinary locations otherwise.
CHROMIUM_HINTS = ("/opt/pw-browsers/chromium",)

INSTALL_HINT = (
    "Install it with:  pip install playwright && playwright install chromium\n"
    "(Playwright is optional — Silk Code's only runtime dependency is httpx.)"
)


def chromium_path() -> str | None:
    """An explicit Chromium executable to launch, or None to let Playwright
    find its own."""
    import os
    for candidate in (os.environ.get("CHROMIUM_PATH"), *CHROMIUM_HINTS):
        if candidate and Path(candidate).exists():
            return candidate
    return shutil.which("chromium") or shutil.which("chromium-browser")


def available() -> tuple[bool, str]:
    """Whether a page can be rendered here, and why not when it cannot."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False, "Playwright is not installed, so no browser is available.\n" + INSTALL_HINT
    return True, ""


def is_local(url: str) -> bool:
    """Whether this points at this machine.

    Checking a page the agent is building is the ordinary case and needs no
    ceremony. Reaching out to the internet is a different act — it leaves the
    machine, and a fetched page is content someone else wrote — so it goes
    through the permission gate like any other outward command.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Every page load asks for a favicon, and almost no page being built has one.
# Reporting that as a fault would put a problem on essentially every report,
# which is how a tool stops being believed.
IGNORED_PATHS = ("/favicon.ico",)


def _note_failure(problems: list[str], url: str, detail: str) -> None:
    from urllib.parse import urlparse as _p
    if _p(url).path in IGNORED_PATHS:
        return
    # One missing file arrives twice - a 404 response and then an aborted
    # request - and listing a resource twice makes two faults look like four.
    # The first report wins; both name the same file.
    if any(url in existing for existing in problems):
        return
    problems.append(f"{detail}: {url}")


def _note_console(problems: list[str], message) -> None:
    """Console errors worth repeating.

    Chromium logs "Failed to load resource: ... 404" for every bad request
    without saying which one - the response handler above reports the same
    thing with the URL attached, so keeping both means saying it twice, once
    uselessly.
    """
    if message.type != "error":
        return
    if "Failed to load resource" in message.text:
        return
    problems.append(f"console error: {message.text}")


@dataclass
class PageReport:
    url: str
    status: int | None = None
    title: str = ""
    width: int = 1280
    height: int = 800
    problems: list[str] = field(default_factory=list)
    screenshot: str | None = None
    error: str = ""

    def render(self) -> str:
        if self.error:
            return f"Could not load {self.url}\n  {self.error}"
        lines = [f"GET {self.url} -> {self.status}",
                 f"title: {self.title!r}",
                 f"viewport: {self.width}x{self.height}"]
        if self.problems:
            lines.append("")
            lines.append(f"problems ({len(self.problems)}):")
            lines += [f"  ! {p}" for p in self.problems]
        else:
            lines.append("")
            lines.append("No console errors, uncaught exceptions, failed requests, "
                         "or horizontal overflow.")
        if self.screenshot:
            lines.append("")
            lines.append(f"screenshot saved: {self.screenshot}")
            lines.append("(an image, so it is for the person reading this — "
                         "the report above is what describes the page)")
        return "\n".join(lines)


def check(url: str, width: int = 1280, height: int = 800,
          timeout: float = 20.0, screenshot_to: Path | None = None) -> PageReport:
    """Load `url` in a headless browser and report what a browser would see."""
    report = PageReport(url=url, width=width, height=height)
    ok, why = available()
    if not ok:
        report.error = why
        return report

    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    try:
        with sync_playwright() as pw:
            launch: dict = {}
            executable = chromium_path()
            if executable:
                launch["executable_path"] = executable
            browser = pw.chromium.launch(**launch)
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.on("pageerror", lambda e: problems.append(f"uncaught {e}"))
                page.on("console", lambda m: _note_console(problems, m))
                page.on("requestfailed", lambda r: _note_failure(
                    problems, r.url, f"request failed ({r.failure})"))
                page.on("response", lambda r: _note_failure(
                    problems, r.url, f"{r.status}") if r.status >= 400 else None)

                response = page.goto(url, wait_until="networkidle",
                                     timeout=timeout * 1000)
                report.status = response.status if response else None
                report.title = page.title()
                # A page wider than its viewport is the single most common way
                # a layout is wrong on a phone, and it is invisible in the
                # source: it is whichever child refused to shrink.
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth")
                if isinstance(overflow, (int, float)) and overflow > 0:
                    problems.insert(0, f"page scrolls sideways by {int(overflow)}px "
                                       f"at {width}px wide")
                if screenshot_to is not None:
                    screenshot_to.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot_to), full_page=True)
                    report.screenshot = str(screenshot_to)
            finally:
                browser.close()
    except Exception as exc:                      # a launch failure, a timeout
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        if "executable doesn't exist" in message.lower() or "looks like" in message.lower():
            message += "\n  " + INSTALL_HINT
        report.error = message
        return report

    # Deduplicate: one missing stylesheet produces both a failed request and a
    # console error, and saying it twice makes a short list look like a crisis.
    seen: set[str] = set()
    for problem in problems:
        key = problem.split("(")[0].strip()
        if key not in seen:
            seen.add(key)
            report.problems.append(problem)
    return report


def browser_check(ws, url: str, width: int = 1280, height: int = 800,
                  screenshot: bool = True) -> str:
    """The agent-facing tool: render `url` and report what a browser sees."""
    from .statedir import state_dir
    from .workspace import ToolError

    import re

    url = (url or "").strip()
    if not url:
        raise ToolError("browser_check needs a url")
    # A scheme, if there is one, must be http or https. Checking for "://"
    # alone was not enough: `javascript:alert(1)` and `data:text/html,...`
    # contain no "://", so they were quietly turned into
    # `http://javascript:alert(1)` and allowed through. A browser will also
    # read the filesystem given file://; reading files is what the file tools
    # are for, and they are confined to the workspace.
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):(.*)$", url)
    if scheme and not scheme.group(2).split("/")[0].isdigit():
        # a real scheme rather than `host:port`
        if scheme.group(1).lower() not in ("http", "https"):
            raise ToolError(
                f"browser_check only opens http:// and https:// URLs, not "
                f"{scheme.group(1)}:")
    elif "://" not in url:
        url = "http://" + url

    target = None
    if screenshot:
        try:
            import re
            import time
            stem = re.sub(r"[^A-Za-z0-9]+", "-", urlparse(url).netloc + urlparse(url).path)
            stem = (stem.strip("-") or "page")[:60]
            target = state_dir(ws.root) / "screenshots" / f"{stem}-{int(time.time())}.png"
        except Exception:
            target = None      # a read-only workspace still gets the report

    return check(url, width=width, height=height, screenshot_to=target).render()


def permission_command(args: dict, ws) -> str:
    """What the permission gate should judge this call as.

    An empty string means no prompt. Checking a page on this machine — which
    is the whole point next to `live_server` — is not an outward act and does
    not get treated as one. Anything else leaves the machine and is classified
    like any other command that does.
    """
    url = str(args.get("url") or "")
    return "" if is_local(url) else f"browser_check {url}"
