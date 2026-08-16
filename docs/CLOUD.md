# Silk Cloud — a fully hosted Silk Code

**Status: design.** Nothing in this document is implemented yet. It is the
architecture for taking the local-first harness in this repository and running
it as a hosted product: sign in with GitHub, pick a repo, start working — no
`pip install`, no API keys, no local model server.

The guiding constraint is unchanged from the README:

> The coding environment belongs to the developer. The AI model is replaceable.

Hosting must not quietly become lock-in. Silk Cloud is a *deployment* of the
same agent that ships in the wheel, not a fork of it.

---

## 0. Start here — the whole thing in plain language

This document is long. The short version:

**What Silk Code is.** An AI coding assistant that works with *any* AI model.
Every serious competitor — Claude Code, Cursor, Copilot — is American and tied
to one American AI company: their price, their model, your code sent to them.

**Who we are building for.** Banks and fintechs, governments, universities and
smaller companies across Africa, Asia and Latin America. They want AI in their
software work and have three needs no US tool meets together: **cheaper
models**, **their own machines**, and **their own rules about where code
goes**. Silk Code already does all three — cheap providers, Ollama/vLLM on
their own hardware, and an MIT licence anyone can read, teach from and modify.
The incumbents cannot copy this, because their product *is* their model.

**Who pays first is probably financial services.** For a regulated bank,
keeping source code in-country is a rule, not a preference — so where
regulation blocks the US tools, our competition is not Copilot, it is *nothing*.
Banks and fintechs also have budget, infrastructure and a normal way to buy
software. Universities and ministries matter strategically, but they are slower
and poorer. See §14.5.

**What "Cloud" adds.** Today Silk Code is one person on one laptop. Cloud makes
it serve a whole institution: everyone signs in, work runs on the institution's
servers, using the institution's chosen model, under its policy. The coding
engine does not change — we build sign-in, user management, model policy and an
installer around it.

**How we get paid.** Sell to the *institution*, not the seat — a university
with 5,000 students cannot buy 5,000 subscriptions but can sign one annual
contract. Licence, plus setup and support, plus training; then grants and
government programmes, partners who resell, and eventually public sign-up.
**We do not make money reselling AI tokens** — providers publish their prices,
so any markup is visible and gets competed away. Pass inference through near
cost and charge for the platform.

**What we build, in order.** ① multi-user · ② point it at their model ·
③ package it to install on their servers · ④ land one paid pilot ·
⑤ open public sign-up later.

**What to prove before building.** One partner institution. One invoice
actually paid. One campus running against its own model.

**The main risk.** Getting paid is harder than building it — cards are
uncommon, currency rules are real, institutions buy once a year through
procurement. That is not an engineering problem, which is why it gets
postponed. Answer it first.

**If the code is free, what are they paying for?** Not the software — the
accountability, the deployment, the upkeep, the compliance paperwork, the
administration layer for many users, and the training. For government and
campus buyers there is a twist: procurement often *cannot* buy "free", because
there is no vendor, invoice or accountable party. Open source wins the security
review; the contract is what makes adoption possible. §14 has the detail,
including which parts stay MIT and which are commercial.

**Is coding a big enough market?** As a wedge, yes; as the whole business,
probably not. What this repository actually contains is an *agent runtime* —
model routing, tool calling, permissions, sandboxing, sessions, MCP — of which
coding is the first application. Coding is the right opening because it is
measurable, has an identifiable buyer, and already works. The platform then
expands *inside the account*, after delivery. Broaden the platform, not the
pitch. §15.

**How it reaches institutions.** Not through marketing spend. The free MIT tool
creates champions inside organizations; universities are a channel rather than
only a customer, because their students become the developers who ask for it;
local partners carry the contract and the vendor approval. The step everyone
gets wrong is the handoff from champion to procurement — build the security
whitepaper, DPA template and price sheet early, because without them bottom-up
adoption stalls exactly when it should become revenue. §16.

Everything below is the detail behind those paragraphs. §16 is distribution,
§15 the market size, §14 what customers buy, §13 the market, §12 the money,
§11 the roadmap, §§2–9 the architecture.

---

## 1. The three decisions this design rests on

| Decision | Choice | Consequence |
| --- | --- | --- |
| **Model access** | Managed pooled credits — Silk holds the provider keys, users spend Silk credits | Zero-config onboarding; Silk carries inference cost, abuse risk, and provider rate limits |
| **Runtime** | Full cloud workspace — repo, agent loop, file edits and commands all run in a per-session cloud container | True zero-install; works from a phone; the browser is a thin client |
| **Codebase** | One agent, two deployments — `silkcode` stays the pip-installable local harness, cloud imports it | No fork, no drift; local users keep BYO-key freedom |
| **Substrate** | Our own containers — Pods on our existing Kubernetes, isolated with gVisor | No sandbox vendor; we own isolation, warm-start, capacity and reaping (§7) |

Sections 3–9 follow from these. Section 10 records where the pooled-credit
choice creates tension with "without limitations" and what to do about it.

---

## 2. The load-bearing insight: the container is the tenant boundary

The obvious reading of "make it multi-tenant" is that `silkcode/gui/server.py`
has to be rewritten — it is a single-tenant singleton today. `GuiState.__init__`
takes *one* `path`, builds *one* `Workspace`, calls `Config.load()` against
`~/.silkcode`, and opens *one* `SessionStore` in that same directory. All of
that is per-process global state.

In the full-cloud-workspace model, that is not a problem to fix. It is the
design. **Each user session gets its own container, so each `GuiState` still
serves exactly one user.** The single-tenant assumptions stay true because the
tenant boundary moved outward, from the process to the machine.

Three things make this work with very little change to the existing code:

1. `config_dir()` already honours `$SILKCODE_HOME` (`silkcode/config.py:16`).
   Per-container that is all the config isolation needed.
2. `Workspace.resolve()` already refuses paths outside the root
   (`silkcode/workspace.py:18`). Defence in depth on top of container isolation.
3. `OpenAICompatProvider` already accepts a literal `api_key` and arbitrary
   `base_url` (`silkcode/providers/openai_compat.py:18`), so the pooled-credit
   gateway needs **zero changes to the agent or provider layer** — see §5.

What is genuinely new is everything *around* the container: authentication,
orchestration, the model gateway, metering, and durable session storage. That
is the control plane, and it does not exist yet.

---

## 3. Target architecture

```
Browser (thin client, today's app.html)
    │  HTTPS, session cookie
    ▼
┌─────────────────────────────────────────────────────────┐
│ Control plane  (new)                                     │
│  · GitHub OAuth web flow, user + org records             │
│  · workspace orchestration: start / attach / reap        │
│  · credit ledger, plan limits, billing                   │
│  · authenticated reverse proxy → the user's container    │
└─────────────────────────────────────────────────────────┘
    │ mTLS / signed short-lived token
    ▼
┌─────────────────────────────────────────────────────────┐
│ Runner Pod  (one per session, disposable, gVisor)        │
│  · our own K8s, dedicated tainted node pool              │
│  · git clone of the user's repo  ← source of truth       │
│  · `silkcode gui` — the code in this repo, unchanged     │
│  · SILKCODE_HOME=/workspace/.silkcode                    │
│  · no provider API keys, no long-lived GitHub token      │
│  · no service-account token, no inbound                  │
└─────────────────────────────────────────────────────────┘
    │ OpenAI-compatible HTTP                    │ git push
    ▼                                           ▼
┌───────────────────────────┐          ┌──────────────────┐
│ Model gateway  (new)      │          │ Egress proxy     │
│  · session token → user   │          │  allowlist:      │
│  · balance check          │          │  registries,     │
│  · injects real key       │          │  git hosts,      │
│  · meters Usage → credits │          │  gateway         │
└───────────────────────────┘          └──────────────────┘
    │
    ▼  DeepSeek · Qwen · Kimi · GLM · MiniMax · OpenRouter · …
```

