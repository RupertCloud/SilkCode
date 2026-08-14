# Silk Code — Next Steps

A working roadmap for the repo. Each item is grounded in the [SRS](SRS.md) and
the current codebase. Status legend: `[x]` done · `[~]` in progress · `[ ]` planned.

The full spec lives in [SRS.md](SRS.md); this file is the *priority order* —
what to build next and why.

---

## Priority 1 — Projects (the workspace experience)

Current state: one session = one workspace. GitHub repos clone into
`~/.silkcode/projects/<owner>-<repo>`, recent projects are remembered, and the
GUI `＋` modal + CLI `/project` both work. The plumbing is fine; management and
switching are the gaps (SRS FR-GUI-001, section 10; section 85).

- [ ] **Switch project within a session** (highest value)
      A Project dropdown in the GUI header that re-targets the current session's
      workspace without losing the conversation (SRS line 290 mockup, section 2160:
      "switch model without changing project" — and vice versa). Requires
      persisting `cwd` changes on session save (server: `new_session` /
      `_resolve_workspace`, GUI: `#project-modal` → header control).
- [ ] **Project management UI** — list cloned repos + local projects under
      `~/.silkcode/projects/`, show last-opened and disk usage, allow delete /
      re-clone (`project.py` already has the primitives; add a `/api/projects/manage`
      route and a modal).
- [ ] **Clone options** — branch selection, SSH vs HTTPS, "fresh clone" vs "update"
      (`clone_github_repo` is currently always `main` over HTTPS).
- [ ] **Per-project settings** — remember test command, model, and permission mode
      per project (SRS 51 "Project-Specific Model Ranking" is the long-term version;
      the swarm already takes a test command, so persisting it per project makes
      🐝 one click).
- [ ] **Native folder picker** — the local tab is a text input today; use
      `<input type="file" webkitdirectory>` or an OS dialog.
- [ ] **Repository explorer depth** (SRS FR-GUI-002) — create / rename / delete /
      move / search in the file tree; today the tree is read-only navigation.

## Priority 2 — Swarm hardening

Current state: tester/critic/worker loop with 0–10 scoring, token budgets,
tester-skip efficiency, GUI visualization, and (new) worker permission prompts
with **Yes to all** (SRS 39 "Multiple Agents" is the reference).

- [~] **Commit the hygiene self-match fix** — the swarm's own scanner flags its
      own source (`TODO` in `TODO_RE`, `"breakpoint("` in `DEBUG_MARKERS`), so
      dogfooding Silk Code on itself can never score 10/10. The fix (split marker
      literals in `silkcode/swarm.py` + `tests/test_swarm.py`) is already in the
      working tree, uncommitted.
- [ ] **Worker cleanup discipline** — workers left `tmp_repro.py` behind and hit
      the per-turn max-steps cap mid-task; consider requiring cleanup of temp
      files and raising the step cap for swarm workers.
- [ ] **No test framework detected** → critic already suggests adding one; make
      the swarm prompt for a test command up front instead of burning an iteration.
- [ ] **CLI swarm permissions** — the CLI `silkcode swarm` still runs the worker
      in auto-approve agent mode; add a `--mode ask|edit|agent` so the terminal
      swarm can prompt too (the GUI already shares the session's permission
      manager via `worker_permissions`).

## Priority 3 — SRS V0.2/V0.3 gaps still open

Much of V0.2 is done (providers, repo maps, skills, memory, MCP, commits,
checkpoints, benchmarking, swarm). Remaining gaps:

- [ ] **Model Auto Router** — `config.py` only has the basic "first available"
      router (section 17 / 554); a real router (task-type → best model) is the
      headline V0.2 item still missing.
- [ ] **Tree-sitter symbol indexing** — `tools/symbols.py` is regex-based; the
      SRS (22/23) wants real syntax trees for better repo maps and search.
- [ ] **Planning mode / Code review mode / Debugging mode** (SRS 36, 37, 38) —
      the agent loop has the building blocks; these are mode presets on top.
- [ ] **Usage dashboard & cost limits** (SRS 48, 49) — usage is tracked and shown
      in the header; a real dashboard + per-session/per-day cost caps are not.
- [ ] **V0.3: enterprise & team** — audit logs, team settings, shared skills,
      organization model gateway, GitLab integration (SRS 80).

## How to verify changes

```bash
cd /Users/ridelink/Documents/GitHub/SilkCode
.venv/bin/python -m pytest tests/ -q        # full suite (currently 198 passed, 1 skipped)
.venv/bin/python -m pytest tests/test_gui.py tests/test_swarm.py tests/test_permissions.py -q
```

GUI runs from this repo's venv (the daemon on :8377 was started from the *old*
checkout at `/Users/ridelink/SilkCode`):

```bash
kill $(lsof -tnP -iTCP:8377 -sTCP:LISTEN)
/Users/ridelink/Documents/GitHub/SilkCode/.venv/bin/silkcode gui
curl -s http://127.0.0.1:8377/ | grep -c swarm-btn   # expect 1
```
