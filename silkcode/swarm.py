"""Multi-agent improvement swarm.

Runs three agents - tester, critic, worker - in a loop against one workspace:

    1. score the current state 0-10 (test suite results + code hygiene)
    2. if the score meets the target, stop
    3. the tester investigates failures and proposes fixes
    4. the critic reviews the state and returns prioritized suggestions (JSON)
    5. the worker implements the suggestions
    6. repeat ("without end") until the target is reached, the score stalls,
       or a hard iteration cap is hit

Scores and per-iteration traces are saved under ~/.silkcode/swarm/.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .agent import Agent
from .agent.prompts import (
    SWARM_CRITIC_PROMPT,
    SWARM_TESTER_PROMPT,
    SWARM_WORKER_PROMPT,
)
from .checkpoints import Checkpoints
from .config import Config, config_dir
from .permissions import PermissionManager
from .providers import ProviderError, build_provider
from .repomap import repo_map
from .tools.shell import run_command
from .tools.testing import detect_test_command
from .workspace import ToolError, Workspace

# Progress callback: takes a human-readable status line.
ProgressFn = Callable[[str], None]

SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h",
               ".cpp", ".hpp", ".java", ".rb", ".sh", ".kt"}
SKIP_DIRS = {".git", ".silkcode", "node_modules", "venv", ".venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", "target"}
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
DEBUG_MARKERS = ("breakpoint(", "pdb.set_trace(", "debugger;")

MAX_CRITIC_SUGGESTIONS = 5
CLIP_CHARS = 4_000


# --------------------------------------------------------------------------- #
# Scoring (the 0-10 performance metric)
# --------------------------------------------------------------------------- #

@dataclass
class Score:
    score: float            # overall 0..10, one decimal
    tests: float            # test component 0..8
    hygiene: float          # hygiene component 0..2
    tests_passed: int
    tests_failed: int
    test_output: str
    detail: str
    test_command: str | None = None


def _source_files(ws: Workspace):
    for path in ws.root.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_EXTS:
            continue
        rel = path.relative_to(ws.root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path


def _hygiene(ws: Workspace) -> float:
    """Hygiene points (0..2): 1 for no TODO/FIXME markers, 1 for no debug leftovers."""
    points = 0.0
    has_todos = False
    has_debug = False
    for path in _source_files(ws):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if TODO_RE.search(text):
            has_todos = True
        if any(marker in text for marker in DEBUG_MARKERS):
            has_debug = True
    if not has_todos:
        points += 1.0
    if not has_debug:
        points += 1.0
    return points


def _parse_test_counts(output: str) -> tuple[int, int]:
    """Best-effort pass/fail counts from test-runner output (pytest, cargo, go...)."""
    passed = failed = 0
    for match in re.finditer(r"(\d+)\s+passed", output):
        passed = max(passed, int(match.group(1)))
    for match in re.finditer(r"(\d+)\s+failed", output):
        failed = max(failed, int(match.group(1)))
    if failed == 0 and passed == 0:
        match = re.search(r"(\d+)\s+errors", output)  # pytest collection errors
        if match:
            failed = int(match.group(1))
    return passed, failed


def score_workspace(ws: Workspace, test_command: str | None = None,
                    timeout: int = 300) -> Score:
    """Score the workspace 0-10 without any model calls.

    tests (0..8): 8 when the suite exits 0, else 8 * pass ratio.
    hygiene (0..2): no TODO/FIXME markers, no debug leftovers.
    """
    hygiene = _hygiene(ws)
    command = test_command or detect_test_command(ws)
    if not command:
        return Score(round(hygiene, 1), 0.0, hygiene, 0, 0, "",
                     "no test command detected; install a test framework to score higher")
    try:
        output = run_command(ws, command, timeout=timeout)
    except ToolError as exc:
        return Score(round(hygiene, 1), 0.0, hygiene, 0, 0, str(exc),
                     f"test command failed to run: {exc}", command)
    exit_ok = output.startswith("exit code: 0")
    passed, failed = _parse_test_counts(output)
    if exit_ok and passed == 0 and failed == 0:
        tests = 4.0  # suite ran but collected nothing
        detail = "test suite exits 0 but ran no tests"
    elif exit_ok:
        tests = 8.0
        detail = f"all {passed} tests pass"
    elif passed + failed > 0:
        tests = 8.0 * passed / (passed + failed)
        detail = f"{passed} passed, {failed} failed"
    else:
        tests = 0.0
        detail = "test suite fails (could not parse a pass count)"
    return Score(round(tests + hygiene, 1), round(tests, 1), hygiene,
                 passed, failed, output, detail, command)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class SwarmResult:
    iterations: int
    scores: list[float]
    final_score: float
    target: float
    status: str                       # done | stalled | max-iterations | error
    detail: str
    tokens: int = 0
    seconds: float = 0.0
    saved_to: str | None = None
    traces: str | None = None


def _clip(text: str, limit: int = CLIP_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]"


def _summarize(text: str, limit: int = 220) -> str:
    one_line = " ".join((text or "").split())
    return one_line[:limit] + ("..." if len(one_line) > limit else "")


def _diff_summary(ws: Workspace) -> str:
    """Current git state for the critic, or '' when not a git repo."""
    try:
        from .tools.git import git_diff, git_status
        status = git_status(ws)
        diff = git_diff(ws)
        if "git error" in status and "git error" in diff:
            return ""
        return f"git status:\n{status}\n\ngit diff:\n{diff}"
    except Exception:
        return ""


def _parse_critic(content: str) -> dict:
    """Extract {critique, suggestions} JSON from the critic's reply."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        return {"critique": content,
                "suggestions": [{"title": "Review the tester and critic output",
                                 "detail": content}]}
    if not isinstance(data, dict):
        return {"critique": str(content), "suggestions": []}
    raw = data.get("suggestions") or []
    suggestions = []
    for item in raw[:MAX_CRITIC_SUGGESTIONS]:
        if isinstance(item, str):
            suggestions.append({"title": item, "detail": item})
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("detail") or "suggestion")
            detail = str(item.get("detail") or item.get("title") or "")
            suggestions.append({"title": title, "detail": detail})
    data["suggestions"] = suggestions
    return data