Postgres backs the control plane (users, sessions, ledger). Object storage
holds transcripts. Neither is on the container's critical path for a turn.

---

## 4. What moves, what splits, what is new

### Reused as-is

| Component | Why it survives |
| --- | --- |
| `silkcode/agent/loop.py` | Provider-agnostic, already accumulates `Usage` per turn — the metering primitive is already there (`loop.py:94`) |
| `silkcode/providers/` | `ModelProvider` ABC + OpenAI-compatible client is exactly what the gateway speaks |
| `silkcode/tools/` | Tools operate on a `Workspace`; in cloud mode the workspace is local *to the container* |
| `silkcode/workspace.py` | Path containment already enforced |
| `silkcode/swarm.py` | Token budgets already implemented (`swarm.py:330`, status `token-budget`) — reusable as the credit-exhaustion mechanism |
| `silkcode/gui/app.html` | Already SSE-streaming with permission prompts, file tree and diff. The API contract survives; only auth and session-id types change |

### Changed

| Component | Change | Reason |
| --- | --- | --- |
| `silkcode/sessions.py` | Extract a `SessionStore` interface; keep the JSON/`fcntl` implementation for local, add a durable one for cloud | Container disks are disposable; a session must outlive its container |
| Session ids | `int` → opaque ULID | `SessionStore.new_id()` hands out sequential integers (`sessions.py:52`). Enumerable ids across a shared control plane is an IDOR waiting to happen |
| `silkcode/github_oauth.py` | Add a redirect-based web OAuth flow beside the device flow | Device flow exists because terminals cannot redirect. Browsers can |
| `silkcode/config.py` | Add a `silkgateway` provider entry pointed at the gateway | See §5 — this is a config change, not a code change |
| `silkcode/execbackend.py` | Not used in cloud mode | `RemoteBackend` syncs a tarball *up* and never syncs results back ("Artifacts created remotely are not synced back"). When the agent already runs beside the files, sync disappears. Keep it for the local + cloud-exec hybrid |

### New (proposed layout: a `silkcloud/` package, or a separate private repo)

- `silkcloud/gateway/` — the metered, OpenAI-compatible model proxy
- `silkcloud/controlplane/` — auth, users, credits, orchestration, the proxy
- `silkcloud/runner/` — container image + supervisor that boots `silkcode gui`
- `silkcloud/placement/` — the `RunnerPlacement` drivers (§7.1);
  `KubernetesPlacement` is the one we run

The rule that keeps this honest: **`silkcloud` imports `silkcode`; `silkcode`
never imports `silkcloud`.** If a cloud feature needs an agent change, it lands
in `silkcode` and local users get it too.

---

## 5. The model gateway — the heart of pooled credits

The gateway is an internal OpenAI-compatible endpoint. Containers are
configured with one synthetic provider:

```json
{
  "providers": {
    "silk": {
      "type": "openai_compat",
      "base_url": "https://gateway.silkcode.dev/v1",
      "api_key": "<short-lived session token>"
    }
  },
  "default_model": "silk/deepseek-chat"
}
```

`OpenAICompatProvider` sends that token as a bearer header and otherwise
behaves normally, so **no agent or provider code changes at all**. The gateway
then:

1. resolves the session token → user, session, plan;
2. checks the credit balance and per-session cap;
3. maps the requested model (`deepseek-chat`, `qwen3-coder-plus`, `MiniMax-M2`,
   `glm-4.6`, …) to a real upstream from `BUILTIN_PROVIDERS`;
4. swaps in the real provider key and streams the response back verbatim;
5. records prompt/completion tokens and debits credits from a pricing table.

### Resolve `principal → upstream`, not `model → pooled key`

Step 3 is the one place a shortcut would be costly later. Written narrowly it
maps a model name to *our* pooled key. Written generically it resolves a
**principal** — user, organization, or session — to an `(upstream endpoint,
credential)` pair, with the pooled key as merely the default case.

That one abstraction covers all three products this design eventually sells:

| Principal resolves to | Serves |
| --- | --- |
| Silk's pooled key | Hosted default (§5) |
| The user's own key | BYO-key tier (§10) — our best-margin customer |
| The organization's vLLM / Azure / Bedrock endpoint | Enterprise org gateway (§12.5) |

It costs almost nothing to build this way at M0 and is an unpleasant refactor
once a ledger, a cache and a router all assume a single global upstream. Build
it generically first.

### Why the container must never hold a provider key

A user can run arbitrary shell commands in their own container — that is the
entire product. `cat /proc/self/environ` would hand them the pooled
`DEEPSEEK_API_KEY` in one turn. So the current key-resolution path
(`Config.api_key_for` reading `api_key_env` from the process environment,
`config.py:204`) is exactly what must **not** run in a cloud container.

Session tokens instead: scoped to one session, minted by the control plane,
expiring in minutes and refreshed, revocable, and worthless outside the
gateway. This single property is what makes pooled credits survivable.

### Streaming, tools, and failure

The gateway must be a transparent proxy for SSE, including tool-call deltas —
`_parse_stream` in `openai_compat.py` reassembles fragmented tool-call
arguments, and any gateway that buffers or re-chunks will break it. Metering
therefore happens on the final `usage` frame, with a token-count fallback for
providers that omit it on streamed responses.

Provider outage handling belongs here too: the gateway is the natural home for
the **Model Auto Router** that `NEXT_STEPS.md` lists as the headline missing
V0.2 item. `Config._resolve_auto` picks the first *available* model; a
server-side router can pick the best model for the task, fail over on 429/5xx,
and do it for every hosted user at once.

---

## 6. Credits, metering, and abuse

**Ledger, not a counter.** Append-only rows (`user_id`, `session_id`, `model`,
`prompt_tokens`, `completion_tokens`, `credits`, `at`) with balance as a
materialized sum. Reconcilable against provider invoices; a counter is not.

**Enforcement at three levels:**
- *Pre-flight* — gateway rejects a request when the balance is zero.
- *Mid-turn* — the swarm's `token-budget` stop already models this
  (`swarm.py:420`); generalize it so a normal agent turn also halts cleanly
  with a "credits exhausted" status rather than an opaque provider error.
- *Per-session cap* — a runaway loop cannot drain a month's credits.

**Abuse is the real cost risk, and it is not mainly about tokens.** Pooled
credits plus arbitrary code execution invites crypto mining, DDoS origination,
and spam relaying. Mitigations, in rough order of value:

- egress through an allowlist proxy — package registries, git hosts, the
  gateway; default-deny everything else, no inbound at all;
- hard CPU/memory caps and an idle reaper (containers die after N minutes);
- wall-clock and concurrent-session limits per account;
- GitHub account age/verification signals before granting free credits;
- card-on-file for anything beyond a small trial.

The egress allowlist has a real cost: projects that fetch from unusual hosts
will break, and the allowlist becomes a support surface. Publish it and make
it extendable per-project rather than pretending it is invisible.

---

## 7. Isolation: our own containers, on our own Kubernetes

**Constraint: we run the containers ourselves.** No Fly Machines, no E2B, no
Modal. Session containers are Pods on our existing Kubernetes cluster, isolated
with **gVisor (`runsc`)**.

That is a deliberate trade. A managed sandbox vendor silently handles six
things; owning the substrate means owning all six:

| What the vendor did | What we now build |
| --- | --- |
| Isolation boundary | gVisor RuntimeClass on a dedicated node pool (§7.2) |
| Sub-second start | Warm Pod pool + pre-baked toolchain image (§7.4) |
| Placement and capacity | Scheduler is K8s; headroom and node scaling are ours |
| Lifecycle and reaping | Idle reaper + `activeDeadlineSeconds` + orphan sweeper (§7.5) |
| Network containment | NetworkPolicy default-deny + egress proxy (§7.3) |
| Resource containment | Requests/limits, pids, ephemeral-storage quotas (§7.2) |

### 7.1 The placement driver, and why it is swappable

