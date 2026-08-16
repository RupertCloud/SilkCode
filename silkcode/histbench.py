"""Benchmark tasks mined from the repository's own history (SRS section 55).

Public benchmarks are contaminated (every model has trained on them) and
generic. A project's own merged work is neither: it is private, and it looks
exactly like the code the agent will be asked to write next.

For each candidate change this module reconstructs the task the developer
originally faced:

    base commit  +  the change's test files  ->  tests must FAIL
    base commit  +  the whole change         ->  tests must PASS

Only candidates that satisfy both conditions become tasks, which guarantees
the tests really capture the change and that the task is solvable in this
environment. At benchmark time the agent gets the base snapshot with the
tests already applied and the original request as its instruction; the test
files are protected, so deleting or weakening them fails the task.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .tools.testing import detect_test_command
from .workspace import ToolError, Workspace

# Paths that hold tests rather than implementation.
TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.py$"
    r"|\.(test|spec)\.[jt]sx?$|_test\.go$|(^|/)conftest\.py$"
)
# Trailers and footers that are not part of the developer's request.
TRAILER = re.compile(
    r"^(Co-Authored-By|Co-authored-by|Signed-off-by|X-Silk-[\w-]+|Claude-Session|"
    r"Reviewed-by|Refs|Closes|Fixes)\s*:", re.I
)
MERGE_PR_FROM = re.compile(r"^Merge pull request #(\d+) from \S+$")
MERGE_PR_TITLED = re.compile(r"^Merge pull request #(\d+):?\s*(.+)$")

DIFFICULTIES = ("easy", "medium", "hard")
# (minimum implementation lines changed, minimum implementation files changed)
THRESHOLDS = {"easy": (20, 1), "medium": (50, 2), "hard": (100, 3)}

MAX_INSTRUCTION_CHARS = 2000


@dataclass
class MinedTask:
    """A validated task reconstructed from one change in the repository."""

    id: str
    instruction: str
    repo: str
    base_commit: str
    head_commit: str
    test_paths: list[str]
    test_command: str
    difficulty: str
    impl_files: int
    impl_lines: int
    subject: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> "MinedTask":
        return MinedTask(**data)


def _git(repo: Path, *args: str, timeout: int = 60) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ToolError(f"git {' '.join(args[:2])} failed: "
                        f"{(proc.stderr or proc.stdout).strip().splitlines()[:1]}")
    return proc.stdout


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def clean_instruction(subject: str, body: str) -> str:
    """The developer's request, without merge boilerplate or trailers."""
    title = subject.strip()
    body_lines = [ln for ln in body.splitlines()]
    from_match = MERGE_PR_FROM.match(title)
    if from_match:
        # GitHub's default merge subject; the real title is the first body line
        first = next((ln.strip() for ln in body_lines if ln.strip()), "")
        title = first or f"Pull request #{from_match.group(1)}"
        body_lines = body_lines[body_lines.index(first) + 1:] if first in body_lines else []
    else:
        titled = MERGE_PR_TITLED.match(title)
        if titled:
            title = titled.group(2).strip()
    kept: list[str] = []
    for line in body_lines:
        stripped = line.strip()
        if TRAILER.match(stripped) or stripped.startswith("🤖 Generated with"):
            continue
        if stripped.startswith("https://claude.ai/"):
            continue
        kept.append(line.rstrip())
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    text = title if not kept else title + "\n\n" + "\n".join(kept)
    if len(text) > MAX_INSTRUCTION_CHARS:
        text = text[:MAX_INSTRUCTION_CHARS].rstrip() + "\n... [truncated]"
    return text


def difficulty_of(impl_lines: int, impl_files: int) -> str | None:
    for name in reversed(DIFFICULTIES):  # hardest tier that fits
        min_lines, min_files = THRESHOLDS[name]
        if impl_lines >= min_lines and impl_files >= min_files:
            return name
    return None


@dataclass
class Candidate:
    sha: str
    base: str
    subject: str
    body: str
    test_paths: list[str] = field(default_factory=list)
    impl_paths: list[str] = field(default_factory=list)
    impl_lines: int = 0


