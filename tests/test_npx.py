"""The npx launcher: `npx silkcode` on a machine that only has Node.

The npm package is a doorway, not a second implementation: one dependency-free
JS file that bootstraps the bundled install.py on first run and execs the real
CLI ever after. These tests drive the actual script under node with a fake
runtime and a fake Python, so no network and no real install is involved;
they skip cleanly where node is not installed (CI's runners have it).
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

NPM_DIR = Path(__file__).resolve().parents[1] / "npm"
SCRIPT = NPM_DIR / "bin" / "silkcode.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed here")


def test_the_package_is_a_doorway_not_a_dependency_tree():
    data = json.loads((NPM_DIR / "package.json").read_text())
    assert data["name"] == "silkcode"
    assert data["bin"] == {"silkcode": "bin/silkcode.js"}
    assert "dependencies" not in data, "the launcher must stay supply-chain-free"
    assert "install.py" in data["files"], "the bundled installer ships with the package"
    assert "prepack" in data["scripts"], "prepack copies the canonical install.py in"
    assert (NPM_DIR / "bin" / "silkcode.js").is_file()


def run_launcher(args, env_extra, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SILKCODE_INSTALL_DIR", "SILKCODE_PYTHON")}
    env.update(env_extra)
    return subprocess.run(["node", str(SCRIPT), *args], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def write_executable(path, script):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@needs_node
def test_the_script_parses():
    subprocess.run(["node", "--check", str(SCRIPT)], check=True)


@needs_node
def test_an_installed_runtime_is_execed_directly_with_the_arguments(tmp_path):
    runtime = tmp_path / "runtime"
    write_executable(runtime / "bin" / "silkcode", 'echo "launcher got: $@"\n')
    result = run_launcher(["gui", "--port", "9"],
                          {"SILKCODE_INSTALL_DIR": str(runtime)})
    assert result.returncode == 0
    assert "launcher got: gui --port 9" in result.stdout
    assert "installing" not in result.stderr, "no bootstrap when already installed"


@needs_node
def test_the_exit_code_passes_through(tmp_path):
    runtime = tmp_path / "runtime"
    write_executable(runtime / "bin" / "silkcode", "exit 7\n")
    assert run_launcher([], {"SILKCODE_INSTALL_DIR": str(runtime)}).returncode == 7


@needs_node
def test_first_run_bootstraps_with_install_py_then_execs(tmp_path):
    runtime = tmp_path / "runtime"
    log = tmp_path / "python.log"
    # a fake Python that records what it was asked to run and "installs"
    fake_python = write_executable(tmp_path / "python", (
        f'echo "$@" >> {log}\n'
        f'mkdir -p {runtime}/bin\n'
        f'printf \'#!/bin/sh\\necho ran after install\\n\' > {runtime}/bin/silkcode\n'
        f'chmod +x {runtime}/bin/silkcode\n'
    ))
    result = run_launcher(["gui"], {"SILKCODE_INSTALL_DIR": str(runtime),
                                    "SILKCODE_PYTHON": str(fake_python)})
    assert result.returncode == 0
    assert log.read_text().strip().endswith("install.py"), \
        "the bootstrap must run install.py, not reimplement it"
    assert "First run: installing" in result.stderr
    assert "ran after install" in result.stdout


@needs_node
def test_no_python_is_a_helpful_error_not_a_stack_trace(tmp_path):
    runtime = tmp_path / "runtime"
    # a PATH with node but no python at all
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "node").symlink_to(shutil.which("node"))
    result = run_launcher(["gui"], {"SILKCODE_INSTALL_DIR": str(runtime),
                                    "PATH": str(bindir)})
    assert result.returncode == 1
    assert "Python 3.10" in result.stderr
    assert "python.org" in result.stderr
    assert "Error" not in result.stderr  # a message, not a traceback


@needs_node
def test_a_failed_install_is_reported_and_nothing_is_execed(tmp_path):
    runtime = tmp_path / "runtime"
    fake_python = write_executable(tmp_path / "python", "exit 3\n")
    result = run_launcher(["gui"], {"SILKCODE_INSTALL_DIR": str(runtime),
                                    "SILKCODE_PYTHON": str(fake_python)})
    assert result.returncode == 3
    assert "did not finish" in result.stderr