Everything above sits behind one small interface. The runner image and the
session protocol do not know where they run:

```python
class RunnerPlacement(Protocol):
    def start(self, session: SessionSpec) -> RunnerHandle: ...
    def attach(self, handle: RunnerHandle) -> AttachURL: ...
    def stop(self, handle: RunnerHandle) -> None: ...
    def sweep(self) -> list[RunnerHandle]: ...   # orphans
```

`KubernetesPlacement` is the driver we build and run. A `FlyPlacement` (or any
other) is a second implementation of the same four methods — useful for burst
overflow, a region we have no cluster in, or a faster path to a first public
launch. Keeping this seam costs almost nothing today and prevents the substrate
decision from being load-bearing on the rest of the design.

**The runner image is the portable artifact.** It clones a repo, runs
`silkcode gui` against the gateway, and exits when idle. Nothing in it is
Kubernetes-specific.

### 7.2 Pod shape

One Pod per session, in a single `silk-sessions` namespace, on a **dedicated,
tainted node pool**. The control plane and the gateway must never be scheduled
onto a node that runs user code — that is the one placement rule that matters.

Per-user namespaces are tempting for RBAC and `ResourceQuota`, but K8s degrades
badly at thousands of namespaces and our sessions are ephemeral. Use labels
(`silk.session/user`, `silk.session/id`) and enforce per-user limits in the
control plane, where the credit ledger already lives.

Non-negotiables on the Pod spec:

- `runtimeClassName: gvisor`
- `automountServiceAccountToken: false` — **the most important line here.**
  A mounted token hands a user with shell access an API-server credential.
- `securityContext`: `runAsNonRoot`, `allowPrivilegeEscalation: false`,
  `capabilities: drop: [ALL]`, `seccompProfile: RuntimeDefault`
- `readOnlyRootFilesystem: true`, with writable `emptyDir` mounts for
  `/workspace` and `/tmp` (`sizeLimit` set on both)
- CPU/memory requests **and** limits, `pids` limit, `ephemeral-storage` limit
- no `hostPath`, no `hostNetwork`, no privileged, no container runtime socket
- PodSecurity admission `restricted` enforced on the namespace

### 7.3 Network containment

Default-deny egress `NetworkPolicy`, then allow exactly three things: DNS, the
egress proxy, and the model gateway. All package-registry and git traffic goes
through the proxy allowlist from §6.

Two egress paths are easy to forget and both are breaches:

- **The node metadata endpoint** (`169.254.169.254`). Reachable from a Pod by
  default on most clouds, and it hands out node IAM credentials. Deny it
  explicitly.
- **Cluster-internal CIDRs** — Pod and Service ranges, the API server. A
  session Pod must not reach the gateway's internals, the database, or another
  session. Deny the ranges, allow the specific Services.

No inbound at all. Sessions are attached by the control plane proxying to the
Pod IP from inside the cluster — never an Ingress or a public address per
session.

### 7.4 Start latency and the gVisor tax

Cold start is image pull (avoided by pre-pulling onto the node pool) plus Pod
sandbox creation (~150 ms under `runsc`) plus `git clone`. The clone dominates,
so: shallow clone by default, and a **warm pool** of pre-created Pods idling at
a low CPU floor, claimed and cloned on demand. Size the pool from concurrent-
session telemetry; it is the difference between "instant" and "eight seconds."

**gVisor's real cost is syscall-heavy file I/O, and `npm install` is exactly
that.** Expect a measurable slowdown on dependency installs. Mitigations:
enable gVisor's `directfs`, and bake the common toolchains and a warm package
cache into the image rather than fetching them per session.

Compatibility needs validating against the actual image, not assumed. Known
friction under `runsc`: no nested containers (Docker-in-Docker is out, so
projects whose tests spin up containers will fail), `io_uring` is unsupported,
and `ptrace`-based debuggers and profilers are partly limited. Ordinary
builds and test suites — `pytest`, `cargo`, `go test`, Node — are fine.
Publish the limitations rather than letting users discover them mid-task.

### 7.5 Lifecycle

Three independent mechanisms, because any one of them can fail:

1. **Idle reaper** in the control plane — no turn for N minutes, Pod deleted.
2. **`activeDeadlineSeconds`** on the Pod — a hard TTL the control plane cannot
   forget to enforce.
3. **Orphan sweeper** — periodically reconciles live Pods against session
   records and deletes anything unclaimed. This is what saves the bill when the
   control plane crashes mid-start.

**Ephemeral by default.** Clone fresh from GitHub at session start, work, and
push to a branch. Durability lives in the user's git remote, not in Silk's
storage. This is how the existing GitHub integration already thinks
(`silkcode/github.py`, auto-push, PR creation), it dodges a large class of
backup and privacy problems, and it makes Pod loss a non-event. Offer
persistent volumes later as an opt-in for slow-to-restore workspaces.

Keep the existing Cloudflare Worker as the reference implementation of the
sandbox protocol for local users — it is a different product surface, not a
competing one.

---

## 8. Auth, GitHub, and secrets

- **Sign-in:** GitHub App, web OAuth redirect flow. The device flow in
  `github_oauth.py` stays for the CLI.
- **Repo access:** the GitHub App's *installation* token, scoped to the repos
  the user selected, minted per session and short-lived. The user's own OAuth
  token should never be written into the container.
- **Git credentials in the container:** a credential helper that fetches a
  fresh token from the control plane on demand, so nothing durable sits on disk.
