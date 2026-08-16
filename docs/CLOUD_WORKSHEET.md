# Silk Cloud — starting worksheet

Companion to [CLOUD.md](CLOUD.md). That document is the strategy; this one is
what to do about it first.

Nothing here needs engineering. These are the decisions and facts everything
else is waiting on. Parts A and C are one sitting; part B is a week of
legwork. **Fill it in and commit it** — the answers are more useful in git
than in a chat log.

Owner: ______________  ·  Date: ____________

---

## Part A — Five decisions only you can make

Each blocks work downstream. A recommendation is given; disagreeing is fine,
deciding is the point.

### A1. Who do we sell to first?

Fintech · bank · university · government · SME. One, not a list.

> **Suggested: fintech.** Same regulatory driver as a bank, far less legacy,
> months rather than years — and it becomes the reference that survives a
> bank's vendor-risk review (CLOUD.md §14.5).

**Answer:**

### A2. Which one country?

Payment rails, legal entity, procurement rules and relationships are all
country-specific. Two countries doubles the work before anything is proven.

**Answer:**

### A3. Which build order?

Institutional first (multi-user → their model → installer → paid pilot), or
self-serve first (credit gateway → public sign-up)? These diverge at the very
first milestone (§13.4).

> **Suggested: institutional first.** It matches A1, and the self-serve
> product needs billing, warm pools and abuse defence that a pilot does not.

**Answer:**

### A4. How offline must it run?

Fully air-gapped, or merely tolerant of bad bandwidth during install and
updates? Changes packaging, updates and licence enforcement.

**Answer:**

### A5. What licence goes on the paid half?

The harness stays MIT — settled (§14.3). This is the control plane and
gateway: permissive, copyleft, or source-available. They behave very
differently if a large cloud resells us. Decide before that code exists.

**Answer:**

---

## Part B — Five facts to go and find

Do not guess these. Each is a phone call or a meeting, and each can
invalidate a plan.

### B1. Can we actually get paid?

In the A2 country: how does an institution pay a software vendor — bank
transfer, mobile money, purchase order? What do they need from us to raise
one?

> **The most under-estimated item in the whole plan.** If we cannot collect,
> nothing else matters.

**Found:**

### B2. Do we need a local company?

Many institutions can only pay a locally registered, tax-registered vendor.
Cost, time, and whether a partner-of-record is faster.

**Found:**

### B3. What does their procurement demand?

Ask one target institution for their vendor-onboarding pack — security
questionnaire, insurance, references, certifications. Get the real document.

**Found:**

### B4. Who are the three local integrators?

Firms already approved to sell into our target institutions. They have the
relationships, the approvals and the ability to invoice; margin buys us all
three (§16.3).

**Found:**

### B5. What do the models cost today?

Current published prices per million tokens for the providers we route to.
Needed before any price sheet exists, and they change often — use live
numbers, not remembered ones.

**Found:**

---

## Part C — Three proofs (ninety days)

Each has a yes/no answer. None of them is a demo.

- [ ] **One named design partner** — a real institution, a named person,
      willing to pilot and be referenced. Written down, not implied.
- [ ] **One invoice paid** — any amount, end to end, in the A2 country.
- [ ] **One deployment on their own model** — their hardware, their endpoint,
      real work. Silk Code can nearly do this today.

> **If only one can happen, make it the invoice.** The technical proofs are
> far likelier to succeed than the commercial one, and discovering the payment
> problem after six months of building is the expensive way to learn it.

---

## Part D — Smallest slice that makes a pilot possible

Everything one institution needs to run it. Nothing else.

- [ ] **Per-agent tool sets.** `tools/__init__.py:46` holds `TOOLS` as a
      module-level global, so every agent in the process shares one tool set.
      Making it per-agent is an afternoon now and a refactor later — and it is
      what makes anything beyond coding possible (§15.2).
- [ ] **Multiple users, one install.** Sign-in and separate workspaces;
      currently single-user.
- [ ] **Organisation model endpoint.** Set once, for everyone — the
      `principal → upstream` resolution in §5.
- [ ] **Audit log.** Who asked for what, which tool ran, what changed. Table
      stakes for a bank's first conversation, not a later tier (§14.5).
- [ ] **An installer that works on a bad connection.** A failed install kills
      a pilot before the product is ever judged.
- [ ] **The procurement pack.** Security overview, data-agreement template,
      price sheet, two references. Cheap, reusable, and without it every
      champion stalls exactly when they were about to become revenue (§16.4).

**Deliberately not yet** — so these do not eat the pilot: credits, billing and
public sign-up; warm pools, autoscaling and abuse defence; SSO, teams and org
policy beyond one model endpoint; any non-coding domain; a second country.

---

## Part E — Four numbers, weekly

Five minutes to update. If one has not moved in a month, the problem is the
plan rather than the effort.

| | This week |
| --- | --- |
| Institutions in real conversation | |
| Developers who installed it | |
| Money collected | |
| Weeks to first pilot live | |
