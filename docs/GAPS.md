# Gaps

What is still missing for a core that can be dropped into any host, run any operational or
administrative function, and be audited end to end.

Nothing here is designed yet. Each item says what breaks without it and roughly what it
costs. Ordered by what blocks what, not by difficulty.

**How to read this.** Bucket A blocks shipping anything. Bucket B blocks *operating* what
you shipped. Bucket C blocks the enterprise and manufacturing sale specifically. A gap in a
later bucket is not less real — it is less urgent.

---

## Bucket A — blocks a working product

### A1 · Per-session concurrency control — **RESOLVED, scheduled in F2 (D19)**

> Decided: coalesce messages inside a debounce window, serialize per session. DBOS supplies
> both primitives natively (`partition_concurrency=1` with a session partition key, plus
> `delay_seconds` and `deduplication_id`), so this is configuration rather than code.
> Tasks `t-f2-03` … `t-f2-09`. The description below is kept for the reasoning.

Two messages arrive for the same session at the same time. Today the behaviour is
**undefined**: two turns run concurrently against the same conversation, both load the same
history, both append, and the second silently overwrites part of the first's context.

This is a correctness bug, not a performance concern, and it is the only item on this page
that can corrupt data.

OpenClaw solves it with a lane-aware queue: **session lane concurrency 1**, strict
serialisation, with a global lane at 4 and a sub-agent lane at 8. Queue modes decide what
happens to the second message — `collect` (coalesce), `steer` (interrupt), `followup`.

We need the same. DBOS queues can provide it, so the cost is mostly deciding the *policy*:
does a second message wait, merge into the running turn, or interrupt it?

**Without it:** conversations corrupt under normal use as soon as a user double-sends.

### A2 · Idempotency at the HTTP edge

A client retries `POST /turns` after a timeout. Two turns start. The user is answered twice
and billed twice.

Needs an `Idempotency-Key` header, a short-lived key table, and the same-key-same-response
rule. Small — a few hundred lines — but it must exist before anything real calls the API.

### A3 · Session lifecycle policy

`ContextEngine.on_session_end` exists and **nothing ever fires it**. So sessions never end,
conversations grow forever, and compaction runs on a history that should have been closed
days ago.

OpenClaw resets daily at 04:00 local, configurable, plus an optional idle window. Some
policy is required; the exact one is a product decision.

**Without it:** every conversation is one infinite session and cost per user climbs
permanently.

### A4 · Streaming to the user

Everything today is 202-plus-poll. For any chat product you need token streaming over SSE or
WebSocket — a whole new driving adapter.

It also carries a verified trap: **a deferred tool call never reaches
`event_stream_handler`**, because deferred means not executed and only executing tools emit
events. Pause context arrives on separate events. Without knowing that, you will hunt a
phantom bug in the UI.

**Without it:** the product feels broken next to any competitor, even when it is correct.

> **Narrowed by D23.** The 202-plus-poll model was also flagged as unable to serve
> WhatsApp Cloud API or Telegram Bot API, which only push webhooks and never poll us back.
> That part is now designed: outbound delivery of a finished `TurnResult` goes through a
> channel registry plus one `_step_deliver` step (D23), tasked as `t-f3-06`…`t-f3-09`. What
> remains genuinely undesigned here is real-time token streaming for an interactive client
> (own app / browser), and per-channel delivery retry and idempotency semantics for
> `_step_deliver` itself — a retried DBOS step must not double-send a message to a channel
> that has no dedup of its own.

### A5 · Human handoff and escalation — **DEFERRED TO D2**

> Decided: not in the Day-1 scope. It is still table stakes for customer service, so it
> ships with D2 rather than being dropped. Note the consequence: until D2, an agent that
> cannot resolve a conversation has no exit, and the fallback is whatever the channel
> already offers outside the agent.

Distinct from approval. Approval is *per tool call*; handoff transfers **the whole
conversation** to a human operator, who then answers as themselves while the agent stops.