- **User secrets** (a project's own `.env`, test database URLs): encrypted at
  rest in the control plane, injected at container start. Accept that a user
  with shell access can read their own secrets — that is fine, they are theirs.
  What must never appear is *Silk's* secrets or *another tenant's*.

---

## 9. Permissions in a cloud threat model

`PermissionManager` classifies risk assuming the blast radius is the
developer's laptop — `rm -rf`, `sudo`, `mkfs`, `dd` are HIGH risk
(`permissions.py:16`). In a disposable container most of that list is close to
harmless: destroying the container is the recovery procedure.

What actually matters in cloud shifts to **outward-facing and cross-boundary**
actions:

- `git push`, `github merge-pr` — already HIGH risk, correctly;
- anything touching the egress allowlist;
- credit-spending actions (a swarm run is a much bigger commitment when Silk is
  paying).

Concretely: keep the existing classifier, and add a `cloud` permission profile
that downgrades local-destruction patterns and upgrades spend and publish
actions. `agent` mode becomes a much more comfortable default in the cloud than
it is locally — which is a large part of why hosted agents feel faster.

---

## 10. The honest tension in "without limitations"

Pooled credits give the best onboarding and the worst version of "unlimited."
Inference has a marginal cost, so free unlimited use is not a pricing decision,
it is an unbounded liability. Any hosted product on pooled keys *will* have
caps, and it will also inherit the upstream providers' own rate limits — which
means limits can appear at the worst moment through no fault of the user.

Two things resolve this without abandoning the decision:

1. **Keep BYO-key as the unlimited tier.** Attach your own DeepSeek/OpenRouter
   key and the caps disappear, because Silk is no longer paying. The gateway
   supports this with one extra branch — per-user key instead of the pooled
   key — and it costs almost nothing to build alongside. It is also the
   version that stays true to "the model is replaceable."
2. **Make the limits legible.** A visible balance, cost per turn, and a
   projection beat a silent throttle. `Usage` is already tracked per turn and
   shown in the GUI header, so the data is in hand.

Recommendation: ship pooled credits as the default experience and BYO-key as
the escape hatch in the same release. It is the difference between "hosted Silk
Code" and "hosted Silk Code you can't get stuck in."

And BYO-key is not the revenue leak it looks like. A BYO-key user costs us no
inference at all, so that tier carries the *best* gross margin in the product —
see §12.1. It is a concession to users and an improvement to the P&L at the
same time, which is a rare combination and an argument for shipping it early
rather than reluctantly.

---

## 11. Roadmap

### 11.1 The shape of it

Five milestones on the critical path, and one track that runs alongside from
day one:

```
Track A ── foundations (also improve the local product) ────────────┐
                                                                     │
M0 Gateway ──▶ M1 Runner ──▶ M2 Hosted MVP ──▶ M3 Launch ──▶ M4 Unlock
   (revenue)     ⚠ GATE 1      (private beta)    (public)     (why host)
                                    ⚠ GATE 2      ⚠ GATE 3
```

The ordering is deliberate: **M0 earns money without a single container, and
M1 is a measurement whose result can change M2.** The expensive, irreversible
work is scheduled last, behind the evidence that should inform it.

### 11.2 Track A — foundations, startable now

None of these need a hosting decision, and every one of them improves the
shipped local product. Doing them first means a delayed cloud launch wastes
nothing.

| | Work | Also gives local users |
| --- | --- | --- |
| A1 | Extract a `SessionStore` interface; keep JSON/`fcntl` as the local impl | Pluggable session storage |
| A2 | Opaque session ids (ULID) replacing sequential ints (`sessions.py:52`) | Nothing — but it is a one-line-of-thinking change now and a migration later |
| A3 | Generalize the swarm's `token-budget` stop (`swarm.py:420`) to ordinary turns | Per-session spend caps in the CLI and GUI |
| A4 | Pricing table + cost-per-turn accounting on the existing `Usage` | **SRS 48/49 usage dashboard** — an open V0.2 gap |
| A5 | Provider retry and failover on 429/5xx in the provider layer | Fewer hard failures on flaky upstreams |

A4 and A5 are the two that matter most: A4 is the ledger's data model
rehearsed locally, and A5 is the auto-router's mechanism.

### 11.3 M0 — Gateway *(no containers at all)*

The metered, OpenAI-compatible proxy from §5, plus accounts, ledger and
billing. Local CLI and GUI users point `base_url` at it, buy credits, and stop
managing API keys.

**Done when:** a user with no provider key can `pip install silkcode`, sign in,
and work — paying in credits.

Why first: it ships the entire pooled-credit business model with zero
orchestration work, and it de-risks the hardest correctness problem in the
design — transparent SSE proxying of fragmented tool-call deltas (§5). It also
closes the **Model Auto Router**, which `NEXT_STEPS.md` lists as the headline
missing V0.2 item; server-side, it upgrades every hosted user at once.

### 11.4 M1 — Runner under gVisor *(the measurement)*

Container image with the toolchains, cloning a repo and running `silkcode gui`
against M0's gateway. Driven by hand with `kubectl run`, RuntimeClass `gvisor`,
and the full §7.2 hardened Pod spec from the very first run.

**Done when:** the agent completes a real task under `runsc`, and we have
numbers for `npm install` versus `runc`, cold-start, and how many representative
projects need Docker-in-Docker.

> **⚠ Gate 1 — is gVisor viable?** If the file-I/O tax is unacceptable after
> `directfs` and a baked package cache, or if too many real projects need
> nested containers, the answer is not "push on." It is to reconsider the
> boundary (Kata/Firecracker on the same cluster) or to lean on the second
> placement driver (§7.1). This gate is the reason M1 precedes M2.

### 11.5 M2 — Hosted MVP *(private beta)*

Control plane: GitHub web OAuth, session records, `KubernetesPlacement`
(start/attach/stop/sweep), the in-cluster attach proxy, and the durable
`SessionStore` backed by A1. `app.html` gains cookie auth and opaque ids.

**Done when:** an invited user signs in, picks a repo, works, and pushes a
branch — from a browser, with nothing installed.

> **⚠ Gate 2 — what does a session actually cost?** Node-hours plus tokens per
> real session, measured on beta traffic. This sets the free tier, or proves
> there cannot be one. Deciding it before beta is guessing.

### 11.6 M3 — Public launch *(safety and commerce)*

Everything that makes it safe to hand to strangers: NetworkPolicy and the
egress proxy (§7.3), warm pool (§7.4), the three reapers (§7.5), abuse signals,
per-session caps, plan limits — and the **BYO-key tier from §10, in this
milestone, not later**. It is one branch in the gateway and it is what makes
"without limitations" true rather than aspirational.

> **⚠ Gate 3 — does the egress allowlist break real work?** Run beta traffic
> against it in report-only mode during M2 and count what it would have
> blocked. An allowlist tuned on guesses becomes a support queue.

### 11.7 M4 — What hosting unlocks

Only now do the features that justify hosting over a local install:

- **Async tasks** — the agent works with no browser open, and the result is a
  PR. This is the single biggest unlock and it is impossible locally.
- **PR-first workflow** — review, CI-failure follow-up, iterate on a branch.
- **Mobile** — `app.html` is already a thin SSE client; the work is layout.
- **Teams** — shared workspaces, org model policy, audit logs, pooled billing.
  Closes most of **SRS 80 / V0.3 enterprise**, which the local product could
  never really deliver.

### 11.8 Explicitly deferred

Named so they do not quietly consume M0–M3: persistent workspace volumes,
GitLab, tree-sitter symbol indexing, per-user namespaces, multi-region, and
anything in `NEXT_STEPS.md` Priorities 1–2 that is not on Track A. The
Projects work (Priority 1) partly reshapes into M2's repo picker; the rest can
wait.

### 11.9 How this meets the existing roadmap

The cloud work is not a detour from `NEXT_STEPS.md` — it completes three items
already on it. The Model Auto Router lands in M0, the usage dashboard
(SRS 48/49) in Track A, and most of V0.3 enterprise (SRS 80) in M4. That is
the argument for doing it in this order: the first paying milestone also pays
down existing roadmap debt.

## 12. Revenue

> **Read §13 first.** The margin logic below (§12.1, §12.3) holds anywhere. The
> *pricing shapes* in §12.2 — monthly seats, self-serve credits, card billing —
> assume a North American or European buyer. For the market this product is
> actually aimed at, §13 replaces them.

### 12.1 We are selling three things, and only one has good margin

The instinct is "hosted product plus enterprise." That is the right destination
but the wrong resolution, because a hosted session bundles three separable
goods with very different economics:

| What the user consumes | Our cost | Margin | Reality |
| --- | --- | --- | --- |
| **Inference** (tokens) | Provider price, direct pass-through | Thin | A commodity with published prices. Resellers compete this to near zero — the user can check our markup against DeepSeek's own price list in one click |
| **Compute** (the Pod) | Node-hours on our cluster | Moderate | Real margin, but only at good utilization. Idle capacity and the gVisor tax eat it directly (§12.3) |
| **The platform** (harness, UI, orchestration, teams, policy) | Engineering, amortized | High | This is the actual business |

**Do not build the business on token markup.** Charging cost-plus on inference
makes us a payment processor with extra steps, and it puts us in a price war
with every other reseller. Inference should be billed at or near cost — as a
convenience that removes the API-key step, not as the profit centre.

The corollary is the reframe in §10: a **BYO-key user is our best-margin
customer**, because they pay for platform and compute while carrying their own
inference cost. That tier should be priced and marketed as a first-class
option, not hidden as a grudging escape hatch.

### 12.2 The pricing structure that follows

Five tiers, each pointed at a different job:

| Tier | What it is | Who it is for | Margin |
| --- | --- | --- | --- |
| **Local** | The MIT wheel, BYO key, free forever | Adoption engine | n/a — this is marketing |
| **Free hosted** | Small monthly session-hour and credit allowance | Trial, conversion | Negative by design; cap it hard (§12.4) |
| **Pro** | Monthly seat, included credits + session hours, overage billed | Individual developers | Moderate; breakage helps |
| **Pro BYO-key** | Cheaper monthly seat, platform + compute only, uncapped inference | Developers who already hold keys, or want a specific model | **Highest** |
| **Team / Enterprise** | Per seat annually + the §12.5 feature set | Organizations | Highest absolute |

Two structural notes. **Included credits plus overage** beats pure pay-as-you-go
for predictability on both sides, and unused allowance is real gross margin.
And **unused credits are a balance-sheet liability**, not revenue — decide the
expiry and refund policy before selling the first one, not after.

The actual numbers must be set from live provider rates and the §12.3
measurements. Do not pick them from intuition; the inputs are cheap to obtain
and wrong pricing is expensive to unwind.

### 12.3 Unit economics are an architecture problem

Gross margin per session is:

```
revenue − (node_hours × node_rate)          ← §7 substrate
        − (tokens × provider_rate)          ← §5 gateway
        − (storage + egress)
```

Several decisions elsewhere in this document are therefore **direct COGS
lines**, which is the argument for treating them as revenue work rather than
infrastructure hygiene:

- **The gVisor tax (§7.4)** — slower `npm install` means more node-seconds per
  session. Gate 1 is not only a technical gate; it sets a gross-margin input.
- **The warm pool (§7.4)** — pre-booted idle Pods are latency we buy with
  margin. Pool size is a pricing decision wearing an ops costume.
- **The three reapers (§7.5)** — an unreaped Pod bills until someone notices.
  The orphan sweeper is a margin control.
- **Abuse (§6)** — crypto mining is not primarily a security incident, it is
  COGS with no matching revenue. On a free tier with pooled keys it is the
  single fastest way to burn cash.
- **Model routing (§5)** — the auto-router picking a cheaper adequate model for
  a simple task is margin created at zero cost to the user's experience. On
  pooled credits this compounds across every hosted session.

**Instrument gross margin per session from M2's first beta user.** It is the
number Gate 2 exists to establish, and it decides whether a free tier can exist
at all.

### 12.4 The free tier is the dangerous one

Free, pooled keys, and arbitrary code execution is the highest-burn
combination in this design. Anyone can sign up, spend our tokens, and run our
CPU. Constrain it on all three axes at once — capped credits, capped
session-hours, capped concurrency — plus GitHub account-age and verification
signals (§6), and card-on-file beyond a token allowance.

The safest generous free tier is **BYO-key**: unlimited use of the platform,
zero inference cost to us. That is the offer to lead with, and it doubles as
the proof that "the model is replaceable" is real.

### 12.5 Enterprise: sell what the frontier labs structurally cannot

Enterprise revenue will not come from being a cheaper Claude Code. Cursor,
Copilot and Claude Code have distribution we will not out-spend, and a
me-too pitch loses on every axis.

The wedge is the thing already written on the front of the README: **the model
is replaceable.** A large set of organizations cannot or will not send source
code to a single US frontier lab — regulated industries, defence-adjacent
work, EU and Gulf data-residency regimes, sovereignty policies, and anyone with
a domestic-model mandate. For them, model agnosticism is not a preference, it
is a procurement requirement, and the incumbents cannot offer it because their
product *is* their model.

That makes the enterprise product **bring your own model, bring your own
cloud**:

- **Self-hosted / BYOC** — the whole stack in the customer's cluster; code and
  prompts never leave their perimeter. `silkcode` already runs against vLLM and
  any OpenAI-compatible endpoint, so the hard part is packaging, not capability.
- **Organization model gateway** — central policy over which models any
  developer may use, with per-team routing and spend limits. Already on the
  roadmap as SRS 80; §5's gateway is the implementation.
- **SSO/SAML, audit logs, shared skills, pooled billing** — the rest of SRS 80,
  and the M4 team work.
- **Support and SLA**, priced annually per seat.

**Open-core is the licensing shape this implies**, and §14.3 draws the line:
the MIT harness drives adoption and is the reason a security
team will approve us, while the control plane, gateway and enterprise features
are what is actually sold. Where exactly that line falls needs deciding before
`silkcloud` has much code in it, because moving it later is a re-licensing
argument nobody enjoys.

### 12.6 Enterprise is the destination, not the starting point

The enterprise product in §12.5 is close to the **inverse** of the hosted
product in §§3–7:

| | Hosted | Enterprise BYOC |
| --- | --- | --- |
| Who runs the containers | Us, our cluster | Them, their cluster |
| Who holds the model keys | Us, pooled | Them, their vLLM/Azure/Bedrock |
| Control plane shape | Multi-tenant SaaS | Single-tenant appliance |
| Sales motion | Self-serve, minutes | 6–12 months, security review, SOC 2 |

Same harness, same gateway, genuinely different products. Building both at
once is how a small team ships neither, so **hosted comes first and funds the
enterprise motion** — it produces the revenue, the reference logos, the
operational track record, and the compliance groundwork that an enterprise
buyer will ask for anyway.

There is also a hard ordering constraint: **we cannot credibly sell "run this
in your cluster" before we have run it in ours.** Our own M2/M3 deployment is
the proof, the dogfood, and the reference architecture for the appliance.

But three decisions have to be made *now*, because retrofitting them is
expensive:

1. **The control plane must be deployable single-tenant.** Not built yet, just
   not foreclosed. Concretely: no hard dependency on one cloud's proprietary
   managed services on the critical path — plain Postgres and S3-compatible
   object storage, configuration through env and secrets, no assumption of our
   own DNS or identity provider. A control plane that only runs in our account
   is a rewrite when the first BYOC deal lands.
2. **The gateway needs per-org upstreams from day one.** Routing a request to
   *this customer's* vLLM endpoint is the same code path as the per-user
   BYO-key branch in §10. Build it once, generically: `(principal → upstream +
   credential)`. That single abstraction covers pooled credits, BYO-key, and
   the enterprise org gateway.
3. **The open-core boundary** (§14.3). The MIT harness is not only marketing —
   it is the enterprise lead generator. Developers adopt it locally, security
   teams approve it because they can read it, and the organization then buys
   the control plane, gateway and policy layer. That is the standard open-core
   motion, and it only works if the split is drawn deliberately rather than
   discovered.

Everything else about enterprise — SSO, audit logs, the packaging, SOC 2 — can
wait for M4 and a real prospect. Do not build it on spec.

### 12.7 When money actually starts

Revenue maps onto the §11 milestones, and deliberately starts before the
expensive infrastructure exists:

| Milestone | Revenue unlocked |
| --- | --- |
| **M0 Gateway** | First revenue. Credits sold to existing local users — no containers, no COGS beyond inference |
| **M2 Hosted MVP** | Pro subscriptions in private beta; Gate 2 sets the real prices |
| **M3 Public launch** | Free → Pro conversion at scale; BYO-key tier live |
| **M4 Unlock** | Team seats, then enterprise — the long sales cycle that hosted revenue funds |

The sequencing matters: M0 proves people will pay us for models before we
spend a quarter building a cluster to run their code on.

## 13. The market this is actually for

### 13.1 The customer

Universities, public institutions and organizations across Africa, South and
Southeast Asia, and Latin America. They want AI in their software work. Every
mature harness available to them is built in the United States and coupled to a
single American frontier lab.

That coupling is the opening. For this buyer, model neutrality is not a
preference or a philosophical position — it is what makes adoption *possible*:

- **Cost.** DeepSeek, Qwen, GLM and MiniMax are a fraction of frontier-lab
  prices. A harness that routes to a cheap-but-adequate model is worth more
  here than anywhere else on earth. `BUILTIN_PROVIDERS` already covers exactly
  these providers.
- **Sovereignty.** National data rules, university IP policy, and a broad
  political unwillingness to route public-sector source code through a single
  foreign vendor.
- **Infrastructure reality.** Intermittent connectivity, expensive bandwidth,
  and a campus GPU server that already exists. `silkcode` already runs against
  Ollama, vLLM and LM Studio — today a footnote in the README, and for this
  market the headline feature.
- **Auditability.** MIT licensing means a university can read it, teach from
  it, modify it, and get it through procurement. A closed US SaaS clears none
  of those bars.

The incumbents cannot follow. Their product *is* their model, and their
economics assume a customer paying $20–40 per seat per month.

### 13.2 Why §12.2's pricing does not transfer

Three structural facts, each of which breaks a standard SaaS assumption:

1. **Per-seat monthly pricing fails.** $20–30/month is a meaningful share of a
   developer's salary in much of this market, and a university with 5,000
   computing students cannot pay 5,000 × anything. Charge the *institution*,
   not the seat. (One exception, and it is a large one: regulated financial
   institutions pay per developer at enterprise rates — see §14.5.)
2. **Collection is a real engineering and legal problem, not a checkbox.**
   International card penetration is low, corporate cards rarer, and FX
   controls and repatriation rules are real. Mobile money and local rails
   (M-Pesa, Paystack, Flutterwave, PIX, UPI), bank transfer, and plain invoicing
   against a purchase order matter more than Stripe Checkout. **If we cannot
   collect payment, the business model is irrelevant** — this is the single
   most under-estimated item in this document.
3. **Budgets are annual, lumpy, and procurement-gated.** Institutions buy once
   a fiscal year, through a tender or a framework agreement, often requiring a
   local legal entity, tax registration, and a local invoice. Self-serve
   conversion is not the motion; relationships and paperwork are.

And a fourth, uncomfortable one: **we cannot out-price free.** Large US and
Chinese vendors give product away in these markets for strategic lock-in. The
counter is not discounting — it is neutrality, sovereignty, and not extracting
their data or their students.

### 13.3 Where the money actually is

Six lines, roughly in order of how soon they can pay:

| Line | What it is | Notes |
| --- | --- | --- |
| **Institutional site licence** | Annual, flat, unlimited seats inside one institution. Includes the multi-user control plane, admin and usage dashboards, SSO against their existing IdP, shared skills | The core product. Price by institution size and country tier, not headcount |
| **Deployment and support** | Install into their cluster, wire up their model, integrate their IdP, keep it running | Frequently 30–50% of contract value in this market, and it is the line that makes the licence deliverable |
| **Training and curriculum** | Workshops, instructor material, certification. The harness is MIT and readable, so it is teachable | Not a consolation prize — for universities this is often the thing they most want to buy |
| **Donor and development programmes** | Digital-skills and AI-capacity funding from development banks, foundations and bilateral agencies | A sovereign, self-hostable, open AI platform is precisely the shape these programmes fund. Needs a named owner and real proposal work; it is a business-development discipline, not a side effect |
| **Government and public sector** | National AI strategies, sovereign AI initiatives, e-government, national research and education networks | Longest cycle, largest contracts, strongest sovereignty pitch |
| **Partner / OEM** | Regional clouds and telcos white-label the platform; revenue share | They already have the billing rails, the enterprise relationships and the local entity we lack. This is the fastest route around problem 2 above |

**Managed hosting is a service line here, not the product.** Offer it in-region
for organizations with no infrastructure — this is where the §7 Kubernetes work
pays off — but expect the institutional licence and services to carry revenue.

### 13.4 What this changes upstream

This ICP is not a marketing overlay on the design above. It moves three things:

**BYO-model becomes the default, not the escape hatch.** §10 framed pooled
credits as the primary experience and BYO-key as the way out. For this market
the polarity flips: most customers arrive with their own model — a campus vLLM
box, a national cloud, a cheap Chinese API — and pooled credits are the
convenience option for individuals. Good news for margin (§12.1), since the
best-margin tier becomes the common case.

**The single-tenant deployable control plane is promoted from option to
product.** §12.6 argued for keeping it *possible*. Here it is the thing being
sold, which raises the priority of a genuinely offline-capable install:
distributable images, no phone-home requirement, no assumption of good
bandwidth during setup, and a documented air-gapped path.

**The roadmap order changes.** §11 put M0 (the credit-selling gateway) first
because it monetizes fastest against a self-serve Western buyer. Against an
institutional buyer, the first invoice comes from a campus deployment — which
needs multi-user control plane, per-org model routing, SSO and an installer,
and does *not* need credits, billing, warm pools or an abuse-hardened free
tier. A plausible reordering:

| | Western self-serve order (§11) | Institutional order |
| --- | --- | --- |
| First | M0 credit gateway | Org gateway (per-org upstreams, no credits) + M1 runner |
| Then | M1, M2 hosted MVP | M2 control plane, multi-user, SSO, installer → **first paid pilot** |
| Then | M3 public launch | Deployment, training, references |
| Later | M4 teams and enterprise | M0 credits and M3 public hosting, for individuals and reach |

The `principal → upstream` abstraction in §5 is what makes both orders the same
codebase, which is the strongest argument yet for building it generically at
the very first opportunity.

### 13.5 What to prove first

The cheapest possible validation, before any of the above is built:

1. **One design-partner institution.** A single university or ministry willing
   to co-design and pilot. Their procurement process, their model, their
   hardware, their constraints — discovered by working with them rather than
   guessed at here.
2. **A collection path that actually works.** One real invoice paid, end to
   end, in one target country. Do this early; it gates everything.
3. **A campus deployment running against their own model.** `silkcode` can
   almost do this today against a vLLM box. The gap is multi-user and
   packaging, not capability — which means the first pilot is much closer than
   the full hosted product.

Everything in §§3–12 remains the right architecture. What §13 changes is who
pays first, how they pay, and therefore what gets built first.

## 14. What customers pay for when the code is free

### 14.1 Nobody is buying the software

The code is MIT and downloadable. That is not the product, and it never was.
What an institution is actually buying:

| What they pay for | Why free source does not provide it |
| --- | --- |
| **Accountability** | A contract, an SLA, a named vendor, someone answerable when it breaks. Free software has no counterparty |
| **Making it work** | Installed into *their* cluster, wired to *their* model, integrated with *their* identity system, on *their* network. A ministry does not have people who do this |
| **Keeping it working** | Security patches, version upgrades, migrations, someone to call. Ongoing, which is why it is the best revenue line |
| **Time** | They *could* self-host free — with three engineers for six months they do not have. We sell the six months back |
| **Compliance paperwork** | Security questionnaires, data-processing agreements, pen-test reports, procurement documents. Tedious, mandatory, and not something a repository provides |
| **The administration layer** | Many users, org policy, SSO, audit logs, spend visibility (§14.3) |
| **Knowledge** | Training, curriculum, certification. For universities frequently the most wanted item of all |

The framing that follows: **do not price the software, price the outcome.**
"A supported deployment, kept running, with an SLA" is purchasable. "A licence
for software you could have downloaded" invites exactly the objection in the
question.

### 14.2 Public procurement cannot buy "free"

The counterintuitive part, and it matters most for the government and campus
segments: **being free can *block* a purchase.**

A ministry or a public university cannot put "we downloaded it" through
procurement. There is no vendor, no invoice, no line item, no accountable
party, no support contract — and often an explicit policy requiring all three.
Meanwhile the same institution's security review is far easier to pass *because*
the source is readable and auditable.

So open source does not cannibalise this segment. It de-risks the decision
while leaving the purchase fully intact — they still need someone to sell them
a supported, warranted, invoiced version. Open source is the reason they trust
it; the contract is the reason they can adopt it.

### 14.3 What stays open, and what is commercial

One test decides every case:

> **Does closing this weaken the claim that the model is replaceable, or that
> the code is auditable?** If yes, it stays open. If it only helps an
> organization administer many users, it can be commercial.

| Always open (MIT) | Commercial |
| --- | --- |
| The agent, tools, CLI, local GUI | Multi-user control plane |
| The provider layer and every model integration | Organization model gateway and policy |
| Single-user local use, forever free | SSO/SAML, audit logs, admin and spend dashboards |
| The sandbox protocol | Packaged installer, managed updates, support tooling |

Closing the harness would destroy the thesis — a sovereignty pitch that cannot
be inspected is not a sovereignty pitch. Closing the *administration* layer
costs an individual developer nothing, because a single user never needed it.
That asymmetry is what makes open-core work here rather than being a tax on
goodwill.

Licence choice for the commercial half is a live decision (§17) — permissive,
copyleft, or source-available all behave differently when a large cloud decides
to resell us.

### 14.4 The segments buy different things

Government agencies, departments, schools, campuses and SMEs are not one
market. They differ in budget, cycle, and which product they should even be
sold:

| Segment | Buying | Product | Cycle |
| --- | --- | --- | --- |
| **Bank / regulated financial** | Regulatory compliance, code that provably never leaves, audit trail | On-prem appliance + support + SLA + audit | 9–18 months, vendor-risk gated |
| **Fintech / payments** | The same, with less legacy and more urgency | Appliance or in-region private deployment | Months — the fastest of the serious buyers |
| **Government / ministry** | Sovereignty, accountability, compliance, local presence | Self-hosted appliance + support + SLA | Longest, largest, tender-based |
| **Department / agency unit** | A working setup someone maintains | Appliance or in-region hosting, smaller contract | Medium, budget-holder decides |
| **University / campus** | Multi-user, SSO, teaching material, research freedom | Annual institution licence + training | Annual, procurement-gated |
| **School** | Something turnkey, plus curriculum | Hosted, or a very simple appliance; often grant-funded | Short but tiny budget — volume play |
| **SME** | Working AI coding with no IT department | **Hosted, self-serve, pay as you go** | Immediate, card or mobile money |

Two rows carry more weight than the rest. **SMEs rescue the hosted product** —
they will never run a cluster, so the pooled-credit service in §§3–7 is exactly
right for them even though it is not what a ministry buys. And **banks and
fintech are where the money is**, for reasons that invert several assumptions
made earlier in this document.

### 14.5 Banks and fintech invert the assumptions

§13 was written around institutions with small budgets, slow procurement and
weak infrastructure. Regulated financial institutions are the opposite on every
axis, and they are the best-fitting customer in the document:

- **Regulation makes it mandatory, not preferred.** Central-bank rules on data
  residency and third-party processing mean many banks *cannot* send source
  code to a foreign AI service at all. This is a hard bar, not a preference —
  and the strongest possible version of the §13.1 sovereignty argument.
- **There is no incumbent to displace.** Where regulation blocks the US tools,
  the alternative is not Copilot — it is *no AI coding tooling*. Competing
  against nothing is a far better position than competing with Cursor on
  features.
- **They have money.** Unlike a university, a bank has a real software budget
  and is used to paying six figures for developer tooling.
- **They have infrastructure and know how to buy.** Own datacentres, security
  teams, existing procurement paths for licensed software with support. The
  appliance is the natural shape for them.
- **They have developers at scale.** Core banking, mobile money, payment
  integrations — real seat counts, not a pilot classroom.

**Open source is an advantage here, not a complication.** Banks routinely
require *source-code escrow* so they are not stranded if a vendor fails; an
MIT harness satisfies that by construction. Their security team can audit the
code directly, turning what is normally a six-month third-party-risk blocker
into a selling point.

What they demand on top of §14.1: vendor risk assessment, penetration-test
reports, immutable audit logs of what the AI touched, role-based access and
segregation of duties, strict network segmentation or air-gap, data-loss
guarantees, liability and indemnity terms, and business-continuity commitments.

Three consequences:

1. **Per-seat pricing works here** — the §13.2 objection is an
   education-and-government constraint, not a universal one. Price banks per
   developer at enterprise rates and institutions per site; the products are
   the same, the price metric is not.
2. **Audit logs and RBAC move up the roadmap.** They sit in M4 today (§11.7).
   For this segment they are not a later tier, they are table stakes for the
   first conversation.
3. **Fintech is the better beachhead than banks.** Mobile-money operators,
   payment processors and digital banks have the same regulatory driver with
   less legacy and far shorter cycles. Land fintech first, use it as the
   reference that survives a bank's vendor-risk review.

**The honest obstacle is third-party risk management.** A young company with no
SOC 2, no audited financials and few references will struggle to clear a tier-1
bank's onboarding, however good the product is. Two mitigations: start with
fintechs and mid-tier banks whose process is lighter, and partner with a local
systems integrator who has already cleared vendor onboarding and can carry the
contract (§13.3's partner line, which pays off twice here).

**This is plausibly who pays first.** Universities remain strategically
valuable — adoption, talent, references, teaching — but they are slow and poor.
Financial services can fund the company while education builds the ecosystem,
and §13.4's roadmap ordering holds either way, since both need multi-user,
per-org model routing and the installer before anything else.

### 14.6 The honest risks

- **Some will self-host and never pay.** They will. Universities especially.
  Treat them as references, talent pipeline and credibility rather than lost
  revenue — a share convert the first time something breaks in production.
- **Someone forks it.** Real but rare for infrastructure that needs support;
  the credible threat is a large cloud reselling us, which the commercial-half
  licence choice exists to address.
- **"Why pay if it is free?" will be asked in every deal.** The answer must be
  one sentence and segment-specific — for a ministry, *"because you cannot put
  a download through procurement, and nobody is accountable when it stops";*
  for an SME, *"because you would need an engineer you do not have."* Rehearse
  it; it is the objection that decides deals.

## 15. Is coding a big enough market?

### 15.1 What is actually in this repository is not a coding tool

Read `silkcode/` as an outsider and the coding parts are the smallest part of
it. What is really there is an **agent runtime**: a model-agnostic provider
layer, a tool-calling loop, a permission and approval model
(`permissions.py`), sandboxed execution (`execbackend.py`), session
persistence, skills and memory, and **MCP support** (`mcp.py`) for attaching
arbitrary external tools.

Coding is the first *application* of that runtime, not the runtime itself. The
file, git and shell tools are the coding application; swap them for an
institution's own systems over MCP and the same harness does procurement
review, data analysis, or back-office automation.

So the honest positioning is **a sovereign AI agent platform, starting with
software development** — and every hard part of it (model neutrality, running
on their infrastructure, permissioned actions, audit) transfers to every other
application unchanged.

### 15.2 Broaden the platform, not the pitch

The failure mode is obvious and common: reposition as "an AI platform for
everything," and become vapourware to every buyer. Coding is the right wedge
precisely because it is narrow:

- **It is measurable.** Code shipped, tests passing, review time saved. Most
  agent use cases cannot show that in a pilot.
- **The buyer is identifiable.** A head of engineering exists, has a budget,
  and feels the problem. "AI for the organization" has no such owner.
- **It is already built.** V0.1 works today.

The expansion is **within the account, after delivery** — land a bank's
engineering team on coding, then sell the same platform to their operations
and compliance functions once it is already installed, approved and trusted.
Land-and-expand, not a broader opening pitch.

**MCP is the mechanism** and it is already in the codebase. An institution
connects its own systems as MCP servers and the harness does non-coding work
without us writing a single integration. That is the cheapest possible route
from "coding tool" to "platform," and it is mostly a packaging and
documentation exercise rather than new engineering.

### 15.3 Size it by institutions, not by developers

"Developers × monthly seat price" is the wrong sum for this market and makes
coding look far too small. The right one is **institutions × contract value**.

A single bank on a platform contract is worth several hundred individual
subscriptions, and platform contracts grow inside the account as more
departments adopt. That changes the arithmetic from a thin developer-tools
market into an enterprise-software one — which is what §14.5 is really saying.

Two honest caveats. **Coding alone, in emerging markets alone, is probably not
a venture-scale market** — the platform expansion in §15.2 is what makes the
ceiling high enough, and it should be a deliberate plan rather than a hope.
And **sovereignty demand is not limited to these regions**; European public
sector and regulated industries want the same thing. That is expansion room,
not a reason to lose focus now.

### 15.4 Which other fields, and how to pick them

"Which industries could use an AI agent" is not a useful question — the answer
is all of them. The useful one is which fields share the four properties that
make *this* architecture the right one:

1. **The data cannot leave.** Sovereignty is the whole pitch; a field without
   that constraint is one where we have no advantage over Copilot.
2. **The agent must take actions, not just answer.** Tool calling, permission
   prompts and audit are what we built. If a chatbot would do, we are overkill.
3. **The output is checkable.** This is the strongest predictor of success and
   the most commonly ignored. Coding works for agents because tests pass or
   fail. A field with a verifiable result — the query runs, the plan applies,
   the report reconciles, the clause matches the template — will work. "Write
   me a strategy" will disappoint and burn credibility.
4. **The work is artifact-shaped.** Files in a directory, versioned and
   diffable. The `Workspace` model transfers directly, and version history is
   itself valuable to regulated buyers.

That filter produces three tiers, in order of how little new engineering each
needs:

**Tier 1 — same workspace, different file types.** Barely new domains at all:
data and analytics engineering (SQL, dbt, notebooks), infrastructure and DevOps
(Terraform, Kubernetes manifests, CI configs), security engineering (patching,
detection rules, incident runbooks), and QA automation — `tools/testing.py`
already exists. New prompts and a few tools; the rest is unchanged.

**Tier 2 — documents as artifacts.** New tools, same runtime: regulatory and
compliance reporting (banks file enormous volumes of rule-bound, versioned
documents — plausibly the highest-value non-coding use in a bank), procurement
and tender documents for government and universities, contract review, and
grant writing and reporting for the donor-funded programmes in §13.3. Git
history over documents is a feature to a regulator, not an accident.

**Tier 3 — their systems, over MCP.** Back-office operations, reconciliation
and exception handling, ticket triage, research assistance, public-service
delivery. Highest value and highest stakes, because the agent is acting on live
systems — which is exactly where permissions and audit earn their keep.

**Poor fits, named so they are not drifted into.** Customer-facing chat and
support bots are a different product with different constraints, commoditised
and competitive. Purely generative-creative work has no verification and no
sovereignty driver. Clinical decision support carries regulatory and liability
weight a small company should not take on.

**The selection rule is not "which market is biggest."** It is: *what else does
this buyer, in this account, already own?* Expansion follows the org chart, not
the industry analyst. For a bank: coding → data engineering → infrastructure →
security → compliance reporting → back-office, where the first four report to
the same person. For a university: coding → research computing → grant
reporting → administration. For government: coding → procurement documents →
service delivery.

Every one of these needs the same four things already built — the model on
their infrastructure, permissioned tool calling, an audit trail, and a
versioned workspace. That is what makes this one platform rather than six
products.

## 16. Distribution: getting into institutional hands

We cannot outspend anyone on marketing, and institutional buying in these
markets is relationship-led rather than inbound-led. Distribution therefore has
to be structural, not promotional.

### 16.1 Open source is the marketing budget

`pip install silkcode` is the top of the funnel. A developer inside a bank
tries it on their own machine, likes it, and becomes an internal champion —
who then asks for the version the organization can actually deploy. Bottom-up
adoption creating top-down demand is the only motion that costs us nothing per
prospect.

This makes the free local tool a **distribution asset, not a giveaway**, and it
argues for spending real effort on first-run experience, documentation and
install reliability — the things that decide whether a curious developer
becomes a champion.

### 16.2 Universities are a channel, not only a customer

The most valuable thing a university gives us is not its licence fee. Students
learn on Silk Code, graduate, join banks, fintechs and ministries, and ask for
the tool they already know. That is how MATLAB, SAS, AutoCAD and JetBrains won
their enterprise markets, and it is a two-to-four year pipeline that compounds.

Reframed: **academic licensing is a marketing expense, not a revenue line.**
Price it accordingly — free or near-free, with training and curriculum as the
paid part — and measure it on graduates placed, not fees collected.

### 16.3 Partners carry the contract

Local systems integrators and resellers already have the relationships, the
vendor approvals and the ability to invoice locally (§13.2). Giving them margin
buys distribution we cannot build ourselves, and it solves the vendor-risk
problem in §14.5 at the same time.

Train and certify them so deployments scale without our headcount. A partner
who can install and support it is worth more than an ad campaign.

### 16.4 The champion-to-procurement handoff

**This is the highest-leverage thing in this section, and where most
open-source companies fail.** A champion who loves the tool still has to get it
through their organization, and they cannot do that with enthusiasm alone. They
need artifacts:

- a security whitepaper and architecture overview
- a data-processing agreement template
- a penetration-test summary
- a clear price sheet with the institutional shape (§14.4)
- two reference customers they can name

Build that pack early — it is cheap, it is reusable, and without it every
bottom-up adoption stalls at exactly the moment it was about to become revenue.

### 16.5 The rest of the channel mix

- **Regulator and ministry relations.** Getting onto an approved-vendor list,
  or being referenced in a national AI strategy, outperforms any amount of
  advertising in this market.
- **Developer communities.** Local meetups, hackathons and training networks
  are strong, underserved and high-trust. Cheap, and they feed §16.1.
- **Industry bodies.** Banking and fintech associations, and national research
  and education networks, are where institutional buyers already cluster.
- **Proof over promotion.** A live demo running against *their* model on
  modest hardware persuades this buyer more than any deck. Reference logos are
  the dominant buying signal — the first flagship customer is worth
  disproportionate effort and discount.

### 16.6 What not to do

Do not buy ads against Copilot or Cursor keywords; we lose that auction and the
comparison. Do not lead with "cheaper than Copilot" — it invites a price war
and ignores that for many of these buyers the US tools are not an option at all
(§14.5). Do not pitch the platform before delivering the wedge. And do not
launch a public hosted service to "get traction" before an institution has
paid — §13.4 already sequenced that, and marketing pressure is exactly what
tends to reverse it.

## 17. Open questions

- **Warm pool from day one?** Cold Pod plus clone is seconds. The pool is the
  fix, but it means paying for idle capacity on our own nodes — a direct cost
  we now carry rather than a vendor's per-second billing problem.
- **gVisor compatibility surface.** M1 must measure this, not assume it.
  Specifically: `npm install` wall-clock versus `runc`, and how many real
  projects need Docker-in-Docker (which `runsc` cannot give them at all).
  This is a gross-margin input as well as a technical one (§12.3).
- **Pricing inputs.** Credit-per-token rates, the seat price, and whether a
  free tier can exist at all are all blocked on live provider rates plus Gate
  2's measured cost per session. Nothing in §12 should be committed to a
  public price page before those land.
- **Which order do we build in?** §13.4 sets out two roadmap orderings — the
  self-serve one in §11 and the institutional one. They diverge at the very
  first milestone, so this needs deciding before M0 starts, not during.
- **Can we collect money?** Which target countries first, what payment rails
  work there, and do we need a local legal entity or a partner of record to
  invoice at all (§13.2)? This gates revenue independently of anything
  technical and should be answered by someone this month.
- **How offline must the appliance be?** Fully air-gapped, or merely tolerant
  of bad bandwidth during install and updates? The answer changes packaging,
  licensing enforcement and the update mechanism (§13.4).
- **Node pool capacity planning.** Sessions per node, headroom for spikes, and
  what happens when the pool is full — queue the session, autoscale, or
  overflow to a second placement driver?
- **Repo size.** `MAX_SYNC_BYTES` caps the local sandbox at 100 MB compressed;
  a cloud clone has no such limit but does have a clone-time cost. Shallow
  clone by default?
- **Data handling.** Pooled keys mean user code transits Silk's gateway to
  third-party providers. This needs an explicit, published statement about
  retention and provider training policies before launch, not after.
- **Licence for the commercial half.** §14.3 settles *where* the open-core line
  falls; it does not settle what licence sits on the closed side. Permissive,
  copyleft and source-available behave very differently when a large cloud
  decides to resell us. Decide before `silkcloud` has much code in it — moving
  it later is a re-licensing argument nobody enjoys.
