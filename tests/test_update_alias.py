"""`silkcode -update`, and updating an install that is not a git checkout.

Two gaps this covers. `silkcode -update` used to die on `unrecognized
arguments: -update` with no hint that `silkcode update` was the same thing.
And an install made the way the README leads with — `pip install
git+https://github.com/RupertCloud/SilkCode` — carries no git metadata, so
update refused and pointed at `pip install -U silkcode`, a package that does
not exist on PyPI.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from silkcode.cli.main import REPL_FLAGS, _repl_parser, main, subcommand_alias
from silkcode.update import install_origin, update_installation, update_pip_install

COMMANDS = {"update": 1, "sandbox": 1, "version": 1, "models": 1, "inference": 1,
            "gui": 1, "sync": 1}


def run(argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = main(argv)
        except SystemExit as exc:            # argparse --help / --version
            code = exc.code or 0
    return code, out.getvalue()


# ---- the flag form of a verb -------------------------------------------------

@pytest.mark.parametrize("token", ["-update", "--update"])
def test_the_flag_form_of_update_reaches_the_update_command(token):
    assert subcommand_alias(token, COMMANDS) == "update"


@pytest.mark.parametrize("token", ["-gui", "--models", "-sync", "--inference"])
def test_every_verb_accepts_the_flag_form(token):
    assert subcommand_alias(token, COMMANDS) is not None


def test_update_help_is_reachable_through_the_flag_form():
    code, out = run(["-update", "--help"])
    assert code == 0
    assert "silkcode update" in out
    assert "--branch" in out and "--install" in out


def test_a_flag_that_names_no_verb_is_left_to_the_repl():
    assert subcommand_alias("--nonsense", COMMANDS) is None
    assert subcommand_alias("---update", COMMANDS) is None
    assert subcommand_alias("update", COMMANDS) is None   # the bare verb, not our job


# ---- the flags the REPL owns keep their meaning ------------------------------

def test_sandbox_stays_a_repl_flag_not_the_sandbox_command():
    """`silkcode --sandbox <path>` runs the REPL against the configured
    sandbox. Reading it as the `sandbox` management command would silently
    change what a documented flag does."""
    assert subcommand_alias("--sandbox", COMMANDS) is None


def test_dash_dash_version_keeps_its_short_output():
    """`--version` is one line; `silkcode version` is the full report. The
    alias must not collapse the two."""
    assert subcommand_alias("--version", COMMANDS) is None
    code, out = run(["--version"])
    assert code == 0
    assert out.startswith("Silk Code")
    assert len(out.strip().splitlines()) == 1


def test_the_reserved_flag_list_matches_the_parser():
    """REPL_FLAGS is hardcoded so startup does not pay for building the parser
    (it shells out to git for the build id). This is the guard against it
    drifting away from the parser it is meant to mirror."""
    declared = {opt for action in _repl_parser("silkcode")._actions
                for opt in action.option_strings}
    missing = declared - REPL_FLAGS
    assert not missing, f"REPL flags absent from REPL_FLAGS: {sorted(missing)}"


# ---- where pip got this copy -------------------------------------------------

class FakeDistribution:
    def __init__(self, payload):
        self.payload = payload

    def read_text(self, name):
        return self.payload if name == "direct_url.json" else None


@pytest.fixture
def pip_record(monkeypatch):
    def install(payload):
        import importlib.metadata as md
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        monkeypatch.setattr(md, "distribution", lambda name: FakeDistribution(text))
    return install


def test_a_git_install_is_recognised_with_the_url_pip_used(pip_record):
    pip_record({"url": "https://github.com/RupertCloud/SilkCode",
                "vcs_info": {"vcs": "git", "requested_revision": "main"}})
    origin = install_origin()
    assert origin["kind"] == "vcs"
    assert origin["spec"] == "git+https://github.com/RupertCloud/SilkCode@main"


def test_a_git_install_without_a_named_revision_keeps_the_bare_url(pip_record):
    pip_record({"url": "https://github.com/RupertCloud/SilkCode",
                "vcs_info": {"vcs": "git"}})
    assert install_origin()["spec"] == "git+https://github.com/RupertCloud/SilkCode"


def test_an_editable_install_is_reported_as_such(pip_record):
    pip_record({"url": "file:///home/me/SilkCode", "dir_info": {"editable": True}})
    assert install_origin()["kind"] == "editable"


def test_a_missing_or_broken_record_is_not_an_error(pip_record):
    pip_record("not json at all")
    assert install_origin() is None
    pip_record({})
    assert install_origin() is None


# ---- updating without a checkout ---------------------------------------------

def test_a_pip_installed_copy_reinstalls_from_where_pip_got_it(monkeypatch, tmp_path):
    """The README's primary install is `pip install git+https://...`, which is
    not a checkout. It must still update, not refuse."""
    import silkcode.update as up
    monkeypatch.setattr(up, "git_repo_root", lambda start=None: None)
    monkeypatch.setattr(up, "install_origin", lambda: {
        "kind": "vcs", "spec": "git+https://github.com/RupertCloud/SilkCode",
        "url": "https://github.com/RupertCloud/SilkCode"})
    calls = []
    monkeypatch.setattr(up, "update_pip_install",
                        lambda spec, on_progress=None: calls.append(spec) or
                        {"status": "updated", "detail": "aaaa -> bbbb"})
    result = update_installation()
    assert result["status"] == "updated"
    assert calls == ["git+https://github.com/RupertCloud/SilkCode"]


def test_with_no_checkout_and_no_record_the_advice_is_one_that_works(monkeypatch):
    """It used to say `pip install -U silkcode`. That package is not on PyPI,
    so the one instruction the user was given returned a 404."""
    import silkcode.update as up
    monkeypatch.setattr(up, "git_repo_root", lambda start=None: None)
    monkeypatch.setattr(up, "install_origin", lambda: None)
    detail = update_installation()["detail"]
    assert "pip install -U silkcode" not in detail
    assert "git+https://github.com/RupertCloud/SilkCode" in detail


def test_a_failed_reinstall_reports_pips_own_last_line(monkeypatch):
    import silkcode.update as up
    monkeypatch.setattr(up, "_installed_commit", lambda: "abc123")

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Could not find a version that satisfies the requirement\n"
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: Proc())
    result = update_pip_install("git+https://example.invalid/x")
    assert result["status"] == "error"
    assert "Could not find a version" in result["detail"]


def test_a_reinstall_that_changes_nothing_reports_up_to_date(monkeypatch):
    import silkcode.update as up
    monkeypatch.setattr(up, "_installed_commit", lambda: "abc123def456")

    class Proc:
        returncode = 0
        stdout = "Successfully installed silkcode-0.1.0\n"
        stderr = ""
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: Proc())
    assert update_pip_install("git+https://example/x")["status"] == "up-to-date"


def test_the_commit_pip_recorded_is_kept(pip_record):
    """For a `pip install git+...` there is no git metadata anywhere on disk.
    The commit in pip's record is the only thing that changes when a reinstall
    picks up new upstream code, so it is what makes "did anything move?"
    answerable for that install at all."""
    pip_record({"url": "https://github.com/RupertCloud/SilkCode",
                "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})
    assert install_origin()["commit_id"] == "a" * 40


def test_the_probe_prefers_pips_recorded_commit_over_git(tmp_path):
    """Run the real probe against a fake distribution: a checkout would answer
    from git, but a pip record must win, or a pip-installed copy sitting inside
    someone's clone would report the clone's commit instead of its own."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import silkcode
    from silkcode.update import _COMMIT_PROBE

    # sitecustomize is imported before the -c body, so the fake record is in
    # place by the time the probe asks for it.
    (tmp_path / "sitecustomize.py").write_text(
        "import json, importlib.metadata as md\n"
        "class D:\n"
        "    def read_text(self, name):\n"
        "        return json.dumps({'url': 'https://x/y',\n"
        "                           'vcs_info': {'vcs': 'git', 'commit_id': 'beef' * 10}})\n"
        "md.distribution = lambda name: D()\n"
    )
    package_parent = Path(silkcode.__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(package_parent)]))
    proc = subprocess.run([sys.executable, "-c", _COMMIT_PROBE],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "beef" * 10


def test_a_reinstall_that_cannot_name_a_commit_does_not_claim_an_upgrade(monkeypatch):
    """pip succeeded but nothing on disk records a commit. Saying "updated
    aaaa -> bbbb" would be inventing a change we cannot actually see."""
    import silkcode.update as up
    monkeypatch.setattr(up, "_installed_commit", lambda: None)

    class Proc:
        returncode = 0
        stdout = "Successfully installed silkcode-0.1.0\n"
        stderr = ""
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: Proc())
    result = update_pip_install("git+https://example/x")
    assert result["status"] == "updated"
    assert "nothing to compare" in result["detail"]
    assert "->" not in result["detail"]


def test_the_cli_reaches_the_reinstall_path_when_there_is_no_checkout(monkeypatch):
    """cmd_update used to bail on `repo is None` before update_installation was
    ever called, so the reinstall path was unreachable from the command people
    actually run."""
    import silkcode.update as up
    monkeypatch.setattr(up, "git_repo_root", lambda start=None: None)
    seen = {}

    def fake_update(repo=None, branch=None, force=False, on_progress=lambda s: None):
        seen["repo"] = repo
        return {"status": "updated", "detail": "reinstalled from git+https://x"}
    monkeypatch.setattr(up, "update_installation", fake_update)

    code, out = run(["-update"])
    assert code == 0, out
    assert "repo" in seen, "update_installation was never called"
    assert seen["repo"] is None
    assert "reinstalled from" in out
    assert "pip install -U silkcode" not in out