For customer service this is table stakes — an agent with no exit is an agent that traps
angry customers.

Needs: a `HANDOFF` transcript entry kind, a suspended-forever turn state, an operator
takeover endpoint, and a rule for whether the agent resumes afterwards.

### A6 · The fake model provider

Tracked as `t-f0-01`, listed here because it gates everything else. Hermes shipped ~3,821
test files and not one reusable fake; the `Hermes-Core` extraction had to build one before
anything could be tested at all.

---

## Bucket B — blocks operating what you shipped

### B1 · Profile and prompt versioning — **RESOLVED, scheduled in F1 (D20)**

> Decided: profiles carry a version, every turn persists `profile_version` plus the resolved
> snapshot, and old versions stay readable. Tasks `t-f1-17` … `t-f1-19`. Pulled into F1
> rather than left in Bucket B, because it cannot be filled retroactively — the turns are
> already written by then.

`AgentProfile` carries the persona, the budgets and the approval rules. Change the persona
and behaviour changes. **Today nothing records which profile version served a given turn.**

So six months from now, asked *"why did the agent say that?"*, the audit shows the tool
calls and the decisions but not the instructions in force at the time. The record answers
*what* it did and not *why it was allowed to*.

Fix: version profiles, store `profile_version` on every turn, keep old versions readable.
Cheap now, impossible retroactively.

This one matters most given how much weight the design puts on auditability.

### B2 · Rate limiting and quotas

`BudgetState` bounds ONE turn. Nothing stops a caller from starting ten thousand turns.

Needs per-tenant and per-caller limits on turns per minute, concurrent turns, and spend per
day. Day 2's LiteLLM proxy supplies *spend* enforcement via virtual keys; turn-rate limiting
is ours.

### B3 · Telemetry, which is not audit

`AuditSink` is for compliance: append-only, retained, legally readable. Operating the system
needs something else entirely — traces (OpenTelemetry), metrics, structured logs.

The metrics that actually matter here: turn latency p50/p99, tool failure rate by tool,
compaction frequency and how much it freed, cost per tenant per day, suspended-turn age.

Conflating the two is discovered at the worst moment: either your audit is polluted with
debug noise, or your dashboards are missing because someone treated audit as telemetry.

### B4 · Secrets management

Provider API keys, MCP server credentials, database credentials. Completely undesigned.
Environment variables work for one deployment and stop working the moment there is a second
tenant with its own keys. Enterprise will require a vault.

### B5 · Graceful degradation

The model provider is down. Today the turn fails and the user sees an error.

Needs a decision: queue and retry later, fail over to a cheaper model, or answer with a
holding message. This is a product decision with a technical consequence, and it interacts
with `classify_error`.

### B6 · Retention and lifecycle for audit data

Audit tables are append-only and grow forever. Transcripts hold reasoning traces, which are
bulky and contain discarded speculation about users.

Needs a per-table retention window, an archival path, and a documented legal basis for
whatever is kept. `t-f10-05` covers the reasoning window only.

### B7 · Error classification

Tracked as `t-later-03`. Five strategies, not Hermes' twenty-five reasons. Becomes urgent
the moment a second provider or an aggregator is in play.

### B8 · Cost attribution per tenant

`AuditSink.record_turn_end` stores cost per turn. Turning that into per-tenant billing needs
aggregation, a rating model, and a reconciliation path against the provider's own invoice —
which will not match exactly.

### B9 · A profile has no seat for a model setting

`AgentProfile` carries `max_iterations` and `max_cost_usd` — the run's ceilings — and
nothing that reaches the PROVIDER: no `max_tokens`, no `reasoning_effort`, no temperature.
`ModelFactory` is `(model_id, base_url) -> Model`, so there is no seat on that path either.

Found while closing `t-f0-04`'s day-1 endpoint. `tests/integration/test_f0_end_to_end.py`
had been exercising MiniMax M3's two reasoning modes by injecting its own factory carrying
`OpenAIChatModelSettings`; once the test stopped injecting a factory — which is what proves
production can call a model at all — there was nowhere for that setting to come from. The
parametrization was dropped rather than faked, and `docs/FIELD-NOTES.md` keeps what M3
actually does in both modes.

