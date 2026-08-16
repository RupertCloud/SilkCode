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

## 1. The three decisions this design rests on

| Decision | Choice | Consequence |
| --- | --- | --- |
| **Model access** | Managed pooled credits — Silk holds the provider keys, users spend Silk credits | Zero-config onboarding; Silk carries inference cost, abuse risk, and provider rate limits |
| **Runtime** | Full cloud workspace — repo, agent loop, file edits and commands all run in a per-session cloud container | True zero-install; works from a phone; the browser is a thin client |
| **Codebase** | One agent, two deployments — `silkcode` stays the pip-installable local harness, cloud imports it | No fork, no drift; local users keep BYO-key freedom |

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
│ Runner container  (one per session, disposable)          │
│  · git clone of the user's repo  ← source of truth       │
│  · `silkcode gui` — the code in this repo, unchanged     │
│  · SILKCODE_HOME=/workspace/.silkcode                    │
│  · no provider API keys, no long-lived GitHub token      │
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

## 7. Isolation: where the container runs

| Option | For | Against |
| --- | --- | --- |
| **Fly Machines** *(recommended)* | Real VMs, ~300 ms boot from snapshot, per-second billing, persistent volumes, straightforward egress control | Another vendor; capacity planning is yours |
| **Cloudflare Containers** | You already ship a Worker for the sandbox protocol; edge-native | Constrained runtime and image support; less suited to a full dev toolchain |
| **E2B / Modal / Daytona** | Purpose-built agent sandboxes, fastest to a demo | Margin stacking, less control over egress, vendor risk on a core dependency |
| **Own K8s + gVisor/Kata** | Cheapest at scale, total control | Slowest to build; you own the isolation bugs |

Recommendation: **Fly Machines** for the runner, with the container image built
from the toolchains the target languages need. Keep the existing Cloudflare
Worker as the reference implementation of the sandbox protocol for local users
— it is a different product surface, not a competing one.

**Ephemeral by default.** Clone fresh from GitHub at session start, work, and
push to a branch. Durability lives in the user's git remote, not in Silk's
storage. This is how the existing GitHub integration already thinks
(`silkcode/github.py`, auto-push, PR creation), it dodges a large class of
backup and privacy problems, and it makes container loss a non-event. Offer
persistent volumes later as an opt-in for slow-to-restore workspaces.

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

---

## 11. Build order

Each phase is independently shippable.

**Phase 0 — Gateway only.** Build the metered OpenAI-compatible gateway; no
containers, no control plane beyond accounts and a ledger. Local CLI users
point `base_url` at it and buy credits. Ships the pooled-credit business model
and the auto-router without any orchestration work, and de-risks the hardest
correctness problem (transparent tool-call streaming) first.

**Phase 1 — Runner image.** Container that clones a repo, runs `silkcode gui`
against the gateway, and exits when idle. Driven by hand. Proves the agent runs
unchanged in a container.

**Phase 2 — Control plane.** GitHub web OAuth, session records, start/attach/
reap orchestration, authenticated proxy to the container. Opaque session ids
and the durable `SessionStore` land here. This is the first end-to-end hosted
product.

**Phase 3 — Commerce and safety.** Billing, plan limits, per-session caps,
egress allowlist, idle reaping, abuse signals. Also the mid-turn
credits-exhausted stop generalized out of `swarm.py`.

**Phase 4 — What hosting unlocks.** Async tasks that run without a browser
open, PR-first workflows, mobile, shared team workspaces, org-level model
policy. These are the features a local-only harness cannot have, and the reason
to host at all.

## 12. Open questions

- **Per-turn latency budget.** Cold container boot plus clone is seconds; is
  that acceptable per session, or is a warm pool needed from day one?
- **Repo size.** `MAX_SYNC_BYTES` caps the local sandbox at 100 MB compressed;
  a cloud clone has no such limit but does have a clone-time cost. Shallow
  clone by default?
- **Data handling.** Pooled keys mean user code transits Silk's gateway to
  third-party providers. This needs an explicit, published statement about
  retention and provider training policies before launch, not after.
- **Open-source boundary.** Does `silkcloud` stay MIT alongside `silkcode`, or
  is the control plane the proprietary part? This decision shapes how much of
  the above can live in this repository.
