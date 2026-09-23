#!/usr/bin/env python3
"""Cross-platform Silk Code installer: application, Chromium, and launcher."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

DEFAULT_SOURCE = "git+https://github.com/RupertCloud/SilkCode"


def runtime_dir() -> Path:
    override = os.environ.get("SILKCODE_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "SilkCode" / "runtime"
    return Path.home() / ".silkcode" / "runtime"


def paths(root: Path) -> tuple[Path, Path, Path]:
    if os.name == "nt":
        python = root / "Scripts" / "python.exe"
        command = root / "Scripts" / "silkcode.exe"
        launcher = root.parent / "bin" / "silkcode.cmd"
    else:
        python = root / "bin" / "python"
        command = root / "bin" / "silkcode"
        launcher = Path.home() / ".local" / "bin" / "silkcode"
    return python, command, launcher


def run(command: list[str]) -> None:
    print("+", " ".join(map(str, command)))
    subprocess.run(command, check=True)


def write_launcher(command: Path, launcher: Path) -> None:
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher.write_text(f'@echo off\r\n"{command}" %*\r\n', encoding="utf-8")
    else:
        if launcher.exists() or launcher.is_symlink():
            launcher.unlink()
        launcher.symlink_to(command)


def install(source: str, root: Path | None = None) -> Path:
    root = (root or runtime_dir()).expanduser().resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    if not (root / "pyvenv.cfg").exists():
        print(f"Creating isolated environment at {root}")
        venv.EnvBuilder(with_pip=True).create(root)
    python, command, launcher = paths(root)
    run([str(python), "-m", "pip", "install", "--upgrade", source])
    run([str(python), "-m", "playwright", "install", "chromium"])
    # graphify (PyPI: graphifyy) powers the graph tools and the GUI's Graph
    # panel - local tree-sitter parsing, no model call. Installed here rather
    # than as a wheel dependency: fifteen grammar wheels is a lot to charge a
    # bare `pip install silkcode` for, and everything degrades gracefully
    # without it.
    run([str(python), "-m", "pip", "install", "--upgrade", "graphifyy"])
    if not command.exists():
        raise RuntimeError(f"Silk Code launcher was not created at {command}")
    write_launcher(command, launcher)
    return launcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Silk Code and its Chromium browser")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="pip source to install (use '.' from a cloned repository)")
    parser.add_argument("--install-dir", type=Path,
                        help="isolated environment location (advanced)")
    args = parser.parse_args(argv)
    try:
        launcher = install(args.source, args.install_dir)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Silk Code installation failed: {exc}", file=sys.stderr)
        return 1
    print("\nSilk Code and Chromium are installed.")
    print(f"Launcher: {launcher}")
    if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {launcher.parent} to PATH, or run the launcher by its full path.")
    print("Run: silkcode gui")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
