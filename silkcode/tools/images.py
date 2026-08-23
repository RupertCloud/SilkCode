"""Capture and present screenshots from the machine running the workspace."""

from __future__ import annotations

import platform
import subprocess
import time
from urllib.parse import urlparse
from pathlib import Path

from ..workspace import ToolError, Workspace

IMAGE_MARKER = "SILKCODE_IMAGE:"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def show_image(ws: Workspace, path: str) -> str:
    """Validate a workspace image and mark it for inline GUI presentation."""
    image = ws.resolve(path)
    if not image.is_file():
        raise ToolError(f"Image does not exist: {path}")
    if image.suffix.lower() not in IMAGE_SUFFIXES:
        raise ToolError("Only PNG, JPEG, GIF and WebP images can be shown")
    if image.stat().st_size > 20 * 1024 * 1024:
        raise ToolError("Image is larger than the 20 MB display limit")
    return f"{IMAGE_MARKER}{ws.relative(image)}"


def capture_screenshot(ws: Workspace, path: str | None = None,
                       delay: int = 1) -> str:
    """Capture the visible desktop on the local machine and present it."""
    if getattr(ws, "exec_backend", None) is not None:
        raise ToolError("Screen capture is only available for a local workspace")
    rel = path or f".silkcode/screenshots/screenshot-{int(time.time())}.png"
    target = ws.resolve(rel)
    if target.suffix.lower() != ".png":
        raise ToolError("Screenshots must use a .png path")
    target.parent.mkdir(parents=True, exist_ok=True)
    delay = min(max(int(delay), 0), 10)
    system = platform.system()
    if system == "Darwin":
        command = ["screencapture", "-x", "-T", str(delay), str(target)]
    elif system == "Linux":
        if delay:
            time.sleep(delay)
        command = ["gnome-screenshot", "-f", str(target)]
    else:
        raise ToolError(f"Screen capture is not supported on {system}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise ToolError(f"Screenshot utility is not installed: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError("Screenshot capture timed out") from exc
    if result.returncode or not target.is_file():
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(detail or "Screen capture failed; allow Screen Recording access")
    return show_image(ws, ws.relative(target))


def _web_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("Only http:// and https:// web links can be reviewed")
    if parsed.username or parsed.password:
        raise ToolError("Links containing embedded credentials cannot be reviewed")
    return url


def review_url(ws: Workspace, url: str, path: str | None = None,
               mobile: bool = False, wait_ms: int = 1000) -> str:
    """Render a link headlessly, return visible text, and present a screenshot."""
    url = _web_url(url)
    rel = path or f".silkcode/reviews/page-{int(time.time())}.png"
    target = ws.resolve(rel)
    if target.suffix.lower() != ".png":
        raise ToolError("Web review screenshots must use a .png path")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ToolError("Headless review needs Playwright: pip install playwright && playwright install chromium") from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            size = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
            page = browser.new_page(viewport=size)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(min(max(int(wait_ms), 0), 10_000))
            title = page.title()
            text = page.locator("body").inner_text(timeout=5_000)[:12_000]
            page.screenshot(path=str(target), full_page=True)
            status = response.status if response else "unknown"
            browser.close()
    except Exception as exc:
        raise ToolError(f"Could not review {url}: {exc}") from exc
    return (f"{IMAGE_MARKER}{ws.relative(target)}\n"
            f"URL: {url}\nStatus: {status}\nTitle: {title}\nVisible text:\n{text}")
