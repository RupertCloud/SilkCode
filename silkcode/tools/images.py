"""Capture and present screenshots from the machine running the workspace."""

from __future__ import annotations

import platform
import subprocess
import time
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