def candidates(repo: Path, scan: int = 60) -> list[Candidate]:
    """Recent changes that touch both tests and implementation.

    Merge commits are compared against their first parent (the whole merged
    change); ordinary commits against their parent.
    """
    log = _git(repo, "log", f"-{max(int(scan), 1)}", "--first-parent",
               "--format=%x00%H%x01%P%x01%s%x01%b")
    out: list[Candidate] = []
    for record in log.split("\x00"):
        if not record.strip():
            continue
        parts = record.split("\x01")
        if len(parts) < 4:
            continue
        sha, parents, subject, body = parts[0].strip(), parts[1].split(), parts[2], parts[3]
        if not parents:
            continue  # root commit has no base state
        base = parents[0]
        try:
            numstat = _git(repo, "diff", "--numstat", f"{base}..{sha}")
        except ToolError:
            continue
        cand = Candidate(sha=sha, base=base, subject=subject, body=body)
        for line in numstat.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            added, removed, path = fields
            if is_test_path(path):
                cand.test_paths.append(path)
            else:
                cand.impl_paths.append(path)
                cand.impl_lines += sum(int(n) for n in (added, removed) if n.isdigit())
        if cand.test_paths and cand.impl_paths:
            out.append(cand)
    return out


def _export(repo: Path, commit: str, dest: Path, paths: list[str] | None = None) -> None:
    """Materialize a commit (or some of its paths) into a plain directory."""
    dest.mkdir(parents=True, exist_ok=True)
    args = ["archive", "--format=tar", commit]
    if paths:
        args += ["--"] + paths
    archive = subprocess.run(["git", "-C", str(repo), *args],
                             capture_output=True, timeout=120)
    if archive.returncode != 0:
        raise ToolError(f"git archive {commit[:8]} failed: "
                        f"{archive.stderr.decode('utf-8', 'replace')[:200]}")
    if not archive.stdout:
        return
    untar = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout,
                           capture_output=True, timeout=120)
    if untar.returncode != 0:
        raise ToolError(f"extracting {commit[:8]} failed: "
                        f"{untar.stderr.decode('utf-8', 'replace')[:200]}")


def materialize(repo: Path, cand_base: str, cand_head: str, test_paths: list[str],
                dest: Path, with_implementation: bool = False) -> None:
    """Base snapshot plus the change's tests — the task's starting state.
    With with_implementation the whole change is applied instead (the
    reference solution)."""
    _export(repo, cand_head if with_implementation else cand_base, dest)
    if not with_implementation:
        _export(repo, cand_head, dest, test_paths)


def _test_command_for(ws: Workspace, test_paths: list[str]) -> str | None:
    command = detect_test_command(ws)
    if not command:
        return None
    if "pytest" in command:
        existing = [p for p in test_paths if (ws.root / p).exists()]
        if existing:
            return command + " " + " ".join(existing)
    return command


def run_task_tests(ws: Workspace, command: str, timeout: int = 300) -> str:
    """Run a mined task's tests against the snapshot, not the developer's
    installed copy.

    Without this the snapshot is a lie for any project installed in editable
    mode: `import <project>` would resolve to the live working tree through
    site-packages, so the tests would pass even at the base commit.
    """
    from .tools.shell import run_command
    return run_command(ws, f'PYTHONPATH={ws.root}:"${{PYTHONPATH:-}}" {command}',
                       timeout=timeout)


def _tests_pass(ws: Workspace, command: str, timeout: int) -> tuple[bool, str]:
    out = run_task_tests(ws, command, timeout)
    return out.startswith("exit code: 0"), out


