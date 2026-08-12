import json

import pytest

from silkcode.tools.testing import detect_test_command, run_tests
from silkcode.workspace import ToolError, Workspace


def test_detect_npm(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect_test_command(Workspace(tmp_path)) == "npm test"


def test_detect_npm_without_test_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    assert detect_test_command(Workspace(tmp_path)) is None


def test_detect_cargo_go_flutter(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]")
    assert detect_test_command(Workspace(tmp_path)) == "cargo test"
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").write_text("module x")
    assert detect_test_command(Workspace(tmp_path)) == "go test ./..."
    (tmp_path / "go.mod").unlink()
    (tmp_path / "pubspec.yaml").write_text("name: x")
    assert detect_test_command(Workspace(tmp_path)) == "flutter test"


def test_detect_pytest_from_tests_dir(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_x(): pass")
    assert detect_test_command(Workspace(tmp_path)) == "pytest"


def test_detect_nothing(tmp_path):
    assert detect_test_command(Workspace(tmp_path)) is None
    with pytest.raises(ToolError):
        run_tests(Workspace(tmp_path))


def test_run_tests_with_explicit_command(tmp_path):
    out = run_tests(Workspace(tmp_path), command="echo tests-ok")
    assert "$ echo tests-ok" in out
    assert "exit code: 0" in out
    assert "tests-ok" in out
