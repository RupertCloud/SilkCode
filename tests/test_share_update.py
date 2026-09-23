import subprocess

import pytest

from silkcode.share_update import build_share_update
from silkcode.workspace import ToolError, Workspace


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path, subjects):
    root = tmp_path / "great-app"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    for index, subject in enumerate(subjects):
        (root / "work.txt").write_text(str(index))
        _git(root, "add", "work.txt")
        _git(root, "commit", "-m", subject)
    return root


def test_share_update_builds_link_free_platform_drafts(tmp_path):
    root = _repo(tmp_path, ["Add mobile navigation", "Improve project picker"])

    update = build_share_update(Workspace(str(root)))

    assert update["project"] == "great-app"
    assert update["commit_count"] == 2
    assert "Improve project picker" in update["drafts"]["x"]
    assert len(update["drafts"]["x"]) <= 280
    assert "http://" not in update["drafts"]["x"]
    assert "https://" not in update["drafts"]["x"]
    assert "Add mobile navigation" in update["drafts"]["linkedin"]
    assert update["warnings"] == []


def test_share_update_flags_sensitive_commit_language(tmp_path):
    root = _repo(tmp_path, ["Internal only migration for ops@example.com"])

    warnings = build_share_update(Workspace(str(root)))["warnings"]

    assert "Email address" in warnings
    assert "Private-language marker" in warnings


def test_share_update_requires_git_history(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    with pytest.raises(ToolError, match="Git repository"):
        build_share_update(Workspace(str(root)))
