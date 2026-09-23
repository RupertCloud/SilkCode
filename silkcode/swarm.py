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
    EPISODE_CONTRACT,
    SWARM_CRITIC_PROMPT,
    SWARM_TESTER_PROMPT,
    SWARM_WORKER_PROMPT,
    TEAM_DEVELOPER_PROMPT,
    TEAM_ROLE_PROMPTS,
)
from .checkpoints import Checkpoints
from .config import Config, config_dir
from .permissions import PermissionManager
from .providers import ProviderError, build_provider
from .repomap import repo_map
from .roles import custom_specialists, load_roles, role_model, role_prompt
from .roles import withheld as withheld_roles
from .tools.shell import run_command
from .tools.testing import detect_test_command
from .workspace import ToolError, Workspace

# Progress callback: takes a human-readable status line.
ProgressFn = Callable[[str], None]

SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h",
               ".cpp", ".hpp", ".java", ".rb", ".sh", ".kt"}
SKIP_DIRS = {".git", ".silkcode", "node_modules", "venv", ".venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", "target"}
# Marker literals are split so this module (which defines the marker regex)
# does not match its own source when the workspace is scored for hygiene.
_TODO = "TO" + "DO"
_FIXME = "FIX" + "ME"
_XXX = "XX" + "X"
_HACK = "HAC" + "K"
TODO_RE = re.compile(rf"\b({_TODO}|{_FIXME}|{_XXX}|{_HACK})\b")
DEBUG_MARKERS = ("break" + "point(", "pdb.set" + "_trace(", "debug" + "ger;")

MAX_CRITIC_SUGGESTIONS = 5
# What one retained episode may occupy in the next dispatch's prompt. Long
# enough for commands and file paths, short enough that three of them do not
# crowd out the actual task.
EPISODE_CHARS = 2_000
MAX_TEAM_DEVELOPERS = 12
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
    """Hygiene points (0..2): 1 for no leftover marker comments, 1 for no debug leftovers."""
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
    hygiene (0..2): no leftover marker comments, no debug leftovers.
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
    status: str                       # done | stalled | max-iterations | token-budget | stopped | error
    detail: str
    tokens: int = 0
    seconds: float = 0.0
    saved_to: str | None = None
    traces: str | None = None
    role_tokens: dict = None          # {"tester": n, "critic": n, "worker": n}
    artifacts: dict = None            # latest shared team artifacts


# Structured events emitted via on_event(kind, data):
#   ("iteration", {"iteration": n})
#   ("score",     {"score": f, "tests": f, "hygiene": f, "detail": str, "iteration": n})
#   ("phase",     {"role": "tester"|"critic"|"worker"})
#   ("log",       {"line": str})                 # mirrors on_progress
SwarmEventHandler = Callable[[str, dict], None]


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


