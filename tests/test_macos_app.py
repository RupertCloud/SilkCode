"""The macOS app's launcher (packaging/macos/launcher.sh).

The bundle around it is built by packaging/macos/build-dmg.sh and can only be
opened on a Mac; what can be checked anywhere is what the launcher decides to
do: start the daemon from the bundled Python when nothing is listening on the
port, bring the GUI back when something already is, and say so when the
daemon dies instead of silently doing nothing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "packaging" / "macos" / "launcher.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the launcher is a bash script")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bundle:
    """A stand-in "Silk Code.app": the real launcher over a fake python that
    records how it was called, plus fake `open`/`osascript` on PATH."""

    def __init__(self, tmp_path: Path, port: int, gui_behaviour: str) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.calls = tmp_path / "calls"
        self.calls.mkdir()
        self.port = port
        contents = tmp_path / "Silk Code.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        self.launcher = contents / "MacOS" / "SilkCode"
        self.launcher.write_bytes(LAUNCHER.read_bytes())
        self.launcher.chmod(0o755)
        pybin = contents / "Resources" / "python" / "bin"
        pybin.mkdir(parents=True)
        self._script(pybin / "python3",
                     f'echo "$*" >> "{self.calls}/python"\n'
                     f'case "$*" in *"silkcode gui"*) {gui_behaviour} ;; esac\n')
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self._script(self.bin / "open", f'echo "$*" >> "{self.calls}/open"\n')
        self._script(self.bin / "osascript", f'echo "$*" >> "{self.calls}/osascript"\n')

    @staticmethod
    def _script(path: Path, body: str) -> None:
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def run(self, **env: str) -> subprocess.CompletedProcess:
        base = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                "SILKCODE_PORT": str(self.port), "SILKCODE_APP_SHELL": "1"}
        return subprocess.run([str(self.launcher)], env={**base, **env},
                              capture_output=True, text=True, timeout=60)

    def called(self, name: str) -> list[str]:
        path = self.calls / name
        return path.read_text().splitlines() if path.exists() else []


def listen_once(port: int) -> str:
    """A fake daemon: listen on the port until the launcher's probe connects."""
    return (f'exec {sys.executable} -c "import socket; s = socket.socket(); '
            f's.bind((\'127.0.0.1\', {port})); s.listen(1); s.accept()"')


def test_opening_the_app_starts_the_daemon_from_the_bundled_python(tmp_path):
    port = free_port()
    app = Bundle(tmp_path, port, listen_once(port))

    result = app.run()

    assert result.returncode == 0, result.stderr
    assert set(app.called("python")) == {f"-m silkcode gui --port {port}",
                                         "-m playwright install chromium"}
    assert app.called("open") == []          # the daemon opens the browser itself
    assert app.called("osascript") == []
    assert (app.home / "Library/Logs/SilkCode/gui.log").is_file()


def test_opening_the_app_again_just_brings_the_gui_back(tmp_path):
    port = free_port()
    app = Bundle(tmp_path, port, "exit 0")
    with socket.socket() as running:
        running.bind(("127.0.0.1", port))
        running.listen(1)

        result = app.run()

    assert result.returncode == 0, result.stderr
    assert app.called("open") == [f"http://127.0.0.1:{port}"]
    assert app.called("python") == []


def test_a_daemon_that_dies_is_reported_not_swallowed(tmp_path):
    app = Bundle(tmp_path, free_port(), "exit 1")

    result = app.run()

    assert result.returncode == 1
    dialog = " ".join(app.called("osascript"))
    assert "Silk Code could not start" in dialog
    assert str(app.home / "Library/Logs/SilkCode/gui.log") in dialog


def test_the_daemon_is_started_through_the_login_shell(tmp_path):
    """Finder hands apps a bare environment; the launcher re-enters through
    the user's login shell so PATH and exported API keys apply."""
    port = free_port()
    app = Bundle(tmp_path, port, listen_once(port))
    shell = tmp_path / "zsh"
    shell.write_text(f'#!/bin/bash\necho "$*" >> "{app.calls}/shell"\nexec /bin/bash "$@"\n')
    shell.chmod(0o755)

    result = app.run(SHELL=str(shell), SILKCODE_APP_SHELL="")

    assert result.returncode == 0, result.stderr
    assert app.called("shell") == [f'-l -i -c exec "$0" "$@" {app.launcher}']
    assert f"-m silkcode gui --port {port}" in app.called("python")
