"""Knowing what you are running.

`silkcode update` pulls arbitrary commits into the installed checkout, so the
release number alone identifies nothing: everyone tracking `main` reports
"0.1.0" while running different code. These tests pin the two properties that
makes usable — the build id changes when the code does, and asking for it never
takes the process down, because a machine with no git is an ordinary machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from silkcode import version as V

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_caches():
    """The lookups are memoized for the life of a process; tests are not one."""
    for fn in (V.commit, V.is_dirty, V.build_id):
        fn.cache_clear()
    yield
    for fn in (V.commit, V.is_dirty, V.build_id):
        fn.cache_clear()


# ---- what the build id says -------------------------------------------------

def test_a_package_install_reports_the_plain_release(monkeypatch):
    """A wheel is exactly what was tagged, so the release number is the whole
    truth and a git suffix would be a lie."""
    monkeypatch.setattr(V, "_git", lambda *a: None)
    assert V.build_id() == V.RELEASE
    assert V.commit() is None
    assert V.is_dirty() is False


def test_a_checkout_reports_the_commit_it_is_sitting_on(monkeypatch):
    monkeypatch.setattr(V, "_git", lambda *a: "d4e5f6a" if a[0] == "rev-parse" else "")
    assert V.build_id() == f"{V.RELEASE}+gd4e5f6a"


def test_uncommitted_changes_are_visible_in_the_build_id(monkeypatch):
    def fake(*a):
        return "d4e5f6a" if a[0] == "rev-parse" else " M silkcode/agent/loop.py"
    monkeypatch.setattr(V, "_git", fake)
    assert V.build_id() == f"{V.RELEASE}+gd4e5f6a.dirty"


def test_the_build_id_changes_when_the_commit_does(monkeypatch):
    """The whole point: two installs on different commits must not claim to be
    the same thing."""
    seen = set()
    for sha in ("aaaaaaa", "bbbbbbb"):
        for fn in (V.commit, V.is_dirty, V.build_id):
            fn.cache_clear()
        monkeypatch.setattr(V, "_git", lambda *a, s=sha: s if a[0] == "rev-parse" else "")
        seen.add(V.build_id())
    assert len(seen) == 2, f"two commits produced one build id: {seen}"


# ---- and how it behaves when the world is unhelpful -------------------------

@pytest.mark.parametrize("boom", [
    FileNotFoundError("git"),                       # no git on the machine
    OSError("permission denied"),
    subprocess.TimeoutExpired("git", 5),            # a hung git
    subprocess.SubprocessError("something else"),
])
def test_a_broken_git_never_takes_the_process_down(monkeypatch, boom):
    def explode(*a, **k):
        raise boom
    monkeypatch.setattr(V.subprocess, "run", explode)
    assert V.build_id() == V.RELEASE
    assert V.report()          # still renders something a human can read
    assert V.info()["source"] == "package install"


def test_a_directory_that_is_not_a_repository_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_package_dir", lambda: tmp_path)
    assert V.build_id() == V.RELEASE


def test_the_report_tells_you_how_to_update_this_particular_install(monkeypatch):
    """Two installs, two different upgrade paths; guessing wrong wastes the
    user's afternoon."""
    monkeypatch.setattr(V, "_git", lambda *a: None)
    assert "pip install -U silkcode" in V.report()

    for fn in (V.commit, V.is_dirty, V.build_id):
        fn.cache_clear()
    monkeypatch.setattr(V, "_git", lambda *a: "d4e5f6a" if a[0] == "rev-parse" else "")
    assert "silkcode update" in V.report()


def test_info_is_json_serialisable_for_bug_reports():
    json.dumps(V.info())          # raises if anything in there is not plain data


# ---- one number, in one place ----------------------------------------------

def test_the_version_is_declared_exactly_once():
    """It used to be written in both pyproject.toml and __init__.py, which is a
    drift waiting to happen: the wheel is named from one and the program
    reports the other."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = cfg["project"]
    assert "version" not in project, \
        "pyproject.toml hard-codes a version again; it should read __version__"
    assert "version" in project.get("dynamic", [])
    assert cfg["tool"]["setuptools"]["dynamic"]["version"] == \
        {"attr": "silkcode.__version__"}


def test_the_declared_version_looks_like_a_version():
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+([ab]\d+|rc\d+)?", V.RELEASE), V.RELEASE


# ---- the CLI can answer the question ---------------------------------------

def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "silkcode.cli.main", *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=60)


def test_dash_dash_version_prints_the_build():
    out = _cli("--version")
    assert out.returncode == 0
    assert out.stdout.strip() == f"Silk Code {V.build_id()}"


def test_the_short_flag_works_too():
    assert _cli("-V").stdout == _cli("--version").stdout


def test_the_version_command_reports_enough_to_act_on():
    out = _cli("version")
    assert out.returncode == 0, out.stderr
    for expected in ("Silk Code", "install", "python", "platform"):
        assert expected in out.stdout, f"missing {expected!r} in:\n{out.stdout}"


def test_the_version_command_speaks_json():
    out = _cli("version", "--json")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["version"] == V.RELEASE
    assert set(data) >= {"build", "commit", "install", "python", "platform"}
