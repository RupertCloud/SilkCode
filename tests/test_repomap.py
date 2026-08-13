from silkcode.repomap import repo_map
from silkcode.workspace import Workspace


def test_repo_map_languages_markers_structure(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('hi')")
    (src / "util.py").write_text("")
    (tmp_path / "web.ts").write_text("")

    text = repo_map(Workspace(tmp_path))
    assert text.startswith("Repository map:")
    assert "Python (2 files)" in text
    assert "TypeScript (1 files)" in text
    assert "pyproject.toml (Python package)" in text
    assert "src/" in text
    assert "main.py" in text


def test_repo_map_skips_ignored_dirs(tmp_path):
    hidden = tmp_path / "node_modules" / "pkg"
    hidden.mkdir(parents=True)
    (hidden / "index.js").write_text("")
    text = repo_map(Workspace(tmp_path))
    assert "node_modules" not in text
    assert "JavaScript" not in text


def test_repo_map_bounded_on_huge_trees(tmp_path):
    # a deep chain (40 levels) plus a wide fan-out must not be fully walked
    deep = tmp_path
    for i in range(40):
        deep = deep / f"level{i}"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("")
    wide = tmp_path / "wide"
    wide.mkdir()
    for i in range(30):
        d = wide / f"d{i:02}"
        d.mkdir()
        for j in range(20):
            (d / f"f{j}.py").write_text("")

    import time
    started = time.monotonic()
    text = repo_map(Workspace(tmp_path))
    assert time.monotonic() - started < 5
    assert text.startswith("Repository map:")
    # the 40-deep file is beyond MAX_WALK_DEPTH, so it was never visited
    from silkcode.repomap import MAX_WALK_DEPTH
    assert MAX_WALK_DEPTH < 40


def test_repo_map_scan_cap_reports_truncation(tmp_path, monkeypatch):
    import silkcode.repomap as rm
    monkeypatch.setattr(rm, "MAX_SCAN_ENTRIES", 10)
    for i in range(50):
        (tmp_path / f"file{i:02}.py").write_text("")
    text = rm.repo_map(Workspace(tmp_path))
    assert "scan capped" in text


def test_repo_map_truncates(tmp_path):
    for i in range(300):
        d = tmp_path / f"dir{i:03}"
        d.mkdir()
        (d / "f.py").write_text("")
    text = repo_map(Workspace(tmp_path), max_chars=500)
    assert len(text) <= 500 + len("\n... (map truncated)")
    assert text.endswith("(map truncated)")