def _parse_team_plan(content: str, max_developers: int = MAX_TEAM_DEVELOPERS) -> dict:
    """Normalize the lead's plan and reject unsafe/unrecognized task owners."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        raw = json.loads(text)
    except ValueError:
        return {"summary": _summarize(content, 500), "tasks": []}
    if not isinstance(raw, dict):
        return {"summary": str(raw), "tasks": []}
    tasks = []
    for item in raw.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"dev([1-9][0-9]*)", str(item.get("owner") or "").lower())
        if not match or int(match.group(1)) > max_developers:
            continue
        tasks.append({
            "owner": str(item["owner"]).lower(),
            "title": str(item.get("title") or "Assigned improvement"),
            "detail": str(item.get("detail") or ""),
            "acceptance": [str(x) for x in (item.get("acceptance") or [])][:6],
        })
    return {"summary": str(raw.get("summary") or ""),
            "tasks": tasks[:max_developers * 3]}


def _team_discovery_prompt(role: str, objective: str, score: Score) -> str:
    return (f"Product objective:\n{objective}\n\nCurrent score: {score.score:.1f}/10. "
            f"Test state: {score.detail}.\nInspect the repository and provide your {role} brief. "
            "Do not modify files.")


def _team_head_prompt(objective: str, artifacts: dict, score: Score,
                      developer_count: int) -> str:
    staffing = (f"Use exactly dev1 through dev{developer_count} when useful; do not assign "
                "owners above that number." if developer_count else
                f"Choose the smallest effective team from dev1 through dev{MAX_TEAM_DEVELOPERS}. "
                "Simple work should use one developer; use more only for genuinely separable work.")
    return (f"Product objective:\n{objective}\n\nCurrent score: {score.score:.1f}/10.\n\n"
            f"Shared team briefs:\n{_clip(json.dumps(artifacts, indent=2), 12000)}\n\n"
            f"{staffing}\nInspect the repository and return ONLY strict JSON: "
            '{"summary":"...","tasks":[{"owner":"dev1","title":"...",'
            '"detail":"...","acceptance":["..."]}]}')


def _developer_prompt(task: dict, objective: str, plan: dict) -> str:
    acceptance = "\n".join(f"- {x}" for x in task.get("acceptance") or []) or "- Verify the task works"
    return (f"Product objective: {objective}\nTeam plan: {plan.get('summary', '')}\n\n"
            f"Your task: {task['title']}\n{task.get('detail', '')}\n\n"
            f"Acceptance criteria:\n{acceptance}\n\nImplement this task now. Run focused tests and report the result.")


def _episode_of(report: str) -> str:
    """The retained episode from a role's final message.

    The contract asks for an EPISODE block at the end; when a model ignores
    that (small ones do), the tail of the report is the closest thing to a
    work record, so keep that instead of nothing.
    """
    text = (report or "").strip()
    if not text:
        return ""
    marker = text.rfind("EPISODE")
    episode = text[marker:] if marker != -1 else text
    return episode[-EPISODE_CHARS:]


def _episodes_section(episodes: dict, *roles: str) -> str:
    """Retained episodes as prompt text, or "" when there are none yet."""
    parts = [f"[{role}]\n{episodes[role]}" for role in roles if episodes.get(role)]
    if not parts:
        return ""
    return ("\nRetained episodes from the previous iteration (work already "
            "done - build on it, do not rediscover it):\n" + "\n\n".join(parts) + "\n")


def _tester_prompt(score: Score, episodes: dict | None = None) -> str:
    command = score.test_command or "(none detected)"
    return (
        f"The workspace scores {score.score:.1f}/10 "
        f"(tests {score.tests:.1f}/8, hygiene {score.hygiene:.1f}/2). Target: 10/10.\n\n"
        f"Test command: {command}\n\n"
        f"Test output:\n{_clip(score.test_output)}\n\n"
        + _episodes_section(episodes or {}, "worker", "tester")
        + "Investigate the failures (read files, run read-only commands) and report:\n"
        "1. What is broken and why.\n"
        "2. Concrete fixes that would make the tests pass.\n"
        "Do not modify any files - the worker implements changes."
    )


def _critic_prompt(score: Score, tester_report: str, diff: str,
                   previous: list[str], episodes: dict | None = None) -> str:
    lines = [
        f"The workspace scores {score.score:.1f}/10 "
        f"(tests {score.tests:.1f}/8, hygiene {score.hygiene:.1f}/2). Target: 10/10.",
        f"Test command: {score.test_command or '(none detected)'}",
        f"Test detail: {score.detail}",
        f"\nTester report:\n{_clip(tester_report)}",
        f"\nCurrent diff:\n{_clip(diff) if diff else '(not a git repository)'}",
    ]
    section = _episodes_section(episodes or {}, "worker")
    if section:
        lines.append(section)
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


def _worker_prompt(parsed: dict, score: Score, episodes: dict | None = None) -> str:
    lines = [
        f"The workspace scores {score.score:.1f}/10. Target: 10/10.",
        f"Test command: {score.test_command or '(none detected)'}",
    ]
    section = _episodes_section(episodes or {}, "worker")
    if section:
        lines.append(section)
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
                read_only: bool, on_event=None,
                worker_permissions: PermissionManager | None = None,
                owner: str | None = None) -> Agent:
    # tester/critic are read-only: they never touch files, so deny everything.
    # the worker uses the caller-provided permission manager (e.g. the GUI
    # session's, so it can ask the user) or falls back to full agent mode.
    permissions = PermissionManager("ask", asker=lambda prompt: "no") if read_only \
        else (worker_permissions or PermissionManager("agent"))
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
        lock_owner=owner,
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
    max_tokens: int = 0,              # 0 = no token budget; else stop once exceeded
    test_command: str | None = None,
    skip_tester_when_tests_pass: bool = True,
    on_progress: ProgressFn = lambda s: None,
    on_event: SwarmEventHandler | None = None,
    should_stop: Callable[[], bool] | None = None,
    worker_permissions: PermissionManager | None = None,
    worker_owner: str | None = None,
    team_mode: bool = False,
    objective: str | None = None,
    developer_count: int = 0,         # 0 = Head chooses, otherwise Dev1..DevN
) -> SwarmResult:
    """Run the tester/critic/worker loop until the target score is reached.

    `worker_permissions` (optional) is the PermissionManager the worker
    agent uses, so the swarm can ask the user before writes/commands instead
    of auto-approving. When omitted the worker runs in full agent mode
    (writes and MEDIUM commands allowed; HIGH commands auto-denied). The
    tester and critic are always read-only and never prompt.

    `worker_owner` (optional) is the advisory-lock owner the worker writes
    under (e.g. the GUI session id), so its writes are allowed while that
    session holds the workspace lock.

    Efficiency controls:
      - `max_tokens` caps total model tokens; the swarm stops with status
        "token-budget" once the cap is exceeded (checked before each round).
      - `skip_tester_when_tests_pass` (default True) skips the read-only
        tester whenever the test suite is already green or there is no test
        command - the critic still runs to suggest hygiene improvements, so
        an all-green repo costs 2 agents per round instead of 3.
      - the worker is skipped when the critic returns no suggestions and the
        tests already pass (nothing left to implement).
      - per-role token usage is tracked (result.role_tokens) so you can see
        where the budget goes.

    `should_stop` (optional) is polled before each iteration; when it returns
    True the swarm stops with status "stopped". `on_event` receives
    structured events ("iteration", "score", "phase", "log") for live
    visualization. Returns a SwarmResult; it never raises for agent errors -
    those are recorded in the result's status/detail. Config errors still
    raise.
    """
    if not 0.0 <= target <= 10.0:
        raise ValueError("target must be between 0 and 10")
    if stall_limit < 1:
        raise ValueError("stall_limit must be >= 1")
    if max_tokens < 0:
        raise ValueError("max_tokens must be >= 0")
    if not 0 <= developer_count <= MAX_TEAM_DEVELOPERS:
        raise ValueError(f"developer_count must be between 0 and {MAX_TEAM_DEVELOPERS}")
    critic_spec = critic_spec or worker_spec
    tester_spec = tester_spec or worker_spec
    config = Config.load()
    cache: dict = {}
    # Roles defined on disk override the built-in prompts and may pin a
    # model; a definition that reads as prompt injection is not loaded, and
    # the person is told (see roles.py).
    definitions = load_roles(ws)
    for warning in withheld_roles(ws):
        on_progress(warning)
    tester_spec = role_model(definitions, "tester", tester_spec)
    critic_spec = role_model(definitions, "critic", critic_spec)
    worker_spec = role_model(definitions, "worker", worker_spec)
    tester_provider, tester_model, tester_cfg = _provider_for(config, cache, tester_spec)
    critic_provider, critic_model, critic_cfg = _provider_for(config, cache, critic_spec)
    worker_provider, worker_model, worker_cfg = _provider_for(config, cache, worker_spec)

    started = time.monotonic()
    scores: list[float] = []
    previous: list[str] = []
    # Retained episodes, by role, across iterations - the compressed record
    # of the last dispatch, fed forward so iteration N+1 does not rediscover
    # what N established (nac's thread/episode idea, one thread per role).
    episodes: dict[str, str] = {}
    traces: list[dict] = []
    total_tokens = 0
    role_tokens = {"tester": 0, "critic": 0, "worker": 0}
    if team_mode:
        role_tokens.update({role: 0 for role in TEAM_ROLE_PROMPTS})
    artifacts: dict = {}
    best = -1.0
    stall = 0
    iteration = 0
    status, detail = "max-iterations", "iteration cap reached"

    def emit(kind: str, data: dict) -> None:
        if on_event is not None:
            on_event(kind, data)

    def on_worker_event(kind: str, data: object) -> None:
        if kind == "tool_start":
            name = (data or {}).get("name", "?")
            line = f"  worker -> {name}"
            on_progress(line)
            emit("log", {"line": line})
        elif kind == "tool_result":
            output = (data or {}).get("output", "")
            line = f"  worker   {_summarize(output, 140)}"
            on_progress(line)
            emit("log", {"line": line})

    def progress(line: str) -> None:
        on_progress(line)
        emit("log", {"line": line})

    while max_iterations == 0 or iteration < max_iterations:
        if should_stop is not None and should_stop():
            status, detail = "stopped", "swarm stopped by the user"
            break
        if max_tokens and total_tokens >= max_tokens:
            status = "token-budget"
            detail = f"token budget of {max_tokens} exhausted ({total_tokens} used)"
            break
        iteration += 1
        emit("iteration", {"iteration": iteration})
        progress(f"--- iteration {iteration} ---")
        score = score_workspace(ws, test_command=test_command)
        scores.append(score.score)
        emit("score", {"score": score.score, "tests": score.tests, "hygiene": score.hygiene,
                       "detail": score.detail, "iteration": iteration})
        progress(f"score: {score.score:.1f}/10 "
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

        tester = critic = worker = None
        try:
            if team_mode:
                team_objective = (objective or
                                  "Improve this product into a coherent, useful, production-ready release.")
                # Product specialists establish shared intent before anyone edits.
                specialist_roles = [
                    (role, role_prompt(definitions, role, TEAM_ROLE_PROMPTS[role]))
                    for role in ("business", "user", "designer")
                ] + [(d.name, d.prompt) for d in custom_specialists(definitions)]
                for role, prompt_text in specialist_roles:
                    emit("phase", {"role": role})
                    provider, model, cfg = _provider_for(
                        config, cache, role_model(definitions, role, worker_spec))
                    specialist = _make_agent(ws, provider, model, cfg,
                                             prompt_text, read_only=True)
                    report = specialist.run_turn(_team_discovery_prompt(role, team_objective, score))
                    artifacts[role] = report
                    emit("artifact", {"artifacts": artifacts})
                    role_tokens.setdefault(role, 0)
                    role_tokens[role] += specialist.usage.total_tokens
                    total_tokens += specialist.usage.total_tokens
                    progress(f"{role}: {_summarize(report)}")
                emit("phase", {"role": "head"})
                provider, model, cfg = _provider_for(config, cache, critic_spec)
                lead = _make_agent(ws, provider, model, cfg,
                                   role_prompt(definitions, "head", TEAM_ROLE_PROMPTS["head"]),
                                   read_only=True)
                lead_out = lead.run_turn(
                    _team_head_prompt(team_objective, artifacts, score, developer_count))
                role_tokens["head"] += lead.usage.total_tokens
                total_tokens += lead.usage.total_tokens
                plan = _parse_team_plan(
                    lead_out, developer_count or MAX_TEAM_DEVELOPERS)
                artifacts["plan"] = plan
                emit("artifact", {"artifacts": artifacts})
                progress(f"head: {len(plan['tasks'])} task(s) planned")
                team_size = developer_count or max(
                    (int(t["owner"][3:]) for t in plan["tasks"]), default=1)
                allowed = {f"dev{i}" for i in range(1, team_size + 1)}
                for task in (t for t in plan["tasks"] if t["owner"] in allowed):
                    role = task["owner"]
                    role_tokens.setdefault(role, 0)
                    emit("phase", {"role": role})
                    provider, model, cfg = _provider_for(config, cache, worker_spec)
                    # A custom developer prompt is used verbatim - .format on a
                    # body with a brace in it (a JSON example, a dict literal)
                    # would raise, and the file's author never asked for
                    # placeholders. The role line is appended instead.
                    dev_definition = definitions.get("developer")
                    dev_prompt = (f"{dev_definition.prompt}\nYou are {role.upper()}."
                                  if dev_definition else
                                  TEAM_DEVELOPER_PROMPT.format(role=role.upper())) \
                        + EPISODE_CONTRACT
                    developer = _make_agent(
                        ws, provider, model, cfg,
                        dev_prompt,
                                            read_only=False, on_event=on_worker_event,
                                            worker_permissions=worker_permissions, owner=worker_owner)
                    result = developer.run_turn(_developer_prompt(task, team_objective, plan))
                    role_tokens[role] += developer.usage.total_tokens
                    total_tokens += developer.usage.total_tokens
                    artifacts.setdefault("developer_reports", []).append(
                        {"role": role, "task": task["title"], "report": result})
                    emit("artifact", {"artifacts": artifacts})
                    progress(f"{role}: {_summarize(result)}")

            tests_pass = score.tests >= 8.0 or score.test_command is None
            if skip_tester_when_tests_pass and tests_pass:
                tester_report = (
                    "No test command detected - the critic should suggest adding a "
                    "test framework so the swarm can score the test suite."
                    if score.test_command is None else
                    "The test suite passes; there is nothing to investigate. "
                    "Focus on hygiene and robustness improvements instead."
                )
                progress("tester: skipped (tests already pass)")
            else:
                emit("phase", {"role": "tester"})
                tester = _make_agent(ws, tester_provider, tester_model, tester_cfg,
                                     role_prompt(definitions, "tester", SWARM_TESTER_PROMPT)
                                     + EPISODE_CONTRACT,
                                     read_only=True)
                tester_report = tester.run_turn(_tester_prompt(score, episodes))
                episodes["tester"] = _episode_of(tester_report)
                role_tokens["tester"] += tester.usage.total_tokens
                total_tokens += tester.usage.total_tokens
                progress(f"tester: {_summarize(tester_report)}")
            if max_tokens and total_tokens >= max_tokens:
                status, detail = ("token-budget",
                                  f"token budget of {max_tokens} exhausted ({total_tokens} used)")
                break

            emit("phase", {"role": "critic"})
            critic = _make_agent(ws, critic_provider, critic_model, critic_cfg,
                                 role_prompt(definitions, "critic", SWARM_CRITIC_PROMPT),
                                 read_only=True)
            critic_out = critic.run_turn(
                _critic_prompt(score, tester_report, _diff_summary(ws), previous,
                               episodes))
            role_tokens["critic"] += critic.usage.total_tokens
            total_tokens += critic.usage.total_tokens
            parsed = _parse_critic(critic_out)
            suggestions = parsed.get("suggestions") or []
            previous.extend(item.get("title", "") for item in suggestions if item.get("title"))
            progress(f"critic: {len(suggestions)} suggestion(s)")
            if max_tokens and total_tokens >= max_tokens:
                status, detail = ("token-budget",
                                  f"token budget of {max_tokens} exhausted ({total_tokens} used)")
                break

            if suggestions or not tests_pass:
                emit("phase", {"role": "worker"})
                worker = _make_agent(ws, worker_provider, worker_model, worker_cfg,
                                     role_prompt(definitions, "worker", SWARM_WORKER_PROMPT)
                                     + EPISODE_CONTRACT,
                                     read_only=False,
                                     on_event=on_worker_event,
                                     worker_permissions=worker_permissions,
                                     owner=worker_owner)
                worker_out = worker.run_turn(_worker_prompt(parsed, score, episodes))
                episodes["worker"] = _episode_of(worker_out)
                role_tokens["worker"] += worker.usage.total_tokens
                total_tokens += worker.usage.total_tokens
                progress(f"worker: {_summarize(worker_out)}")
            else:
                worker_out = "No suggestions and tests pass - nothing to implement."
                progress(f"worker: skipped ({worker_out.lower()})")
            if max_tokens and total_tokens >= max_tokens:
                status, detail = ("token-budget",
                                  f"token budget of {max_tokens} exhausted ({total_tokens} used)")
                break
        except ProviderError as exc:
            status, detail = "error", f"agent failed: {exc}"
            break

        traces.append({
            "iteration": iteration,
            "score": score.score,
            "episodes": dict(episodes),
            "tester": tester.messages if tester is not None else [],
            "critic": critic.messages if critic is not None else [],
            "worker": worker.messages if worker is not None else [],
            "role_tokens": dict(role_tokens),
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
        "role_tokens": role_tokens,
        "seconds": round(time.monotonic() - started, 1),
        "worker_spec": worker_spec,
        "critic_spec": critic_spec,
        "tester_spec": tester_spec,
        "traces": str(trace_dir),
        "team_mode": team_mode,
        "objective": objective,
        "artifacts": artifacts,
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
        role_tokens=role_tokens,
        artifacts=artifacts,
    )


def _score_bars(scores: list[float], width: int = 40) -> list[str]:
    """ASCII bar chart of the score history, one line per iteration."""
    if not scores:
        return ["  (no iterations)"]
    lines = []
    for i, s in enumerate(scores, 1):
        filled = round(min(max(s, 0.0), 10.0) / 10.0 * width)
        bar = "█" * filled + "░" * (width - filled)
        lines.append(f"  it{i:<3} {s:>4.1f}  {bar}")
    return lines


def format_swarm_report(result: SwarmResult) -> str:
    lines = [
        f"swarm finished: {result.status}",
        f"iterations: {result.iterations}",
        f"final score: {result.final_score:.1f}/10 (target {result.target:.1f})",
        "score history: " + (" -> ".join(f"{s:.1f}" for s in result.scores) or "-"),
        "score chart:",
    ]
    lines.extend(_score_bars(result.scores))
    if result.role_tokens:
        rt = result.role_tokens
        lines.append(f"tokens by role: tester {rt.get('tester', 0)}, "
                     f"critic {rt.get('critic', 0)}, worker {rt.get('worker', 0)} "
                     f"(total {result.tokens})")
    else:
        lines.append(f"tokens: {result.tokens}")
    lines.append(f"time: {result.seconds}s")
    if result.detail:
        lines.append(f"detail: {result.detail}")
    if result.saved_to:
        lines.append(f"results saved to {result.saved_to}")
    if result.traces:
        lines.append(f"full traces in {result.traces}")
    return "\n".join(lines)
