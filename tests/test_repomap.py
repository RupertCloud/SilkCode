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


def test_repo_map_truncates(tmp_path):
    for i in range(300):
        d = tmp_path / f"dir{i:03}"
        d.mkdir()
        (d / "f.py").write_text("")
    text = repo_map(Workspace(tmp_path), max_chars=500)
    assert len(text) <= 500 + len("\n... (map truncated)")
    assert text.endswith("(map truncated)")
