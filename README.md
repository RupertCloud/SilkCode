# Silk Code

**Homepage: [silkcode.web.app](https://silkcode.web.app)** · MIT licensed

**An open, model-agnostic AI coding harness.** Use DeepSeek, Qwen, Kimi, OpenRouter, any
OpenAI-compatible endpoint, or local models (Ollama, vLLM, LM Studio) to understand a
repository, plan changes, write code, run commands and tests, and review diffs — from a
CLI or a local GUI.

> **The coding environment belongs to the developer. The AI model is replaceable.**

The full specification is in [SRS.md](SRS.md). The prioritized roadmap — what
to build next and why — is in [NEXT_STEPS.md](NEXT_STEPS.md), and the design for
a fully hosted Silk Code is in [docs/CLOUD.md](docs/CLOUD.md). This implementation
is V0.1: the Python agent runtime, the CLI, and the GUI (a local web app served
by the Silk Code daemon — designed to be wrapped in Tauri later, per SRS
sections 67-68).

## Install

Without building from source — grab the wheel from the
[latest release](https://github.com/RupertCloud/SilkCode/releases/latest):

```bash
pip install https://github.com/RupertCloud/SilkCode/releases/download/v0.1.0/silkcode-0.1.0-py3-none-any.whl
# or isolated, with pipx:
pipx install https://github.com/RupertCloud/SilkCode/releases/download/v0.1.0/silkcode-0.1.0-py3-none-any.whl
```

Or from source:

```bash
pip install -e .          # from a clone of this repository
```

Requires Python 3.10+. The only runtime dependency is `httpx`.

## Quick start

```bash
# Cloud model (DeepSeek):
export DEEPSEEK_API_KEY=sk-...
silkcode ~/my-project                      # CLI REPL
silkcode gui ~/my-project                  # GUI at http://127.0.0.1:8377

# Local model (Ollama):
silkcode models pull qwen2.5-coder         # pulls into a running Ollama server
silkcode --model ollama/qwen2.5-coder ~/my-project
```

Then just ask:

```text
silk> Build a small Flask API with tests, and make sure the tests pass.
```

The agent reads and edits files, runs commands and tests, and reports back. File writes
and risky commands ask for your approval first (see *Permissions* below).

## Models: cloud, local, and onboarding your own

Silk Code ships with built-in providers: `deepseek`, `qwen`, `kimi`, `glm`, `minimax`,
`cloudflare` (Workers AI), `openrouter`, `ollama`, `vllm`, `lmstudio`. API keys are read
from environment variables (`DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `MOONSHOT_API_KEY`,
`GLM_API_KEY`, `MINIMAX_API_KEY`, `CLOUDFLARE_API_TOKEN`, `OPENROUTER_API_KEY`).

**Cloudflare Workers AI** (models on Cloudflare's edge GPUs) needs your account id once:

```bash
export CLOUDFLARE_API_TOKEN=...        # token with Workers AI permission
silkcode models add cloudflare --account-id <your-account-id>
silkcode --model cloudflare                                    # qwen2.5-coder-32b default
silkcode --model "cloudflare/@cf/meta/llama-3.3-70b-instruct"  # any Workers AI model
```

**Cloudflare AI Gateway** (caching/analytics proxy in front of any provider) is just an
OpenAI-compatible endpoint:

```bash
silkcode models add gateway \
  --base-url https://gateway.ai.cloudflare.com/v1/<account-id>/<gateway>/compat \
  --api-key-env CF_AIG_TOKEN --model <model>
```

```bash
silkcode models                            # list providers, keys, local models
silkcode models pull qwen2.5-coder         # pull a model into Ollama
silkcode models default ollama/qwen2.5-coder

# Onboard any OpenAI-compatible endpoint (private server, enterprise gateway, ...):
silkcode models add myserver \
  --base-url https://ai.company.com/v1 \
  --model my-coder-model \
  --api-key-env MY_API_KEY
silkcode --model myserver
```

Model specs are `provider` (uses its default model) or `provider/model`, e.g.
`deepseek`, `ollama/qwen2.5-coder:32b`, `openrouter/deepseek/deepseek-chat`. Switch
mid-session with `/model <spec>` in the CLI or the model selector in the GUI.
Providers can also be onboarded from the GUI (**+ Add model**).

`--model auto` picks the first available model: a running local server first
(Ollama, preferring coder models), then cloud providers with an API key configured.
The order is configurable via `auto_order` in the config file.

Configuration lives at `~/.silkcode/config.json` (override the directory with
`$SILKCODE_HOME`).

Per-provider network options in `config.json` (for flaky networks or slow first
tokens, e.g. DeepSeek):

```json
{
  "providers": {
    "deepseek": {
      "timeout": 600,
      "retries": 3,
      "retry_delay": 1.5
    }
  }
}
```

`timeout` is the per-request (connect + read) limit in seconds (default 180).
- `retries` (default 2) — how many times a *transient* failure is retried before
  giving up. Transient means a network timeout/connection drop (`Operation timed
  out`, `Connection reset`) or a `429`/`5xx` provider response. A client `4xx`
  (bad key, bad request) is a problem with the request and is never retried.
- `retry_delay` (default 1.0s) — the base backoff between attempts; it doubles
  each retry (1s, 2s, 4s, …). For streaming, a request is only re-sent while no
  response has arrived yet — once tokens are streaming out, a mid-stream drop is
  surfaced as an error rather than replaying duplicate output.

## CLI

```bash
silkcode [path] [--model M] [--mode ask|edit|agent]   # interactive REPL
silkcode -p "add input validation to the API" .       # one-shot, non-interactive
silkcode gui [path] [--port N]                        # local GUI
silkcode review [path]                                # AI review of uncommitted changes
silkcode models [add|pull|default]                    # provider/model management
silkcode swarm [path] [--model M] [...]               # multi-agent improvement loop
silkcode update [--branch B]                          # pull updates, hot-apply them
silkcode sessions                                     # list saved sessions
silkcode resume <id>                                  # continue a session (GUI or CLI)
silkcode test [path] [--command CMD]                  # run the project's tests (auto-detected)
silkcode mcp [add|remove]                             # manage MCP servers
silkcode connect github                               # set up GitHub access
silkcode config                                       # show configuration
```

REPL commands: `/model`, `/models`, `/mode`, `/project`, `/diff`, `/usage`, `/revert`,
`/clear`, `/sessions`, `/help`, `/exit`.

**Open a different project mid-session:** `/project` prompts you to pick a project for
the session — either a GitHub repository you have access to (cloned for you into
`~/.silkcode/projects/`) or a directory you type in. Passing a spec directly skips the
prompt: `/project /path/to/other` or `/project github:acme/widget`. The session's files,
git status/diff, and commands then refer to that project (SRS: new sessions ask for a
project).

## GUI

`silkcode gui` starts the Silk Code daemon and opens a browser app with the project
explorer, AI conversation, agent activity timeline, git diff / file viewer, model and
mode selectors, provider onboarding, checkpoint revert, a stop button for running
turns, live permission prompts, a **🐝 Swarm** button (multi-agent improvement loop,
see below), and a **↻ Update** button (self-update, see below).

**Multiple sessions:** open as many conversations as you like (the ＋ button) and
switch between them with the session picker — each has its own agent, model choice,
workspace (project), transcript, and checkpoints, and turns can run concurrently
(⏳ marks a busy session; permission prompts from any session reach you wherever you
are). The ＋ button first asks which **project** to run the new session on: pick a
GitHub repository (cloned for you into `~/.silkcode/projects/`) or type a local
directory, so different sessions can work on different codebases side by side.
Sessions are saved to the same store as the CLI — resume any of them from the picker
or with `silkcode resume <id>` (SRS section 47).

**Multiple GUI instances on one machine:** run one daemon per project and address —
each instance gets its own port (and optionally its own bind address):

```bash
silkcode gui ~/project-a                          # http://127.0.0.1:8377
silkcode gui ~/project-b --port 8378              # http://127.0.0.1:8378
silkcode gui ~/project-c --host 0.0.0.0 --port 8379   # reachable from the LAN
silkcode gui --port 0 ~/scratch                   # OS-assigned port (printed on start)
```

Instances share the session store but session ids are allocated atomically across
processes, so two daemons never hand out the same id or overwrite each other's
session files; each session records the instance (`host:port`) that created it, shown
by `silkcode sessions`. Opening the *same* project in two instances is allowed — the
per-workspace lock makes the second one read-only until the first closes.

## Improvement swarm

`silkcode swarm` runs a multi-agent improvement loop against a project until it
scores 10/10, the score stalls, or a cap is reached. Each iteration scores the
workspace 0–10 — test suite up to 8, code hygiene up to 2 (no TODO/FIXME markers,
no debug leftovers) — then three agents work on it:

* **tester** (read-only) investigates test failures;
* **critic** (read-only) returns prioritized suggestions as JSON;
* **worker** implements the suggestions and re-runs the tests.

```bash
silkcode swarm ~/my-project --model deepseek              # target 10/10 by default
silkcode swarm . --model M --critic-model M2 --tester-model M3
silkcode swarm . --target 8 --max-iterations 5 --stall-limit 2
silkcode swarm . --max-tokens 200000 --no-skip-tester --test-command "pytest tests/"
```

Stops when the target is reached, after `--stall-limit` flat rounds, at
`--max-iterations`, or when the `--max-tokens` budget is spent. When the test
suite is already green, the read-only tester is skipped (2 agents per round
instead of 3) unless you pass `--no-skip-tester`. Scores and per-iteration
traces are saved under `~/.silkcode/swarm/`.

In the GUI, the **🐝 Swarm** button runs the same loop with live progress, a
score-history chart, a pipeline phase indicator, and per-role token stats. The
worker asks you before modifying files or running commands — pick **Yes to all**
on a permission prompt to let it run unattended for the rest of the session.

## Self-update

### Resyncing a branch that moved

A session can hold a workspace for hours while the branch changes underneath
it — a teammate pushes, the base branch advances, a pull request is merged.

```
silkcode sync                # fetch and report; changes nothing
silkcode sync --apply        # fast-forward, or merge if both sides moved
```

It exits non-zero when the branch needs attention, so it composes:
`silkcode sync || silkcode sync --apply`.

The case it exists for is the one `git status` describes misleadingly. After a
squash merge your branch is "ahead" — those commits genuinely are not on the
base — but the *work* is already there under the squashed commit. Merging
produces a confusing duplicate; the branch should restart from the base. Sync
tells the two apart by comparing trees rather than commits, and says so:

```
branch feature · tracking origin/feature · 8 ahead of origin/main
· already merged into origin/main
suggested: restart — this branch's work is already in origin/main under a
different commit (a squash merge) …
```

Restarting moves the branch pointer, so it needs `--restart` explicitly.
Nothing here discards work: uncommitted changes stop it, and a conflicted
merge is reported rather than guessed at. The agent has the same thing as the
`git_sync` tool, so it can check before committing on a stale base.

`silkcode update` fast-forwards the installed checkout to the latest code from
its git remote (refuses on a dirty tree or non-fast-forward; `--force` overrides
that, `--install` also re-runs `pip install -e .` for new dependencies):

```bash
silkcode update               # pull the latest code
silkcode update --branch B    # update to a specific branch
```

A running GUI daemon watches the checkout's HEAD and, once new code lands (and
it is idle — no session or swarm running), re-execs itself with the same
arguments so the update goes live without a manual restart. Sessions are
persisted on disk and survive the restart; the browser reloads automatically.
This works for git-checkout installs (a clone or `pip install -e .`); wheel
installs carry no git metadata, so update those with `pip install -U silkcode`.
The GUI's **↻ Update** header button does the same thing.

## Project instructions, memory, and skills

- **`SILKCODE.md`** at the repository root is loaded automatically into the agent's
  context — put your project rules there ("Use TypeScript", "run tests after auth
  changes", ...).
- **Project memory** lives in `.silkcode/memory.md`: the agent records durable notes
  with its `remember` tool (checkpointed and revertable like any write); inspect it
  with `/memory` or edit the file directly.
- **Skills** are markdown files in `~/.silkcode/skills/` (user) or
  `<project>/.silkcode/skills/` (project overrides user). Optional frontmatter gives a
  `name:` and `description:`; the agent sees the list and loads one with `use_skill`
  when relevant. List them with `/skills`.

## GitHub

Connect once, then the agent can work with your GitHub project. The preferred way is
**Sign in with GitHub** — install the Silk Code app, approve in the browser, no tokens:

```bash
silkcode connect github            # shows a code, opens github.com/login/device
```

(or click **Sign in with GitHub** on the GUI's authorization page). Tokens issued this
way are short-lived, scoped to the repos where the app is installed, and refreshed
automatically. This requires the Silk Code GitHub App's client id — a one-time
maintainer registration, see [docs/GITHUB_APP.md](docs/GITHUB_APP.md); until then, or
if you prefer, a personal access token works exactly as before:

```bash
export GITHUB_TOKEN=github_pat_...  # fine-grained token: Contents/PRs/Issues rw
silkcode connect github             # verifies the token and detects owner/repo
```

The repository is auto-detected from the workspace's `origin` remote. Agent tools:
`github_create_pr` (draft by default, approval-gated), `github_merge_pr`,
`github_list_prs`, `github_list_issues`, `github_get_issue`, plus `git_push` and
`git_pull` (HTTPS remotes authenticate with your token; SSH remotes use your own
keys). GitHub Enterprise: set `github.api_url` in the config. Prefer MCP?
`silkcode connect github` prints the equivalent `silkcode mcp add` command.

### Attribution

Commits the **agent** makes register Silk Code as co-author, with provenance you can
query later:

```text
Add input validation

Co-Authored-By: Silk Code <agent@silkcode.dev>
X-Silk-Model: deepseek/deepseek-chat
X-Silk-Session: 42
```

You remain the commit author; the trailers record that (and which model) did the
work — `git log --grep="X-Silk-Model"` finds every agent commit. PRs the agent opens
get a matching footer. Commits **you** make (or trigger directly, like `/push`) stay
clean. Opt out with `"attribution": false` in the config.

### Pushing your work

After a session (or any turn), push manually — `/push` in the CLI, the **⇧ Push**
button in the GUI — or turn on **auto-push** and Silk Code pushes any unpushed
commits after each turn automatically:

```bash
silkcode --auto-push ~/my-project        # CLI (implies the push grant)
silkcode gui --auto-push ~/my-project    # GUI; also a checkbox on the GitHub page
```

In the CLI, `/autopush on|off` toggles it mid-session. Auto-push only fires when the
branch actually has commits the remote doesn't.

### Authorization

The GUI has a **GitHub** authorization page: paste a token (verified before it is
stored), see connection status, and pre-authorize `pull` / `commit` / `push` /
`merge` for the session so those operations run without per-action prompts.
Everything not granted keeps its normal approval prompt (push and merge are
high-risk by default), and grants reset when the session ends. From the CLI:

```bash
silkcode --allow pull,commit .            # session grants in the REPL
silkcode gui --allow pull,commit,push .   # or in the GUI
```

### Copilot cloud agents (Agent Tasks API)

With a Copilot Business/Enterprise token, the agent can also delegate work to
GitHub's cloud agents (API version 2026-03-10): `github_agent_task_start` (prompt,
optional model/base branch/auto-PR), `github_agent_tasks`, `github_agent_task_get`
(task state and sessions).

## MCP

Silk Code is an MCP client: connect any MCP server and its tools become available to
the model as `mcp__<server>__<tool>` (approval-gated like medium-risk commands).

```bash
silkcode mcp add fetch --command "uvx mcp-server-fetch"
silkcode mcp                       # list servers and their tools
```

## Remote sandboxes

Run the agent's commands and tests in a disposable cloud container instead of on your
machine (file edits stay local; the workspace syncs up before each command):

```bash
# Self-hosted, on any machine/VM that should execute commands:
silkcode sandbox serve --token <secret>
silkcode sandbox connect http://<host>:8390 --token <secret>

# Or Cloudflare: deploy sandbox/cloudflare-worker (see its README), then
silkcode sandbox connect https://silkcode-sandbox.<you>.workers.dev

silkcode --sandbox ~/my-project           # CLI with remote execution
silkcode gui --sandbox ~/my-project       # GUI with remote execution
silkcode sandbox                          # status / health check
```

Both implement the same documented Silk Sandbox Protocol v1 (`/health`, `/sync`,
`/exec`, bearer-token auth). Remote outputs are labeled `[sandbox]` in the agent's
tool results. Note: artifacts created remotely are not synced back.

## Benchmarking

`silkcode benchmark` runs real end-to-end coding tasks (create, fix, extend, refactor —
each verified by executing code, with anti-cheating checks) through the full agent loop:

```bash
silkcode benchmark -m deepseek -m ollama/qwen2.5-coder   # compare models
silkcode benchmark --ab                                  # paired-condition protocol:
                                                         # bare agent vs full harness context
silkcode benchmark --list                                # available tasks
```

Reports solved count, tokens, and wall-clock per model; saves JSON results and full
per-task traces under `~/.silkcode/benchmarks/` for review and provenance. The `--ab`
mode follows the paired-comparison design used by harness-evaluation protocols
(e.g. Nimbalyst's): same task, model, prompts, and permissions in both conditions, so
the delta isolates the harness's contribution.

### Benchmarks from your own history

Public benchmarks are contaminated — every model has trained on them — and generic.
`--from-history` builds a **private** task set out of your repository's own merged work:

```bash
silkcode benchmark --from-history            # mine this repo, then benchmark on it
silkcode benchmark --from-history --mine-only --limit 10   # just build the task set
silkcode benchmark --tasks-file ~/.silkcode/benchmarks/tasks-123.json -m deepseek
```

For each change that touched both tests and code, Silk Code reconstructs the task the
developer originally faced — the base commit plus that change's tests, with the commit
message as the instruction — and keeps it only if **the tests fail on the base state and
pass once the original implementation is applied**. That gate proves the tests really
capture the change and that the task is solvable here. At benchmark time the test files
are protected, so weakening or deleting them fails the task rather than passing it.

Tasks are tiered by size (`easy` ≥20 changed lines/1 file, `medium` ≥50/2, `hard` ≥100/3;
filter with `--difficulty`) and saved as JSON so a set can be replayed against new models
later. Because the mined snapshot is the source of truth, tests run with the snapshot on
`PYTHONPATH` — otherwise an editable install would leak your current code into the "before"
state and every task would look already-solved.

## Environment: keys, usage, sessions

The GUI's **⚙ Environment** page (and `silkcode env` in the terminal) answers what this
install is holding, spending, and running:

- **Credentials** — every provider, whether its key is set, and where the value in use
  came from (an environment variable shadowing a stored key is shown as such). Keys are
  displayed masked (`…4242`) and never sent to the browser in full; you can store or
  clear a key per provider from the page.
- **Token usage** — totals and a per-model breakdown across every saved session (CLI,
  GUI and swarm alike), plus the last 7 days.
- **Sessions** — how many are open in this daemon, which are running, on which project
  and model, and their live token spend.

```bash
silkcode env                     # the same view in the terminal
silkcode env --set deepseek      # store a key (read from $SILKCODE_KEY or prompted)
silkcode env --clear deepseek    # remove a stored key
```

## Context management

The full conversation is sent to the model each turn. When the estimated size
approaches the model's context window (default budget 100K tokens; set
`context_tokens` per provider in the config), Silk Code compacts automatically:
old tool outputs are truncated first, then the oldest turns are dropped at turn
boundaries — the current turn and recent results are never touched, and the model is
told what was trimmed. Check `/usage` for the live context estimate.

## Permissions and safety

Commands are risk-classified (SRS section 30): read-only commands run automatically;
medium-risk commands (installs, branch switches) need approval unless you are in
`agent` mode; high-risk commands (`rm -rf`, `git push`, destructive checkouts,
`sudo`, ...) always require explicit approval, in every mode.

Modes (SRS section 31): `ask` (approve everything), `edit` (file edits are free,
commands ask), `agent` (autonomous except high-risk).

The GUI's permission prompt also offers **Always** (approve this kind of request
for the session — all writes, or one command) and **Yes to all** (approve every
request for the rest of the session, including high-risk commands, with no
further prompts). The swarm's worker agent shares the same prompt flow, so it
asks the user the same way instead of auto-approving.

Before every automated file modification Silk Code snapshots the file; `/revert` (CLI)
or **Revert** (GUI) restores the last turn's changes (SRS section 28).

## Architecture

```
silkcode/
├── providers/       ModelProvider abstraction: OpenAI-compatible + Ollama
├── repomap.py       compact repository map injected into the model's context
├── context.py       context assembly: repo map + SILKCODE.md + memory + skills
├── skills.py        reusable skills loaded from markdown files
├── memory.py        project memory (.silkcode/memory.md)
├── mcp.py           MCP client (stdio): external tool servers for the agent
├── github.py        GitHub integration: PRs and issues via $GITHUB_TOKEN
├── execbackend.py   execution backends: local subprocesses or remote sandbox
├── sandbox_server.py  reference Silk Sandbox Protocol server (self-hosted)
├── tools/           read/write/edit, glob/grep, run_command, run_tests, git status/diff/log/commit
├── agent/           the agent loop: streaming, tool dispatch, permissions
├── swarm.py         multi-agent improvement loop (tester/critic/worker, 0-10 scoring)
├── update.py        self-update: pull from git, hot-apply via GUI daemon restart
├── permissions.py   risk classification + ask/edit/agent modes + "yes to all"
├── checkpoints.py   snapshot-before-modify, revert per turn
├── sessions.py      persistence shared by CLI and GUI
├── config.py        provider registry and model resolution
├── cli/             REPL + subcommands
└── gui/             local daemon (HTTP + SSE) + browser app
```

The provider layer is deliberately thin and swappable; the agent runtime is
self-contained and can be replaced or supplemented (e.g. by Deep Agents) behind the
same interfaces, per SRS section 8.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite includes an end-to-end test that drives the real agent and provider
stack against a scripted OpenAI-compatible server — no API keys needed.
