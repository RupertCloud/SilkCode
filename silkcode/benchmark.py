"""Model benchmarking against real end-to-end coding tasks (SRS sections 55, 79).

Each task runs through the full agent loop (tools, permissions, checkpoints)
in a fresh temporary workspace, then an independent checker verifies the
result by executing code. Results are saved under ~/.silkcode/benchmarks/.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agent import Agent
from .checkpoints import Checkpoints
from .config import Config, config_dir
from .permissions import PermissionManager
from .providers import ProviderError, build_provider
from .repomap import repo_map
from .tools.shell import run_command
from .workspace import Workspace


@dataclass
class BenchTask:
    id: str
    prompt: str
    files: dict[str, str]                                     # initial workspace
    check: Callable[["Workspace", "BenchTask"], tuple[bool, str]]  # (passed, detail)
    solution: dict[str, str] = field(default_factory=dict)  # reference, used by tests
    protected: tuple[str, ...] = ()                         # files the agent must not change


def _run_ok(ws: Workspace, command: str, expected_stdout: str | None = None) -> tuple[bool, str]:
    out = run_command(ws, command, timeout=60)
    if not out.startswith("exit code: 0"):
        return False, out.splitlines()[0] if out else "no output"
    if expected_stdout is not None:
        body = "\n".join(out.splitlines()[1:]).strip()
        if body != expected_stdout:
            return False, f"expected {expected_stdout!r}, got {body!r}"
    return True, "ok"


def _protected_unchanged(ws: Workspace, task: "BenchTask") -> tuple[bool, str]:
    for name in task.protected:
        current = (ws.root / name).read_text() if (ws.root / name).exists() else ""
        if current != task.files[name]:
            return False, f"protected file {name} was modified"
    return True, "ok"


def _check_with_protection(command: str, expected: str | None = None):
    def check(ws: Workspace, task: BenchTask) -> tuple[bool, str]:
        ok, detail = _protected_unchanged(ws, task)
        if not ok:
            return ok, detail
        return _run_ok(ws, command, expected)
    return check


STATS_BUGGY = "def mean(xs):\n    return sum(xs) / (len(xs) - 1)\n"
STATS_CHECK = (
    "from stats import mean\n"
    "assert mean([2, 4, 6]) == 4\n"
    "assert mean([10]) == 10\n"
    "print('ok')\n"
)
TEXTUTIL = "def shout(s):\n    return s.upper()\n"
TEXTUTIL_CHECK = (
    "from textutil import shout, slugify\n"
    "assert shout('hi') == 'HI'\n"
    "assert slugify('Hello World!') == 'hello-world'\n"
    "assert slugify('  Silk  Code  ') == 'silk-code'\n"
    "print('ok')\n"
)
SAMPLE_TXT = "the quick brown fox\njumps over the lazy dog\n"
GEO = "def area_of_circle(r):\n    return 3.14159 * r * r\n"
GEO_APP = "from geo import area_of_circle\nprint(round(area_of_circle(2), 2))\n"
GEO_CHECK = (
    "from geo import circle_area\n"
    "assert round(circle_area(1), 5) == 3.14159\n"
    "import subprocess, sys\n"
    "out = subprocess.run([sys.executable, 'app.py'], capture_output=True, text=True)\n"
    "assert out.returncode == 0 and out.stdout.strip() == '12.57', out\n"
    "print('ok')\n"
)


def _make_tasks() -> list[BenchTask]:
    tasks = [
        BenchTask(
            id="hello-cli",
            prompt="Create a file hello.py that prints exactly: Hello, Silk!",
            files={},
            check=_check_with_protection("python3 hello.py", "Hello, Silk!"),
            solution={"hello.py": "print('Hello, Silk!')\n"},
        ),
        BenchTask(
            id="fix-bug",
            prompt="Running check_stats.py fails. Find and fix the bug in stats.py. "
                   "Do not modify check_stats.py.",
            files={"stats.py": STATS_BUGGY, "check_stats.py": STATS_CHECK},
            check=_check_with_protection("python3 check_stats.py", "ok"),
            solution={"stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n"},
            protected=("check_stats.py",),
        ),
        BenchTask(
            id="add-feature",
            prompt="Add a slugify(s) function to textutil.py: lowercase, words joined by "
                   "single hyphens, no leading/trailing hyphens, non-alphanumeric characters "
                   "removed. check_textutil.py must pass; do not modify it.",
            files={"textutil.py": TEXTUTIL, "check_textutil.py": TEXTUTIL_CHECK},
            check=_check_with_protection("python3 check_textutil.py", "ok"),
            solution={"textutil.py": TEXTUTIL +
                      "\nimport re\n\ndef slugify(s):\n"
                      "    words = re.findall(r'[a-z0-9]+', s.lower())\n"
                      "    return '-'.join(words)\n"},
            protected=("check_textutil.py",),
        ),
        BenchTask(
            id="word-count",
            prompt="Create wordcount.py so that 'python3 wordcount.py <file>' prints the "
                   "number of lines and the number of words separated by one space, e.g. '3 12'.",
            files={"sample.txt": SAMPLE_TXT},
            check=_check_with_protection("python3 wordcount.py sample.txt", "2 9"),
            solution={"wordcount.py":
                      "import sys\n"
                      "text = open(sys.argv[1]).read()\n"
                      "print(len(text.splitlines()), len(text.split()))\n"},
        ),
        BenchTask(
            id="rename-symbol",
            prompt="Rename the function area_of_circle in geo.py to circle_area, updating all "
                   "usages so app.py still works. Do not modify check_geo.py.",
            files={"geo.py": GEO, "app.py": GEO_APP, "check_geo.py": GEO_CHECK},
            check=_check_with_protection("python3 check_geo.py", "ok"),
            solution={"geo.py": GEO.replace("area_of_circle", "circle_area"),
                      "app.py": GEO_APP.replace("area_of_circle", "circle_area")},
            protected=("check_geo.py",),
        ),
    ]
    return tasks


TASKS: list[BenchTask] = _make_tasks()


def run_task(provider, model: str, task: BenchTask, max_context_tokens: int = 60_000,
             include_context: bool = True, trace_path: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"silkbench-{task.id}-") as tmp:
        ws = Workspace(tmp)
        for name, content in task.files.items():
            path = ws.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        agent = Agent(
            provider, model, ws,
            PermissionManager("agent"),  # high-risk commands are auto-denied (asker says no)
            checkpoints=Checkpoints(),
            context=repo_map(ws) if include_context else None,
            max_context_tokens=max_context_tokens,
        )
        started = time.monotonic()
        error = None
        try:
            agent.run_turn(task.prompt)
        except ProviderError as exc:
            error = str(exc)
        elapsed = time.monotonic() - started
        if error:
            passed, detail = False, f"provider error: {error}"
        else:
            passed, detail = task.check(ws, task)
        result = {
            "task": task.id,
            "passed": passed,
            "detail": detail,
            "seconds": round(elapsed, 1),
            "tokens": agent.usage.total_tokens,
            "steps": sum(1 for m in agent.messages if m.get("role") == "tool"),
        }
        if trace_path is not None:
            # full trace for provenance / later review (Nimbalyst-style protocol)
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(json.dumps(
                {"result": result, "messages": agent.messages}, indent=2))
        return result


def run_benchmark(specs: list[str], task_ids: list[str] | None = None,
                  ab: bool = False,
                  on_progress: Callable[[str], None] = lambda s: None) -> dict:
    """Run the benchmark for one or more model specs.

    With ab=True each task runs twice per model (paired-condition protocol):
    'baseline' with the bare agent, 'harness' with the project context
    injected. Everything else - task, model, prompts, permissions - is held
    constant, so the difference is the harness's contribution.
    """
    config = Config.load()
    tasks = [t for t in TASKS if not task_ids or t.id in task_ids]
    if not tasks:
        raise ValueError(f"no matching tasks; available: {', '.join(t.id for t in TASKS)}")
    started = time.time()
    out_dir = config_dir() / "benchmarks"
    trace_dir = out_dir / f"traces-{int(started)}"
    results: dict = {"models": [], "started": started}
    conditions = [("baseline", False), ("harness", True)] if ab else [("harness", True)]
    for spec in specs:
        provider_name, cfg, model = config.resolve_model(spec)
        provider = build_provider(provider_name, cfg, api_key=config.api_key_for(cfg))
        for condition, include_context in conditions:
            label = f"{provider_name}/{model}" + (f" [{condition}]" if ab else "")
            on_progress(f"benchmarking {label} ...")
            task_results = []
            for task in tasks:
                safe_label = label.replace("/", "-").replace(" ", "")
                result = run_task(
                    provider, model, task,
                    max_context_tokens=cfg.get("context_tokens") or 60_000,
                    include_context=include_context,
                    trace_path=trace_dir / f"{safe_label}-{task.id}.json",
                )
                on_progress(f"  {task.id}: {'PASS' if result['passed'] else 'FAIL'} "
                            f"({result['seconds']}s, {result['tokens']} tokens)")
                task_results.append(result)
            results["models"].append({
                "model": label,
                "spec": spec,
                "condition": condition,
                "tasks": task_results,
                "solved": sum(1 for r in task_results if r["passed"]),
                "total": len(task_results),
                "tokens": sum(r["tokens"] for r in task_results),
                "seconds": round(sum(r["seconds"] for r in task_results), 1),
            })
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"benchmark-{int(started)}.json"
    out_path.write_text(json.dumps(results, indent=2))
    results["saved_to"] = str(out_path)
    results["traces"] = str(trace_dir)
    return results


def format_results(results: dict) -> str:
    lines = [f"{'model':<36} {'solved':<8} {'tokens':<10} time"]
    for entry in results["models"]:
        lines.append(f"{entry['model']:<36} {entry['solved']}/{entry['total']:<6} "
                     f"{entry['tokens']:<10} {entry['seconds']}s")
        failed = [r["task"] for r in entry["tasks"] if not r["passed"]]
        if failed:
            lines.append(f"{'':<36} failed: {', '.join(failed)}")
    if results.get("saved_to"):
        lines.append(f"\nresults saved to {results['saved_to']}")
    if results.get("traces"):
        lines.append(f"full traces in {results['traces']}")
    return "\n".join(lines)
