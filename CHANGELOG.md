# Changelog

Silk Code's history, newest first, grouped by the phase that produced it.
Entries name what changed and why it mattered; pull requests are numbered
where one existed. The project began 2026-08-12 with the SRS and V0.1.

## Unreleased (PR #39) — retrieval, and six lessons from nac · 2026-08-29/30

Reading [arcee-ai/nac](https://github.com/arcee-ai/nac) and Firecrawl's
developer-index launch produced one new tool and six harness improvements.

### Added
- **`search_docs`** — retrieval of current developer documentation (READMEs,
  docs sites, issues, API specs) with the vendor replaceable: Firecrawl's
  index is the first backend (`FIRECRAWL_API_KEY` alone enables it), and any
  endpoint speaking `POST {query, limit}` → `{results: [...]}` is an equal
  one. Private by default — configure nothing and no query ever leaves the
  machine. Classified like `curl`; refused in plan mode; results go through
  the provenance scan like any other tool output.
- **Retained episodes in the swarm** — each role's final message ends with a
  structured work record (goal / done / verification evidence / blocker /
  next), kept across iterations and fed forward, so iteration N+1 stops
  rediscovering what N established. The critic is exempt (its JSON already
  is its record); episodes are bounded; traces store them.
- **Acceptance criteria in plans** — a step may carry
  `=> how you know it's done` (rendered `(done when: …)`), plans may carry a
  `Verify:` recipe; marking a step done echoes its criterion back, and
  finishing the last step surfaces the end-to-end check.
- **Compaction checkpoints on a light model** — with `"light_model"`
  configured, turns that compaction would drop are summarized into one
  checkpoint: constraints marked active/satisfied/superseded, unverified
  outcomes labeled *reported, not verified*. Inserted as an assistant
  message, folds instead of stacking, never breaks the turn on failure;
  unconfigured means exactly the old behavior.
- **`--isolated`** — the session runs in a git worktree forked from `HEAD`
  on a `silk/<stamp>` branch; the checkout (including uncommitted changes,
  which the fork does not see) stays untouched. Cleanup keeps commits and
  uncommitted work, removes only clean unused forks, and refuses outside a
  git repository rather than silently running unisolated.

### Security
- **No client replays a prompt or credential to a redirect destination.**
  Redirects are off for every client carrying a key, token, or prompt
  (provider, inference probe, docsearch, GitHub clients, sandbox backend);
  every `httpx.Client` must state its choice, test-enforced. The git auth
  proxy deliberately still follows (renamed repositories) and rests on
  httpx stripping `Authorization` cross-origin — now pinned by a test.
  Turning redirects off exposed a real bug: the GitHub client treated 3xx
  as success and returned `{}`.

## PR #38 — closing the gaps a comparison with Metis found · 2026-08-29

### Added
- **Structured project memory** — SQLite store (stdlib) with typed records
  (preference / fact / procedure / failure); repeating a note refreshes it,
  restating one supersedes it (the old record kept and marked, not
  destroyed). `memory.md` survives as a generated human-readable mirror; a
  hand-written one from an older install is imported once, dates intact.
- **Plan → Build** — the plan is a file (`.silkcode/plan.md`, markdown
  checkboxes), written by `propose_plan`, executed step-by-checked-off-step
  via `update_plan`, shown by `read_plan` and `/plan`. The **`plan`
  permission mode** is read-only and refuses (not prompts) writes and
  non-read commands; "Yes to all" and git grants deliberately do not
  override it; the one write allowed is `.silkcode/` state, because
  proposing is the point.
- **Swarm roles as files** — markdown definitions in `~/.silkcode/agents/`
  or `<project>/.silkcode/agents/` override the built-in tester / critic /
  worker / team prompts, may pin a role to a model, and new names add
  read-only specialists. A repository file cannot introduce a new writer,
  and every definition body passes the provenance scan before it may write
  a system prompt.
- **Harness adapter** — `silkcode -p` gained `--trace` (flushed JSONL event
  log with a final token/wall-time record), `--final-answer`, and `--check`,
  with an exit-code contract: 0 completed, 1 check failed, 2 provider/config
  fault — so a benchmark never counts a provider outage as a failed task.

### Fixed
- Checkpoints snapshot **bytes** instead of text — reverting an edit to a
  non-UTF-8 file (an image, a database) used to write back a mangled copy.
- Five newly-added GUI tests repaired, two of which had never executed their
  own setup (a JS-syntax-error `evaluate`, a property called as a method).

## PR #37 — the agent can look at the page · 2026-08-23/25

### Added
- **`review_url`** unified (this branch's diagnostics + main's inline
  screenshot): headless Chromium reports status, title, visible text,
  console errors, uncaught exceptions, failed requests, and horizontal
  overflow — the faults invisible in source. One missing file is one
  problem; favicons are not problems; local pages need no permission while
  outward URLs gate like `curl`; only http(s) opens (`javascript:`/`data:`/
  `file:` refused, with the quoting bypass tested).

### Fixed
- The GUI details drawer showed two panels at once (CSS specificity), and
  the phone layout scrolled sideways (`min-width: auto` on a grid item).
- CI had been executing **zero tests**: a collection error (`import
  install` under bare `pytest`) failed every job before any test ran, and
  a publish test borrowed the machine's git identity. Both fixed; the GUI
  suite went from 15 timeouts to green in a third of the time.

## GUI as a product: Swarm teams, projects, sharing · 2026-08-23 (main)

- Swarm can create, staff (elastic dev1..devN "mission control"), start,
  and publish new projects to GitHub.
- Friendly multi-project switcher: project cards with close/open, per-
  project conversation lists, a project chooser when no path is given,
  and recovery from transient fetch failures when switching or adding.
- Secondary panels collapsed into a details drawer; rich Markdown rendering
  in chat; Environment panel modernized; permission dialogs synced across
  GUI clients; development-update sharing workflow (X/LinkedIn/changelog
  drafts from the branch).
- GitHub device-flow client ID shipped; repository picker fixes; fetch
  progress; agent screenshots shown in conversations (`capture_screenshot`,
  `show_image`, headless link review).
- Cross-platform installer (`install.py`) that sets up Silk Code plus its
  Chromium together; **Playwright became a runtime dependency**.

## Reaching the machine from anywhere · 2026-08-23 (main)

- **Phone-first**: run models on the laptop, drive the agent from a phone —
  `silkcode inference host/link/ping/discover`, connection monitor, QR
  pairing.
- **Tailscale integration** — `tailnet.py` reads Tailscale's state and gives
  situation-specific advice; setup page treats it as part of installing.
  Silk Code never runs `tailscale up` itself: joining a network is the
  user's call.
- GUI access token persists across restarts; DNS-rebinding and CSRF guards;
  a daemon reachable beyond loopback requires its token.
- **Versioning that moves**: setuptools_scm derives the version from git
  tags (`0.2.1.devN+g<sha>` between releases) so `pip install -U` has
  something to compare; release workflow refuses a tag that does not move
  the version and smoke-tests the wheel; `silkcode version` reports the
  build identity, commit, and how to update.

## The trust boundary · 2026-08-19/23 (PRs #24, #28)

- **A file cannot authorize a push** (`provenance.py`): every tool result is
  scanned for text written to steer an agent; a turn that consumed such
  content is *tainted*, and outward actions (push, merge) then stop and ask
  even under standing grants. Detection is deliberately narrow — five
  patterns tuned against this repository's own 13 false positives — because
  a warning that fires on a normal README teaches people to ignore it.
- **Classify the command that will run**: permission decisions moved from
  regex-over-text to shlex-parsed argv (`"git" push` and `gi""t push` are
  `git push`), failing closed on unparseable input; repository instruction
  files (`SILKCODE.md`, memory, skills) are read as untrusted and withheld
  from the system prompt when they read as injection.
- Push weighted by consequence; porcelain no longer printed in the diff
  panel; sessions scoped to their project.

## Quality machinery · 2026-08-14/16 (main + PRs #16, #25–27)

- **Multi-agent improvement swarm** (tester / critic / worker) with 0–10
  scoring, stall detection, token budgets, per-role token accounting, GUI
  visualization, and permission prompts routed to the user ("Yes to all").
- **Benchmarks mined from the repository's own history**: private tasks
  rebuilt from merged changes, kept only if the tests fail before and pass
  after the original implementation; test files protected at benchmark
  time. Plus an A/B protocol isolating the harness's contribution.
- Full test-and-security pass: 7 vulnerabilities and 9 bugs fixed, suite
  grown 267 → 500 tests (#16); documented installs verified against
  reality (#25); wheel smoke-tested before release.
- Self-update (`silkcode update`): pull from git and hot-apply via daemon
  re-exec, restoring the active session. `/reload` for config hot-reload.
- Advisory per-workspace lock (one writer at a time), stale-base optimistic
  concurrency on the file tools, provider retry with exponential backoff,
  bounded filesystem walks, multiple GUI daemons per machine.

## Foundation: V0.1 · 2026-08-12/13 (PRs #1–#13)

- **SRS first, code second**: SRS.md, then the V0.1 implementation — the
  agent loop (streaming, tool dispatch, permissions ask/edit/agent,
  checkpoints/revert), CLI REPL, and the local web GUI, model-agnostic
  across DeepSeek, Qwen, Kimi, GLM, MiniMax, OpenRouter, Ollama, vLLM,
  LM Studio, plus Cloudflare Workers AI and AI Gateway.
- Repository map, skills, project instructions, memory, symbol index, MCP
  client, auto model router, context compaction, benchmark engine,
  test-framework detection, one-shot mode.
- GitHub integration: device-flow sign-in, PR/issue tools, push/pull/merge,
  Agent Tasks; remote sandbox execution (self-hosted server + Cloudflare
  Worker); remote workspaces that live entirely in the sandbox.
- Landing page, MIT license, release wheels, co-author attribution on
  agent-made commits.