def validate(repo: Path, cand: Candidate, timeout: int = 300,
             on_progress: Callable[[str], None] = lambda s: None) -> MinedTask | None:
    """Keep the candidate only if its tests fail on the base state and pass
    once the original implementation is applied."""
    difficulty = difficulty_of(cand.impl_lines, len(cand.impl_paths))
    if difficulty is None:
        return None
    with tempfile.TemporaryDirectory(prefix="silkmine-") as tmp:
        before = Path(tmp) / "before"
        materialize(repo, cand.base, cand.sha, cand.test_paths, before)
        ws_before = Workspace(str(before))
        command = _test_command_for(ws_before, cand.test_paths)
        if not command:
            on_progress(f"  {cand.sha[:8]}: skipped (no test framework detected)")
            return None
        passed, _ = _tests_pass(ws_before, command, timeout)
        if passed:
            on_progress(f"  {cand.sha[:8]}: skipped (tests already pass without the change)")
            return None

        after = Path(tmp) / "after"
        materialize(repo, cand.base, cand.sha, cand.test_paths, after,
                    with_implementation=True)
        ws_after = Workspace(str(after))
        passed, output = _tests_pass(ws_after, command, timeout)
        if not passed:
            first = output.splitlines()[0] if output else "no output"
            on_progress(f"  {cand.sha[:8]}: skipped (tests fail with the original change: {first})")
            return None

    return MinedTask(
        id=f"hist-{cand.sha[:8]}",
        instruction=clean_instruction(cand.subject, cand.body),
        repo=str(repo),
        base_commit=cand.base,
        head_commit=cand.sha,
        test_paths=cand.test_paths,
        test_command=command,
        difficulty=difficulty,
        impl_files=len(cand.impl_paths),
        impl_lines=cand.impl_lines,
        subject=cand.subject.strip(),
    )


def mine(repo: str | Path, limit: int = 5, scan: int = 60,
         difficulties: list[str] | None = None, timeout: int = 300,
         on_progress: Callable[[str], None] = lambda s: None) -> list[MinedTask]:
    """Mine up to `limit` validated tasks from the most recent `scan` changes."""
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        raise ToolError(f"{repo} is not a git repository")
    wanted = set(difficulties or DIFFICULTIES)
    unknown = wanted - set(DIFFICULTIES)
    if unknown:
        raise ToolError(f"unknown difficulty: {', '.join(sorted(unknown))}; "
                        f"choose from {', '.join(DIFFICULTIES)}")
    found: list[MinedTask] = []
    cands = candidates(repo, scan=scan)
    on_progress(f"scanning {len(cands)} candidate change(s) that touch tests and code ...")
    for cand in cands:
        if len(found) >= limit:
            break
        task = validate(repo, cand, timeout=timeout, on_progress=on_progress)
        if task is None:
            continue
        if task.difficulty not in wanted:
            on_progress(f"  {cand.sha[:8]}: skipped ({task.difficulty} not requested)")
            continue
        on_progress(f"  {task.id}: accepted [{task.difficulty}] {task.subject[:60]}")
        found.append(task)
    return found


def save_tasks(tasks: list[MinedTask], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([t.to_json() for t in tasks], indent=2) + "\n")
    return path


def load_tasks(path: Path) -> list[MinedTask]:
    data = json.loads(Path(path).read_text())
    return [MinedTask.from_json(item) for item in data]


PROMPT_TEMPLATE = """{instruction}

This repository has failing tests that describe the work. Implement the change
so the whole test suite passes. Do not modify the test files: {tests}."""


def to_bench_task(task: MinedTask):
    """Convert a mined task into a BenchTask the benchmark runner can execute."""
    from .benchmark import BenchTask

    repo = Path(task.repo)

    def setup(root: Path) -> None:
        materialize(repo, task.base_commit, task.head_commit, task.test_paths, root)

    # Test contents are carried so the runner can detect tampering.
    protected_files: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="silkmine-ref-") as tmp:
        snapshot = Path(tmp)
        _export(repo, task.head_commit, snapshot, task.test_paths)
        for rel in task.test_paths:
            candidate = snapshot / rel
            if candidate.is_file():
                protected_files[rel] = candidate.read_text(errors="replace")

    def check(ws: Workspace, bench_task) -> tuple[bool, str]:
        for rel, content in protected_files.items():
            current = (ws.root / rel).read_text(errors="replace") if (ws.root / rel).exists() else ""
            if current != content:
                return False, f"protected test file {rel} was modified"
        out = run_task_tests(ws, task.test_command, timeout=300)
        if not out.startswith("exit code: 0"):
            return False, out.splitlines()[0] if out else "no output"
        return True, "tests pass"

    return BenchTask(
        id=task.id,
        prompt=PROMPT_TEMPLATE.format(instruction=task.instruction,
                                      tests=", ".join(task.test_paths)),
        files=protected_files,
        check=check,
        protected=tuple(protected_files),
        setup=setup,
    )
