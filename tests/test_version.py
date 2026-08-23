"""Knowing what you are running.

`silkcode update` pulls arbitrary commits into the installed checkout, so the
release number alone identifies nothing: everyone tracking `main` reports
"0.1.0" while running different code. These tests pin the two properties that
makes usable — the build id changes when the code does, and asking for it never
takes the process down, because a machine with no git is an ordinary machine.

Note for anyone extending this file: the floor is Python 3.10, so `tomllib` is
not available. An import of it here does not fail one test, it aborts
collection of the whole suite on the 3.10 job.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
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


def test_a_git_url_install_is_identified_by_the_commit_pip_resolved(monkeypatch):
    """`pip install git+https://...` leaves no git metadata on disk, so this
    reported a bare "0.1.0" — the one thing a bug report from the most common
    install must not say. pip knew the commit all along: it writes what it
    resolved into its PEP 610 record at install time."""
    import silkcode.update as U
    monkeypatch.setattr(V, "_git", lambda *a: None)
    monkeypatch.setattr(U, "install_origin", lambda: {
        "kind": "vcs", "spec": "git+https://example.invalid/x",
        "url": "https://example.invalid/x",
        "commit_id": "712928ed2420f5f0a1b2c3d4e5f6a7b8c9d0e1f2"})
    assert V.commit() == "712928e"
    assert V.build_id() == f"{V.RELEASE}+g712928e"
    assert V.is_dirty() is False, "a pip install has no working tree to be dirty"


def test_a_release_wheel_still_reports_the_bare_release(monkeypatch):
    """An archive install records no commit, and inventing one would be worse
    than saying nothing."""
    import silkcode.update as U
    monkeypatch.setattr(V, "_git", lambda *a: None)
    monkeypatch.setattr(U, "install_origin", lambda: {
        "kind": "archive", "spec": "https://example.invalid/silkcode.whl",
        "url": "https://example.invalid/silkcode.whl"})
    assert V.build_id() == V.RELEASE


def test_a_checkout_still_prefers_its_own_git(monkeypatch):
    """git is the live answer; the PEP 610 record is what pip resolved at
    install time and goes stale the moment the checkout moves."""
    import silkcode.update as U
    monkeypatch.setattr(V, "_git", lambda *a: "aaaaaaa" if a[0] == "rev-parse" else "")
    monkeypatch.setattr(U, "install_origin", lambda: {"kind": "vcs", "spec": "x",
                                                      "url": "x", "commit_id": "bbbbbbb"})
    assert V.commit() == "aaaaaaa"


def test_unreadable_install_metadata_cannot_break_identifying_yourself(monkeypatch):
    import silkcode.update as U
    def explode():
        raise RuntimeError("metadata is unreadable")
    monkeypatch.setattr(V, "_git", lambda *a: None)
    monkeypatch.setattr(U, "install_origin", explode)
    assert V.build_id() == V.RELEASE
    assert V.commit() is None


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


def test_a_checkout_is_told_to_run_silkcode_update(monkeypatch):
    """A git checkout is fast-forwarded by `silkcode update`, so say that.

    The anti-assertion is the one that matters: this line used to read
    `pip install -U silkcode`, which is not a package on PyPI, so the one
    person actively diagnosing an update problem got a 404.
    """
    monkeypatch.setattr(V, "_git", lambda *a: "d4e5f6a" if a[0] == "rev-parse" else "")
    out = V.report()
    assert "silkcode update" in out
    assert "pip install -U silkcode" not in out


@pytest.mark.parametrize("origin, expected", [
    # pip recorded a git URL: `silkcode update` can reinstall from it.
    ({"kind": "vcs", "spec": "git+https://example.invalid/x",
      "url": "https://example.invalid/x"}, "silkcode update"),
    # ...an archive, which the updater refuses. Recommending `silkcode update`
    # here sends that same person to a command that always errors, so the
    # report has to name the pip line that actually reinstalls it.
    ({"kind": "archive", "spec": "https://example.invalid/silkcode.whl",
      "url": "https://example.invalid/silkcode.whl"},
     "pip install --upgrade --force-reinstall https://example.invalid/silkcode.whl"),
    # ...nothing at all: fall back to the same instruction `silkcode update`
    # itself prints when it gives up, rather than inventing a second one.
    (None, "pip install --upgrade --force-reinstall "
           "git+https://github.com/RupertCloud/SilkCode"),
])
def test_an_install_with_no_git_is_told_what_actually_updates_it(
        monkeypatch, origin, expected):
    import silkcode.update as U
    monkeypatch.setattr(V, "_git", lambda *a: None)
    monkeypatch.setattr(U, "install_origin", lambda: origin)
    out = V.report()
    assert expected in out
    assert "pip install -U silkcode" not in out
    assert "no git metadata" in out         # still says which install it is


def test_a_broken_origin_lookup_still_prints_a_usable_line(monkeypatch):
    """Reading pip's metadata is not allowed to break `silkcode version`."""
    import silkcode.update as U
    def explode():
        raise RuntimeError("metadata is unreadable")
    monkeypatch.setattr(V, "_git", lambda *a: None)
    monkeypatch.setattr(U, "install_origin", explode)
    assert "pip install --upgrade --force-reinstall" in V.report()


def test_info_is_json_serialisable_for_bug_reports():
    json.dumps(V.info())          # raises if anything in there is not plain data


# ---- one number, in one place ----------------------------------------------

def _pyproject_tables() -> dict[str, list[str]]:
    """`{table name: its lines}` for pyproject.toml.

    Deliberately not tomllib: this project supports Python 3.10, where that
    module does not exist. Reaching for it aborted collection of the entire
    suite on 3.10 rather than failing one test — and the assertions below are
    about a handful of literal lines, which needs no TOML parser.
    """
    tables: dict[str, list[str]] = {}
    current = ""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            tables.setdefault(current, [])
        elif stripped and not stripped.startswith("#"):
            tables.setdefault(current, []).append(stripped)
    return tables


def test_the_version_is_declared_exactly_once():
    """It used to be written in both pyproject.toml and __init__.py, which is a
    drift waiting to happen: the wheel is named from one and the program
    reports the other."""
    tables = _pyproject_tables()
    project = tables["project"]
    assert not any(re.match(r'version\s*=\s*["\']', line) for line in project), \
        "pyproject.toml hard-codes a version again; it should read __version__"
    assert any(re.match(r"dynamic\s*=.*\bversion\b", line) for line in project), \
        "[project] no longer declares a dynamic version"
    dynamic = tables.get("tool.setuptools.dynamic", [])
    assert any("silkcode.__version__" in line for line in dynamic), \
        "the dynamic version no longer reads silkcode.__version__"


def test_the_build_backend_agrees_with_us_about_the_version():
    """The check above reads the file; this one asks setuptools what it will
    actually stamp on the wheel, which is the number that ends up in the
    filename the release notes link to."""
    setuptools = pytest.importorskip("setuptools")   # a build dep, not a runtime one
    del setuptools
    out = subprocess.run(
        [sys.executable, "-c",
         "from setuptools.config.pyprojecttoml import read_configuration as r;"
         "print(r('pyproject.toml')['project']['version'])"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        pytest.skip(f"setuptools cannot read the config here: {out.stderr[-200:]}")
    assert out.stdout.strip() == V.RELEASE


def test_the_declared_version_looks_like_a_version():
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