def _tester_prompt(score: Score) -> str:
    command = score.test_command or "(none detected)"
    return (
        f"The workspace scores {score.score:.1f}/10 "
        f"(tests {score.tests:.1f}/8, hygiene {score.hygiene:.1f}/2). Target: 10/10.\n\n"
        f"Test command: {command}\n\n"
        f"Test output:\n{_clip(score.test_output)}\n\n"
        "Investigate the failures (read files, run read-only commands) and report:\n"
        "1. What is broken and why.\n"
        "2. Concrete fixes that would make the tests pass.\n"
        "Do not modify any files - the worker implements changes."
    )


def _critic_prompt(score: Score, tester_report: str, diff: str,
                   previous: list[str]) -> str:
    lines = [
        f"The workspace scores {score.score:.1f}/10 "
        f"(tests {score.tests:.1f}/8, hygiene {score.hygiene:.1f}/2). Target: 10/10.",
        f"Test command: {score.test_command or '(none detected)'}",
        f"Test detail: {score.detail}",
        f"\nTester report:\n{_clip(tester_report)}",
        f"\nCurrent diff:\n{_clip(diff) if diff else '(not a git repository)'}",
    ]
    if previous:
        lines.append("\nPreviously suggested (do not repeat unless still unfixed):\n"
                     + "\n".join(f"- {s}" for s in previous[-5:]))
    lines.append(
        "\nReview the repository and return ONLY strict JSON, no markdown fences:\n"
        '{"critique": "<short assessment>", '
        '"suggestions": [{"title": "...", "detail": "..."}]}\n'
        "Up to 5 suggestions, most impactful first. Do not modify files."
    )
    return "\n".join(lines)


