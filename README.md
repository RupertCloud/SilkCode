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

```bash
pip install git+https://github.com/RupertCloud/SilkCode
# or isolated, with pipx:
pipx install git+https://github.com/RupertCloud/SilkCode
```

Or from a clone, for working on Silk Code itself:

```bash
pip install -e .
```

Once a tagged release is published, the wheel from the
[latest release](https://github.com/RupertCloud/SilkCode/releases/latest) installs the
same way — `pip install <url-of-the-.whl>` — without needing git on the machine.

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
A server linked with `silkcode inference link` goes to the front of that order, so
`auto` reaches for your laptop when you are on its network and falls through to the
cloud when you are not. The order is configurable via `auto_order` in the config file.

To run the models on a *different* machine than the one you are typing on — a phone
driving a laptop's GPU — see
[Run the model on your laptop, drive it from your phone](#run-the-model-on-your-laptop-drive-it-from-your-phone).

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

## Run the model on your laptop, drive it from your phone

The phone is a good place to *drive* a coding agent and a poor place to *run* the
model: the laptop already has the RAM, the GPU and the weights on disk. Silk Code
links the two, so the agent runs where you are typing and the tokens are generated
where the hardware is.

Both machines need to be on the same network — the same Wi-Fi, or a private mesh
like Tailscale (that address works here too).

**On the laptop** (the one with the model), start your server and ask Silk Code
what the phone needs:

```bash
ollama serve                    # or LM Studio / vLLM / llama.cpp
silkcode inference host
```

Local model servers ship bound to `127.0.0.1` — the right default, and the exact
reason a phone on the same Wi-Fi gets *connection refused*. `inference host`
checks whether each running server is actually reachable from the network, and
prints the one command that opens it if not:

```
This machine is reachable at: 192.168.1.20

  port 11434 (ollama) is running, but only on 127.0.0.1 - nothing else can reach it
    restart it listening on every interface:  OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Run that, run `silkcode inference host` again, and it hands you the address:

```
  http://192.168.1.20:11434    ollama    4 models - reachable from the network

On the phone, run:
  silkcode inference link http://192.168.1.20:11434
```

**On the phone** (Termux, or any second machine), find it and link it:

```bash
silkcode inference discover                          # sweep this network
silkcode inference link http://192.168.1.20:11434    # or just: 192.168.1.20
silkcode                                             # that laptop now serves the session
```

`link` probes the address before saving it, picks a sensible default model (a coder
model if there is one, never an embedding model), and stores it as an ordinary
provider named `laptop` — so `--model laptop`, `/model laptop/<model>`, the GUI's
model selector and `silkcode models` all pick it up with no further setup.

It also puts the laptop at the front of the `auto` router, which means **you can
leave the house without reconfiguring anything**: `--model auto` tries the laptop
first and falls through to your cloud providers when it does not answer.

**The direct-to-provider path is untouched.** Linking a laptop adds a provider, it
does not replace any — Silk Code still talks straight to DeepSeek, Kimi, GLM,
MiniMax, Qwen, OpenRouter and Cloudflare exactly as before, and every
`inference` command prints which of them are ready to take over:

```
Direct to a cloud provider: deepseek
  available at any time:  silkcode --model deepseek
  not set up: qwen, kimi, glm, minimax, openrouter, cloudflare   (silkcode models shows what each needs)
```

So you can mix freely — a local model on the laptop for the fast, private,
free-to-run turns, and a frontier cloud model for the hard ones:

```bash
silkcode --model laptop/qwen2.5-coder:7b    # the laptop's GPU
silkcode --model deepseek                   # straight to DeepSeek, as always
silkcode --model kimi                        # straight to Kimi
silkcode --model auto                        # laptop if it answers, cloud if it does not
```

Or switch mid-session with `/model <spec>`, without restarting.

```bash
silkcode inference                 # what is linked, and is it up right now?
silkcode inference ping            # round-trip latency to the server
silkcode inference ping --chat     # ... and time a real turn through the model
silkcode inference unlink          # go back to local/cloud models
```

`ping` and `ping --chat` answer two different questions, and the gap between them
is where the surprises live: a laptop answers a model listing in a millisecond and
can still take half a minute to produce a first token while it pages a 30B model in
from disk. `--chat` sends a real prompt and times the whole round trip.

Useful flags:

| Flag | What it does |
| --- | --- |
| `link --name <n>` | save it as something other than `laptop` (link several machines) |
| `link --model <m>` | choose the default model instead of letting Silk Code pick |
| `link --token-env VAR` | send a bearer token, for a server behind an authenticating proxy |
| `link --timeout 600` | raise the request timeout for a big model that loads cold |
| `link --force` | save an address that is not answering yet (the laptop is asleep) |
| `discover --host H` | check one host instead of sweeping the subnet |
| `discover --port P` | try an extra port beyond the ones Silk Code knows |

Discovery sweeps the current `/24` for the ports these servers use — 11434
(Ollama), 1234 (LM Studio), 8000 (vLLM), 8080 (llama.cpp), 5001 (KoboldCpp) — and
probes whatever answers.

> Anything you expose this way is unauthenticated unless you put a proxy in front
> of it. Keep it to networks you trust, or use a private mesh (Tailscale/WireGuard)
> rather than forwarding a port on your router.

If you would rather keep Silk Code itself on the laptop and just *drive* it from the
phone, that is the mirror image of this section — see
[Run the agent on your laptop, open it on your phone](#run-the-agent-on-your-laptop-open-it-on-your-phone).

## Run the agent on your laptop, open it on your phone

The mirror image of the section above, and worth keeping straight:

| | Where Silk Code runs | Where the model runs | Where you type |
| --- | --- | --- | --- |
| [`silkcode inference`](#run-the-model-on-your-laptop-drive-it-from-your-phone) | phone | laptop | phone |
| `silkcode gui --host 0.0.0.0` | laptop | laptop or cloud | phone browser |

Here the agent runs on your laptop —
where your files, credentials, build tools and environment variables already
are — and you drive it from a phone browser. Nothing is uploaded anywhere and
no third party sits in the path.

```bash
# on the laptop
silkcode gui ~/my-project --host 0.0.0.0
```

Because that is reachable beyond the machine, Silk Code generates an access
token and prints the addresses another device can open, plus a QR to point a
camera at instead of retyping a 32-character token:

```
This daemon is reachable beyond this machine, so it requires an access token.

Silk Code GUI: http://localhost:8377/?token=JkdF8ZOnXtACibWY83liacawnDmg3u3G

Reachable from another device:
  http://100.101.102.103:8377/?token=JkdF8ZOnXtACibWY83liacawnDmg3u3G
      Tailscale - works from anywhere on your tailnet
  http://192.168.1.20:8377/?token=JkdF8ZOnXtACibWY83liacawnDmg3u3G
      LAN - same network only

Point a phone camera at this to open it (Tailscale):

    █████████████████████████
    ██ ▄▄▄▄▄ █▄▀█▀▄█ ▄▄▄▄▄ ██
    ██ █   █ █ ▄ █ █ █   █ ██
    ██ █▄▄▄█ █ ▀▄▀▄█ █▄▄▄█ ██
    ██▄▄▄▄▄▄▄█▄█ █▄█▄▄▄▄▄▄▄██
    …
```

Scan it and the session opens on the phone: same conversation, same files,
same git diff, with the agent still running on the laptop.

**Pairing another device later.** That banner prints once, at startup. When the
terminal has scrolled, or a second phone turns up, the **📱 Pair** button in the
GUI shows the same QR and addresses on demand — no restart. If the daemon is on
loopback it says so, and tells you to restart with `--host 0.0.0.0`, because
there is nothing to pair with until then.

**Reaching it from anywhere.** A `192.168.x.y` address only resolves while both
devices are on the same router. Put both machines on a
[Tailscale](https://tailscale.com) tailnet and the `100.x` address keeps working
from cellular or someone else's Wi-Fi — Silk Code detects that address
(Tailscale allocates from `100.64.0.0/10`), labels it, lists it first, and puts
*it* in the QR. When you have no such address it says so and points you here.

**The phone layout.** The desktop GUI is a three-column grid; below 820px it
becomes one pane at a time with a switcher — Chat, Project, Activity, Diff —
and the composer pinned within thumb reach.

### What guards it

The token is the only thing between the network and an agent that runs commands
on your machine, so treat that URL as a shell credential.

| Control | What it does |
| --- | --- |
| Access token | 192 bits from `secrets.token_urlsafe`, required on every request once the daemon is off loopback. Compared in constant time. |
| Same-origin check | A browser attaches `Origin` to cross-origin requests; one that disagrees with the `Host` we were reached on is refused, so another site cannot start an agent turn on your behalf. |
| DNS-rebinding guard | Decided on the *parsed* address, not the spelling — a name like `127.0.0.1.evil.example` resolves to loopback and would fool a prefix match. |
| Cookie flags | `HttpOnly`, `SameSite=Strict`, so script cannot read the token and another site cannot ride the cookie. |
| `Referrer-Policy: no-referrer` | The page is opened as `/?token=…` and links out; this stops the query reaching a third party rather than relying on a browser default. |
| `X-Frame-Options: DENY`, `nosniff` | Not framed, not sniffed into another content type. |
| Path confinement | File reads resolve and must sit under the workspace root. |
| No request logging | The HTTP access log is off, so a token in a query string never lands in a log file. |

**Two things it does not do**, worth knowing before you expose it:

- **Traffic is plain HTTP.** On a tailnet WireGuard encrypts it; on open Wi-Fi
  the token crosses the air in the clear. Prefer the tailnet.
- **There is no rate limit.** A 192-bit token is not brute-forceable, so the
  answer to knocking is visibility rather than lockout — see below.

### Watching who connects

`⚙ Environment → Connections` shows who is reaching the daemon:

```
1 active · 1 live stream · 4 refused requests

ADDRESS         REQUESTS  REFUSED  CLIENT                    LAST SEEN
100.101.102.103 ●     128        0  Mozilla/5.0 (iPhone…)     0s ago
192.168.1.44           4        4  curl/8.4.0                12s ago

Most recent refusals
  192.168.1.44  token did not match  /api/state
  192.168.1.44  no token presented   /
```

The daemon also says something on its own terminal the first time an address is
refused, and again at 5, 25 and 100 — enough to notice a scanner, not enough to
let one fill your scrollback:

```
refused 192.168.1.44 (once): no token presented on / [curl/8.4.0]
```

Presented tokens are never recorded, right or wrong: a wrong one is usually a
real credential with a typo, or the right credential for a different daemon, and
the record is rendered in a web page. Client strings are attacker-chosen, so they
are capped, flattened to one line, and escaped at render. Rows are keyed by
address, so devices behind one NAT share a row.


## CLI

```bash
silkcode [path] [--model M] [--mode ask|edit|agent]   # interactive REPL
silkcode -p "add input validation to the API" .       # one-shot, non-interactive
silkcode new [name] [--template T] [--dir D]          # create a new project
silkcode gui [path] [--port N]                        # local GUI
silkcode review [path]                                # AI review of uncommitted changes
silkcode models [add|pull|default]                    # provider/model management
silkcode inference [discover|link|ping|host]          # run the models on another machine
silkcode swarm [path] [--model M] [...]               # multi-agent improvement loop
silkcode update [--branch B]                          # pull updates, hot-apply them
silkcode -update                                     # any verb also works as a flag
silkcode sessions                                     # list saved sessions
silkcode resume <id>                                  # continue a session (GUI or CLI)
silkcode test [path] [--command CMD]                  # run the project's tests (auto-detected)
silkcode mcp [add|remove]                             # manage MCP servers
silkcode connect github                               # set up GitHub access
silkcode config                                       # show configuration
silkcode version [--json]                             # what this install is
```

REPL commands: `/model`, `/models`, `/mode`, `/new`, `/project`, `/diff`, `/usage`,
`/revert`, `/clear`, `/sessions`, `/help`, `/exit`.

### Starting a new project

`silkcode new` scaffolds a project that runs and tests on the first try — source,
a test suite the runner already recognizes, a README, a `.gitignore`, and a
`SILKCODE.md` with project instructions the agent reads on every turn. It then
runs `git init` and makes the initial commit.

```bash
silkcode new --list                                   # show the templates
silkcode new todo-cli --template python-cli           # ~/…/todo-cli, git-initialized
silkcode new site -t web --dir ~/code                 # choose where it lands
silkcode new api --describe "Invoice API for freelancers"
silkcode new api -p "add a health endpoint and a test for it"   # build it out
silkcode new api --open                               # create, then open the REPL in it
silkcode new                                          # prompts for name and template
```

| Template | What you get |
| --- | --- |
| `python` (default) | package + `pyproject.toml` + pytest suite |
| `python-cli` | argparse entry point, `[project.scripts]`, pytest suite |
| `node` | ES-module package with a `node:test` suite (`npm test`) |
| `web` | static HTML + CSS + JS, no build step |
| `blank` | README, `.gitignore`, `SILKCODE.md` only |

Names are slugified (`"My New App"` → `my-new-app`) and the package identifier
follows (`my_new_app`). An existing non-empty directory is refused unless you pass
`--force`, and even then existing files are never overwritten. `--no-git` skips the
repository, scaffolding inside an existing checkout does not bury a nested one, and
a missing git or unset committer identity degrades to a warning rather than losing
the files.

In the REPL, `/new <name> [template]` does the same thing and switches the session
to the new project (created next to the current one, not inside it); `/new` with no
arguments prompts. Either way the project is added to your recent projects, so the
GUI's ＋ modal and `/project` offer it later.

**The full how-to** — creating, opening, switching, teaching the agent about a codebase,
verifying and shipping it, remote/sandboxed projects, and troubleshooting — is at
[silkcode.web.app/projects.html](https://silkcode.web.app/projects.html)
([source](docs/projects.html)). The same guide is built into the GUI: the **? How-to**
button in the PROJECT pane.

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
are). **Project** is a control in the header, beside Session, Model and Mode: it lists the
project you are on, every project you have sessions in, and the ones you opened
before. Choosing one **moves this session** — the conversation, checkpoints and usage
come with you, and the workspace lock moves too. That is what `/project` has always
done in the REPL. The ＋ button opens the same picker for a *new* session instead, so
different sessions can work on different codebases side by side. Either way you can
pick a GitHub repository (cloned for you into `~/.silkcode/projects/`) or type a local
directory.
Sessions are saved to the same store as the CLI — resume any of them from the picker
or with `silkcode resume <id>` (SRS section 47).

The picker lists the sessions of the project you have open, and follows you when you
switch to a session on another project. Sessions are stored per machine rather than
per project, so an unscoped list mixed every repository you had ever opened into
every switcher. Nothing is hidden away, though: when other projects have sessions,
the last entry says how many and where, and choosing it regroups the list by project
so you can jump straight to one. `silkcode sessions` in the terminal is
machine-wide and still lists all of them.

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

```bash
silkcode update              # pull the latest code into this install
silkcode -update             # same thing; the verb also works as a flag
silkcode update --branch main
silkcode update --install    # also run `pip install -e .`, for new dependencies
silkcode update --force      # update even with a dirty working tree
```

Every subcommand also answers to its flag form — `-update`, `--update`, `-models`,
`--gui` — because plenty of tools take their verbs that way and typing
`silkcode -update` used to produce `unrecognized arguments: -update` with no hint
that `silkcode update` was the same command. The handful of flags the REPL itself
defines keep their own meaning: `--sandbox` still runs the REPL against the
configured sandbox rather than opening the `sandbox` command, and `--version`
stays the one-line build id rather than the full `silkcode version` report.

How the update happens depends on how Silk Code was installed, and it works out
which on its own:

| Install | What `silkcode update` does |
| --- | --- |
| a clone, or `pip install -e .` | fast-forwards the checkout to `origin/<branch>` |
| `pip install git+https://…` | reinstalls from the same URL and revision pip recorded |

The second case matters because it is the install the top of this README leads
with, and it carries no git metadata. Silk Code reads pip's own
[PEP 610](https://peps.python.org/pep-0610/) `direct_url.json` record to recover
the URL and branch you installed from, then reinstalls from exactly that. (There
is no `silkcode` package on PyPI, so `pip install -U silkcode` is not a fallback —
it is a 404.)

A git checkout only ever fast-forwards: a dirty tree or local-only commits are
reported rather than overwritten, so nothing is lost and nothing is force-reset.
A running GUI daemon watches the checkout's HEAD and re-execs itself once new
code lands, so the update goes live without a manual restart — sessions are
persisted on disk and survive it.

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
The GUI's **↻ Update** header button does the same thing.

For an install that is not a git checkout — `pip install git+…`, or a release wheel —
one line updates it from anywhere:

```bash
pip install --upgrade --force-reinstall git+https://github.com/RupertCloud/SilkCode
```

`silkcode update` does this for you, reinstalling from wherever pip originally got it.
Run it by hand if your copy predates that support: an install whose updater refuses to
run cannot fetch the fix for its own updater.

`--force-reinstall` is the belt-and-braces form and always works. Plain `--upgrade` is
enough once you are on a build whose version moves with each commit (see below) —
before that, pip sees the same version number and does nothing at all, reporting
success.

### Which version am I running?

Because `silkcode update` pulls whatever is on the remote, the release number
alone identifies nothing — everyone tracking `main` runs different code while
`__version__` says the same thing. So a build id is the release plus, on a
checkout, the commit it sits on:

```bash
silkcode --version          # Silk Code 0.2.1.dev7+g9ccde8e
silkcode version            # + commit, branch, install path, Python, platform
silkcode version --json     # the same, for a bug report or a script
```

| Build id | Means |
| --- | --- |
| `0.2.0` | a released wheel — exactly what was tagged |
| `0.2.1.dev7+g9ccde8e` | seven commits past `v0.2.0`, on that commit |
| `0.2.0+gd4e5f6a` | a checkout sitting on commit `d4e5f6a` |
| `0.2.0+gd4e5f6a.dirty` | …with uncommitted changes on top |

The number is derived from git tags, so it **moves on its own between releases**. That
is not cosmetic: `pip install -U` reads the version to decide whether there is anything
to fetch, so a version that never changes makes every upgrade a silent no-op.

`silkcode version` also names the right way to upgrade *this* install, which
differs between the two. The GUI page carries the build of the daemon that
served it, so a tab left open across an update notices it has gone stale and
says so instead of quietly running against newer server code.

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

### Who asked for this?

Only you can authorize a consequential action. Everything the agent reads on the
way — a file, command output, a fetched page, an MCP result — is data: it can
describe an action and suggest one, but it cannot authorize it. A repository whose
README carries an HTML comment addressed to the assistant, telling it to disregard
its instructions and push, is describing what its author wants — not what you asked
for. (Spelled out rather than quoted here on purpose: a doc that carries a working
payload poisons every agent that reads it, including the ones helping you.)

So every permission prompt now shows what you actually asked and what the agent read
to get here, and in one narrow case it does more: if content read during the turn was
written to steer an agent, an action that leaves this machine (a push, a merge, a
`curl | sh`) asks even when you granted it or answered "Yes to all". Ordinary work is
untouched — a tainted turn still runs `ls` and `pytest` without a word — because a
gate that interrupts often is answered with "yes to all", and then it protects nothing.

Detection is deliberately conservative and will miss things phrased as documentation;
it is context for a decision you were already being asked to make, not the control.
The control is that high-risk actions stop and ask. See `silkcode/provenance.py`.

The same rule applies to the files a repository puts in front of the agent before any
tool runs — `SILKCODE.md`, `.silkcode/memory.md`, and skill descriptions. They arrive
with a clone, so they are read the same way tool output is: an ordinary one is used
exactly as before, and one carrying text written to steer an agent is kept out of the
agent's instructions, reported to you, and counted as something the turn consumed —
so a push in that turn asks even under a standing grant.

Risky commands are classified from the command that will actually run, not the text it
was written as. `"git" push`, `g\it push` and `r\m -rf` are all the plain thing to a
shell, so they are all the plain thing to the gate. A line that cannot be parsed, or
whose command name is decided at run time (`$CMD push`), is treated as high-risk
rather than guessed at.

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
├── tools/           read/write/edit, glob/grep, run_command, run_tests, live_server, git status/diff/log/commit
├── liveserver.py    built-in live preview server: serve the workspace + auto-reload pages on change
├── agent/           the agent loop: streaming, tool dispatch, permissions
├── swarm.py         multi-agent improvement loop (tester/critic/worker, 0-10 scoring)
├── update.py        self-update: pull from git, hot-apply via GUI daemon restart
├── permissions.py   risk classification + ask/edit/agent modes + "yes to all"
├── provenance.py    what a turn read, so a file cannot authorize a push
├── version.py       build identity: release + commit, for installs that track a branch
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