The cost is small and the design question is not: a setting belongs to the profile, so it
has to be a domain field, and adding one means deciding which settings are portable across
providers and which are one provider's dialect. Putting the dialect in the domain is how
`AgentProfile` becomes a passthrough for whatever the current provider accepts.

**Without it:** every profile runs on provider defaults, `max_tokens` included, and the
only ceiling on a runaway generation is `max_cost_usd` — which Pydantic AI can only enforce
when pricing data exists for the model, and warns rather than fails when it does not.

---

## Bucket C — blocks the enterprise and manufacturing sale

### C1 · A documented, versioned public API

For *"insertable anywhere"* this is the requirement, not a nicety. The `Hermes-Core`
extraction recorded this exact gap upstream: **no stable public API, internal import paths
explicitly disclaimed**. That is why the extraction had to define its own surface.

Needs: an explicit export surface, semantic versioning, a deprecation policy, and a decision
on how a host embeds the core — library, container, or both.

### C2 · Real multi-tenancy beyond a column

`tenant_id` exists on the domain types. Actual multi-tenancy needs tenant provisioning and
teardown, per-tenant profiles, per-tenant knowledge collections, per-tenant peer
directories, and isolation tests that prove the boundary rather than assuming it.

### C3 · RBAC beyond caller-versus-admin

Today there are two identities: `CallerIdentity` and `AdminIdentity`. A manufacturer will
want supervisor, auditor, knowledge editor, and operator as distinct roles — with the auditor
able to read every transcript and change nothing.

### C4 · Enterprise authentication

SSO via SAML or OIDC for the admin and transcript surfaces. Nothing designed. It is usually
a procurement checklist item rather than an engineering preference.

### C5 · PII detection and redaction at ingress

Both a compliance requirement and an injection control. Detecting and tagging personal data
as it arrives lets you redact it from prompts, from telemetry, and from what crosses to a
model provider.

Interacts directly with the `MediaPolicy.delivery` decision: bytes versus signed URL only
matters because evidence can contain personal data.

### C6 · Data residency and regional deployment

Enterprises ask which region processes their data and which region stores it. That is an
architectural constraint on the model provider, the database, and the media store all at
once — cheap to design for now, expensive to retrofit.

### C7 · On-premise and air-gapped operation

Manufacturers frequently require it. That means local models through Ollama or vLLM (LiteLLM
already covers both), no external calls, and a knowledge base with no cloud dependency.

Worth confirming as a target *before* anything assumes internet access, because that
assumption spreads quietly.

### C8 · Compliance evidence

Data-flow documentation, retention policies, subprocessor lists, and whatever the customer's
security review asks for. Not code, but it blocks contracts, and the audit design already
built for it is the reason this is a document rather than a rebuild.

---

## The three that deserved attention first — all three now decided

| Gap | Decision | Where |
|---|---|---|
| A1 per-session concurrency | Coalesce inside a window, serialize per session. DBOS partitions do it natively. | D19 → F2 |
| B1 profile versioning | Versioned profiles, `profile_version` on every turn. Pulled forward. | D20 → F1 |
| A5 human handoff | Deferred to D2 — still table stakes, not Day-1 scope. | D2 |

**Next three to look at**, now that those are settled:

1. **A3, session lifecycle.** `on_session_end` exists and nothing fires it, so every
   conversation is one infinite session and cost per user climbs permanently.
2. **A2, HTTP idempotency.** Small, and it must exist before anything real calls the API.
3. **B3, telemetry.** You cannot operate what you cannot see, and audit is not telemetry.

## What is deliberately not here

Fine-tuning, evaluation harnesses, prompt A/B testing, and agent self-improvement. All
useful, none of them core: they belong to whoever operates a specific vertical, and putting
them in the core is how a core becomes a platform nobody can embed.
