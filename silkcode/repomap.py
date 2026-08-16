"""Compact repository map for the model's context (SRS sections 21-22)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .workspace import Workspace

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".silkcode",
                "dist", "build", ".pytest_cache", ".mypy_cache", "target", ".next"}

LANG_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".rs": "Rust", ".go": "Go", ".java": "Java", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++ header",
    ".swift": "Swift", ".kt": "Kotlin", ".dart": "Dart", ".sh": "Shell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".vue": "Vue", ".svelte": "Svelte",
}

MARKER_FILES = {
    "package.json": "Node.js", "tsconfig.json": "TypeScript", "pyproject.toml": "Python package",
    "setup.py": "Python package", "requirements.txt": "Python", "Cargo.toml": "Rust (Cargo)",
    "go.mod": "Go module", "pubspec.yaml": "Flutter/Dart", "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)", "Gemfile": "Ruby (Bundler)", "composer.json": "PHP (Composer)",
    "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose", "Makefile": "Make",
}

MAX_DEPTH = 3
MAX_CHILDREN_SHOWN = 20
MAX_WALK_DEPTH = 8        # never recurse deeper than this
MAX_SCAN_ENTRIES = 10_000  # hard cap on filesystem entries visited


def repo_map(ws: Workspace, max_chars: int = 3500) -> str:
    languages: Counter = Counter()
    markers: list[str] = []
    tree_lines: list[str] = []
    scanned = 0
    truncated_scan = False

    from .remotews import RemoteWorkspace
    if isinstance(ws, RemoteWorkspace):
        # Remote workspace: build the map purely from the sandbox's file
        # listing - no local disk access, so the repo truly never touches
        # this machine.
        files = ws.list_files()
        top_names = {Path(f).parts[0] for f in files}
        for name in sorted(top_names):
            if name in MARKER_FILES:
                markers.append(f"{name} ({MARKER_FILES[name]})")
        entries: list[tuple[int, str]] = []  # (depth, display)
        seen: set[str] = set()
        for f in files:
            parts = Path(f).parts
            if any(p in IGNORED_DIRS or p.startswith(".") or p.endswith(".egg-info")
                   for p in parts):
                continue
            scanned += 1
            if scanned >= MAX_SCAN_ENTRIES:
                truncated_scan = True
                break
            lang = LANG_BY_EXT.get(Path(f).suffix)
            if lang:
                languages[lang] += 1
            for i in range(len(parts)):
                node = "/".join(parts[: i + 1])
                if node in seen:
                    continue
                seen.add(node)
                is_dir = i < len(parts) - 1
                if i < MAX_DEPTH:
                    entries.append((i, parts[i] + ("/" if is_dir else "")))
        # keep per-depth display capped like the local walk
        counts: dict[int, int] = {}
        for depth, disp in entries:
            if counts.get(depth, 0) >= MAX_CHILDREN_SHOWN:
                if counts.get(depth) == MAX_CHILDREN_SHOWN:
                    tree_lines.append("  " * depth + "... (more entries)")
                    counts[depth] += 1
                continue
            counts[depth] = counts.get(depth, 0) + 1
            tree_lines.append("  " * depth + disp)
        parts = ["Repository map (remote sandbox):"]
    else:
        for name, label in MARKER_FILES.items():
            if (ws.root / name).is_file():
                markers.append(f"{name} ({label})")

        def walk(directory: Path, depth: int) -> None:
            nonlocal scanned, truncated_scan
            if scanned >= MAX_SCAN_ENTRIES or depth > MAX_WALK_DEPTH:
                truncated_scan = True
                return
            try:
                children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            shown = 0
            for child in children:
                if scanned >= MAX_SCAN_ENTRIES:
                    truncated_scan = True
                    return
                if child.name in IGNORED_DIRS or child.name.startswith(".") or child.name.endswith(".egg-info"):
                    continue
                scanned += 1
                is_dir = child.is_dir()
                if not is_dir:
                    lang = LANG_BY_EXT.get(child.suffix)
                    if lang:
                        languages[lang] += 1
                if depth < MAX_DEPTH:
                    if shown == MAX_CHILDREN_SHOWN:
                        tree_lines.append("  " * depth + "... (more entries)")
                        shown += 1
                    elif shown < MAX_CHILDREN_SHOWN:
                        tree_lines.append("  " * depth + child.name + ("/" if is_dir else ""))
                        shown += 1
                if is_dir:
                    walk(child, depth + 1)

        walk(ws.root, 0)
        parts = ["Repository map:"]

    if truncated_scan:
        parts.append(f"(large directory: scan capped at {MAX_SCAN_ENTRIES} entries / depth {MAX_WALK_DEPTH}; "
                     "counts are partial)")
    if languages:
        top = ", ".join(f"{lang} ({count} files)" for lang, count in languages.most_common(6))
        parts.append(f"Languages: {top}")
    if markers:
        parts.append(f"Detected: {', '.join(markers)}")
    if tree_lines:
        parts.append("Structure:")
        parts.extend(tree_lines)
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (map truncated)"
    return text