def _worker_prompt(parsed: dict, score: Score) -> str:
    lines = [
        f"The workspace scores {score.score:.1f}/10. Target: 10/10.",
        f"Test command: {score.test_command or '(none detected)'}",
    ]
    critique = (parsed.get("critique") or "").strip()
    if critique:
        lines.append(f"\nCritique:\n{critique}")
    suggestions = parsed.get("suggestions") or []
    lines.append("\nImplement these improvements:")
    if not suggestions:
        lines.append("- Inspect the failing tests and fix them directly.")
    for i, item in enumerate(suggestions, 1):
        lines.append(f"{i}. {item.get('title', '')}\n   {item.get('detail', '')}")
    lines.append("\nMake focused changes, run the tests to verify, and finish "
                 "with a concise summary of what you changed.")
    return "\n".join(lines)


def _provider_for(config: Config, cache: dict, spec: str):
    if spec not in cache:
        name, cfg, model = config.resolve_model(spec)
        provider = build_provider(name, cfg, api_key=config.api_key_for(cfg))
        cache[spec] = (provider, model, cfg)
    return cache[spec]


def _make_agent(ws: Workspace, provider, model: str, cfg: dict, role_prompt: str,
                read_only: bool, on_event=None) -> Agent:
    permissions = PermissionManager("ask", asker=lambda prompt: "no") if read_only \
        else PermissionManager("agent")
    context = repo_map(ws)
    if context:
        context += "\n\n"
    context += role_prompt
    return Agent(
        provider, model, ws, permissions,
        checkpoints=Checkpoints(),
        on_event=on_event,
        context=context,
        max_context_tokens=cfg.get("context_tokens") or 60_000,
    )


def run_swarm(
    ws: Workspace,
    worker_spec: str,
    critic_spec: str | None = None,
    tester_spec: str | None = None,
    target: float = 10.0,
    max_iterations: int = 0,          # 0 = run without end (until target/stall)
    stall_limit: int = 3,             # stop after N non-improving iterations
    min_score_delta: float = 0.5,
    test_command: str | None = None,
    on_progress: ProgressFn = lambda s: None,
) -> SwarmResult:
    """Run the tester/critic/worker loop until the target score is reached.

    Returns a SwarmResult; it never raises for agent errors - those are
    recorded in the result's status/detail. Config errors still raise.
    """
    if not 0.0 <= target <= 10.0:
        raise ValueError("target must be between 0 and 10")
    if stall_limit < 1:
        raise ValueError("stall_limit must be >= 1")
    critic_spec = critic_spec or worker_spec
    tester_spec = tester_spec or worker_spec
    config = Config.load()
    cache: dict = {}
    tester_provider, tester_model, tester_cfg = _provider_for(config, cache, tester_spec)
    critic_provider, critic_model, critic_cfg = _provider_for(config, cache, critic_spec)
    worker_provider, worker_model, worker_cfg = _provider_for(config, cache, worker_spec)

    started = time.monotonic()
    scores: list[float] = []
    previous: list[str] = []
    traces: list[dict] = []
    total_tokens = 0
    best = -1.0
    stall = 0
    iteration = 0
    status, detail = "max-iterations", "iteration cap reached"

    def on_worker_event(kind: str, data: object) -> None:
        if kind == "tool_start":
            name = (data or {}).get("name", "?")
            on_progress(f"  worker -> {name}")
        elif kind == "tool_result":
            output = (data or {}).get("output", "")
            on_progress(f"  worker   {_summarize(output, 140)}")

    while max_iterations == 0 or iteration < max_iterations:
        iteration += 1
        on_progress(f"--- iteration {iteration} ---")
        score = score_workspace(ws, test_command=test_command)
        scores.append(score.score)
        on_progress(f"score: {score.score:.1f}/10 "
                    f"(tests {score.tests:.1f}/8, hygiene {score.hygiene:.1f}/2) - {score.detail}")
        if score.score >= target:
            status, detail = "done", f"reached target score {target:.1f}/10"
            break
        if score.score < best + min_score_delta:
            stall += 1
        else:
            stall = 0
        best = max(best, score.score)
        if stall >= stall_limit:
            status = "stalled"
            detail = f"score stuck at {best:.1f}/10 for {stall_limit} consecutive iterations"
            break

        try:
            tester = _make_agent(ws, tester_provider, tester_model, tester_cfg,
                                 SWARM_TESTER_PROMPT, read_only=True)
            tester_report = tester.run_turn(_tester_prompt(score))
            total_tokens += tester.usage.total_tokens
            on_progress(f"tester: {_summarize(tester_report)}")

            critic = _make_agent(ws, critic_provider, critic_model, critic_cfg,
                                 SWARM_CRITIC_PROMPT, read_only=True)
            critic_out = critic.run_turn(
                _critic_prompt(score, tester_report, _diff_summary(ws), previous))
            total_tokens += critic.usage.total_tokens
            parsed = _parse_critic(critic_out)
            suggestions = parsed.get("suggestions") or []
            previous.extend(item.get("title", "") for item in suggestions if item.get("title"))
            on_progress(f"critic: {len(suggestions)} suggestion(s)")

            worker = _make_agent(ws, worker_provider, worker_model, worker_cfg,
                                 SWARM_WORKER_PROMPT, read_only=False,
                                 on_event=on_worker_event)
            worker_out = worker.run_turn(_worker_prompt(parsed, score))
            total_tokens += worker.usage.total_tokens
            on_progress(f"worker: {_summarize(worker_out)}")
        except ProviderError as exc:
            status, detail = "error", f"agent failed: {exc}"
            break

        traces.append({
            "iteration": iteration,
            "score": score.score,
            "tester": tester.messages,
            "critic": critic.messages,
            "worker": worker.messages,
        })

    # Persist scores and per-iteration traces (mirrors benchmark output layout).
    out_dir = config_dir() / "swarm"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    trace_dir = out_dir / f"traces-{stamp}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for i, trace in enumerate(traces, 1):
        (trace_dir / f"iteration-{i}.json").write_text(json.dumps(trace, indent=2))
    out_path = out_dir / f"swarm-{stamp}.json"
    out_path.write_text(json.dumps({
        "status": status,
        "detail": detail,
        "target": target,
        "iterations": iteration,
        "scores": scores,
        "final_score": scores[-1] if scores else 0.0,
        "tokens": total_tokens,
        "seconds": round(time.monotonic() - started, 1),
        "worker_spec": worker_spec,
        "critic_spec": critic_spec,
        "tester_spec": tester_spec,
        "traces": str(trace_dir),
    }, indent=2))

    return SwarmResult(
        iterations=iteration,
        scores=scores,
        final_score=scores[-1] if scores else 0.0,
        target=target,
        status=status,
        detail=detail,
        tokens=total_tokens,
        seconds=round(time.monotonic() - started, 1),
        saved_to=str(out_path),
        traces=str(trace_dir),
    )


def format_swarm_report(result: SwarmResult) -> str:
    lines = [
        f"swarm finished: {result.status}",
        f"iterations: {result.iterations}",
        f"final score: {result.final_score:.1f}/10 (target {result.target:.1f})",
        "score history: " + (" -> ".join(f"{s:.1f}" for s in result.scores) or "-"),
        f"tokens: {result.tokens}   time: {result.seconds}s",
    ]
    if result.detail:
        lines.append(f"detail: {result.detail}")
    if result.saved_to:
        lines.append(f"results saved to {result.saved_to}")
    if result.traces:
        lines.append(f"full traces in {result.traces}")
    return "\n".join(lines)
