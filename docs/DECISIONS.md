# Decisions

Every decision here carries the evidence that produced it. Where a claim was measured,
the measurement is named. Where it was verified against documentation, that is said.

---

## D1 · Build our own core instead of extracting Hermes'

**Decision.** Write a thin core we understand, orchestrating solved components,
instead of continuing from the extracted Hermes core.

**Evidence.** Measured on the clone at `../Hermes-Core/Hermes`:

- The real intelligence core is **103 modules — 6.8% of the repository**. The rest is
  product, not engine.
- Following only the loop's imports, `tools/` collapses **from 37 modules to 5**. The
  tool machinery is tiny; the bulk is the tools themselves.
- The whole repository is ~1.72M Python lines, ~704K excluding tests.

**Reasoning.** Hermes is not badly built. 93% of its mass solves problems that are
bought solved today: durability, provider quirks, transport, channel connectors. The
core does not compete with Hermes — it competes with the glue Hermes had to hand-write
in 2023.

**Consequence.** The extracted core at `../Hermes-Core/Core` (245 modules, 112,973
lines) is not the basis for this repo. It becomes the **field reference**.

---

## D2 · No agent framework

**Decision.** No LangChain, no LangGraph, no CrewAI. Pydantic AI as the runtime.

**Evidence.** Two independent products converged on the same choice:

- **Hermes** (Python): only `openai` as a hard LLM dependency, used as a typed HTTP
  transport. `anthropic` is an optional extra. Providers are declarative
  `ProviderProfile` plugins, 61–207 lines each.
- **OpenClaw** (TypeScript, Node 24+, pnpm monorepo): no agent framework either.
  Abstracts by API adapter — `openai-completions`, `anthropic-messages`,
  `google-generative-ai`, `ollama`, `bedrock-converse-stream`.

Both abstract **by wire protocol, not by SDK**. Verified against the OpenClaw
repository and official docs.

**Reasoning.** A framework can only supply the 6.8% that is already easy. The
remaining mass is not framework-shaped.

---

## D3 · Pydantic AI over the Claude Agent SDK

**Decision.** Pydantic AI as the `AgentRunner` adapter.

**Reasoning.** The codebase is Python, typed and hexagonal; the runtime must be
provider-agnostic. The Claude Agent SDK is stronger for coding agents — same loop as
Claude Code, file access, shell, subagents — but binds to one model family, which buys
nothing here.

**Reverse this decision if** the product turns out to be a coding agent. Then the
Claude Agent SDK's calibrated loop is worth the binding.

**Selection criterion applied.** Before committing to any agent SDK, one question
decides it: *can I put my own code in the middle of a turn?* Concretely — can state be
persisted **before** the first side effect, and can execution be intercepted between
"the model asked for the tool" and "the tool runs"?

Pydantic AI answers yes, verified in its documentation. Hooks exist at seven lifecycle
stages, each with `before_` / `after_` / `wrap_` / `_error` variants:

```
run → node → model_request → tool_validate → tool_execute
    → output_validate → output_process
```

`before_tool_execute` receives validated args, may modify them, and may block
execution entirely by raising `SkipToolExecution(result)`. That is the approval gate
without writing an approval gate.

---

## D4 · DBOS for durability

**Decision.** DBOS Transact as the durability layer, entering as a driving adapter.

**Evidence.** Verified at `dbos-inc/dbos-transact-py`: MIT-licensed, on PyPI, runs
**entirely inside the application process** — no external server. Checkpoints workflow
state in Postgres via `@DBOS.workflow()` / `@DBOS.step()` decorators. Provides durable
notifications and sleep primitives.

**Reasoning.** Approvals that take hours or days need a durable wait, not a held
thread. This composes exactly with Pydantic AI's deferred tools, which **end the run**
rather than blocking when approval is required.

Without it, "wait three days" means hand-building a state machine, a pending table, a
sweeper job, and idempotency logic so that resuming does not re-run side effects.
Hundreds of lines, all with subtle bugs.

**Temporal was considered and rejected for now** — more powerful, but needs a separate
server and workers. Revisit if DBOS proves insufficient for orchestration.

---

## D5 · LiteLLM as a library on day 1, proxy on day 2

**Decision.** Start with LiteLLM imported in-process. Move to a proxy when
multi-tenancy is real.

**Evidence, verified in LiteLLM's documentation.** Its database is **optional**:

| Without a database | With a database |
|---|---|
| OpenAI-compatible API | + virtual keys |
| Routing and failover between providers | + budgets per key / team / user |
| All via `config.yaml` | + spend tracking and admin UI |

Without a DB, a request carrying a virtual key fails with `"No connected db."` and **no
budget enforces anything**.

Provider coverage verified: OpenRouter, Moonshot AI (Kimi), Z.AI (Zhipu/GLM), MiniMax,
DeepSeek, xAI, Groq, Together, Fireworks, Nebius, Volcengine, Baseten, Ollama, LM
Studio, vLLM — 100+ total, plus a JSON form for any OpenAI-compatible endpoint.

**Consequence for infrastructure.** Day 1 is **one process and one Postgres instance
with two logical databases** (app, dbos). Day 2 adds a third database in the same
instance for LiteLLM. A second Postgres instance is never required by the design.

**Caveat that becomes real later.** "Supported" means the request format is
translated. It does **not** mean the failure modes are normalized. An aggregator's
upstream 429 and your own key's 429 arrive as the same 429, and the correct response is
opposite: one needs a different model, the other a rotated credential. That is why the
error classifier is a written component, scheduled late.

---

## D6 · The DBOS workflow is a driving adapter

**Decision.** Never decorate a use case with `@DBOS.workflow()`. The workflow lives in
`adapters/driving/workflow/` and calls use cases as steps.

**Reasoning.** Decorating the use case puts infrastructure in the application layer:
tests then require Postgres, and the use case is married to an orchestration engine.
A DBOS workflow is a driving adapter in exactly the sense a FastAPI router is one.

---

## D7 · `AgentRunner` is a test seam, not a portability layer

**Decision.** The port exists so use cases and tests do not import Pydantic AI. It does
**not** exist to allow swapping frameworks.

**Reasoning.** That portability is not real. Chasing it produces an inflated
abstraction that reimplements half a framework. The port therefore deliberately
mirrors Pydantic AI's own contract shape.

**Early warning signal.** If the adapter starts reimplementing message handling or
retries, the port has grown too large and must be trimmed.

---

## D8 · Compact rarely and in large steps

**Decision.** High compaction trigger (~70–80% of window), aggressive target, never per
turn.

**Evidence.** Hermes ships a per-exchange micro-compaction mode that is **off by
default**. Its source states the reason verbatim:

> every pass rewrites the prompt prefix and breaks the provider prompt cache

**Reasoning.** Compacting often costs more than it saves. Rewriting old messages
invalidates the cached prefix, so the next request re-bills the whole prompt at full
price. This is the classic first-implementation mistake: enable it per turn, watch the
bill rise, fail to connect the two.

**Related evidence.** Hermes' compaction family totals 11,003 lines across 8 files —
`context_compressor.py` alone is 4,918. The ladder here is budgeted at ~750 and that
budget is the discipline, not an aspiration.

---

## D9 · `HumanGateway`, not `ApprovalGateway`

**Decision.** One port covers approvals, evidence requests and any mid-turn human
interaction.

**Evidence.** Pydantic AI's deferred tools cover **two cases**: tools requiring
approval and tools executed externally. Both end the run and return
`DeferredToolRequests`.

**Reasoning.** "Ask the user for a photo" therefore needs no new machinery — it is an
externally-executed tool travelling the same path as an approval. The right port name
describes the question it answers, not the first use case that motivated it.

---

## D10 · MCP composes into `ToolProvider`; it is not its own port

**Decision.** `MCPToolset` is composed inside the `ToolProvider` adapter alongside the
vertical's local tools.

**Reasoning.** This keeps the contract intact: eleven ports at the time this was written,
fifteen now, and still only two changing
per vertical. Skills and MCP enter as data and composition, not as new code.

**Non-negotiable constraints.** MCP tools pass through the same `ToolPolicy` as local
ones, their results are wrapped in untrusted-content delimiters, and they get a smaller
result budget. Hermes uses 50K against 100K characters, stating that MCP servers
routinely return un-paginated 20–50K payloads.

---

## D11 · The first vertical is delivery, not fraud

**Decision.** F4 implements the delivery optimizer.

**Reasoning.** Short turns, bounded domain and simple approvals exercise the entire
core without regulatory weight. Fraud's domain complexity would mask architectural
errors. F4 exists to prove the port contract holds; it should not be the hardest
domain available.

---

## D12 · Repository documentation is written in English

**Decision.** Repo docs, code, identifiers and comments in English. Conversation stays
in Spanish.

**Reasoning.** Matches the convention already used in the sibling `Hermes-Core/Core`
project, whose `README.md` and `EXTRACTION.md` are in English.

---

## D13 · Async end to end, with two deliberate sync islands

**Decision.** The system is asynchronous. `domain/` stays synchronous, and Postgres
transactions stay synchronous behind `asyncio.to_thread`.

**Why asynchrony is not optional.** A turn can suspend for three days waiting on a human.
No HTTP request survives a weekend or a deploy, so `POST /turns` returns 202 with a
`turn_id` and the turn runs behind it. The asynchrony comes from the human-in-the-loop
requirement, not from a throughput preference.

**Why async all the way through the process.** Both ends are already async — FastAPI is
ASGI, Pydantic AI is async-native (`run()` is a coroutine, `run_sync()` is the wrapper),
LiteLLM and psycopg 3 both offer async, and DBOS supports coroutine workflows and
coroutine steps (verified). A synchronous middle would mean bridging twice instead of
zero times, and the event-loop-to-thread bridge is where deadlocks live.

**Island 1 — `domain/` is sync.** There is no I/O to await. Frozen dataclasses and pure
predicates. Not a compromise.

**Island 2 — DBOS transactions are sync.** Verified in the DBOS docs: *"DBOS does not
support coroutine transactions. To execute transaction functions without blocking the
event loop, use `asyncio.to_thread`."* Repositories under `adapters/driven/persistence_pg/`
using `@DBOS.transaction` are synchronous and wrapped accordingly. This is an engine
constraint, not a choice.

**What async does NOT buy here.** The long human waits cost nothing in either model,
because DBOS makes them durable — a suspended turn holds no thread, no connection, no
memory. Only turns *actively* calling a model matter for concurrency, and those are tens,
not thousands. This is a direct payoff of D4.

**The constraint async introduces.** See CLAUDE.md non-negotiable #7. `asyncio.gather` in
a workflow body is valid *only if steps are started in a deterministic order*. The case
where it bites: the model requests several tools in one round and they run in parallel.
If start order depends on a set, an unordered dict, or which future resolved first, replay
after a crash takes a different path — and nothing fails a test.

**Consequence for `ToolPolicy`.** `decide()` runs on every tool call inside
`before_tool_execute`. A database round trip per call adds network latency to every tool.
The port is therefore split: `load_rules()` is async and runs once per turn, `decide()` is
synchronous and pure over the cached rules.

---

## Standing user preference (carried from the Hermes-Core work)

**Strict sequencing, no parallel work.** Deliver a piece finished and in isolation
before starting the next one. Integration into a consuming system begins only on an
explicit go-ahead.

Recorded during the Hermes core extraction, when parallel work on the extraction and
the Campus-Alert integration was explicitly stopped.

**Amended 2026-09-10 — scoped to workstreams, not to tasks within a phase.** The user
directed that implementation run as parallel waves of subagents: tasks whose dependencies
are satisfied and whose written files are disjoint execute concurrently, wave by wave,
with a barrier between waves. The original preference still holds at the level it was
recorded at — one workstream finished before the next begins, and integration into a
consuming system still waits for an explicit go-ahead. What is now permitted is
concurrency *inside* a single phase of a single workstream.

Two constraints make that safe, and both are load-bearing:

- No two tasks in one wave may write the same file. The wave schedule is derived from a
  `writes` set per task, not from the order tasks appear in `docs/TASKS.md`.
- Every task is test-first. A wave agent writes the failing test, confirms it fails for
  the intended reason, then implements. A test that passes before implementation is a
  broken test and stops that task.


---

## D14 · Skills now, KnowledgeBase foundations, RAG later

**Decision.** Day 1 ships Skills plus a `KnowledgeBase` port running `FULL_TEXT`. No
embeddings, no vector index, no pgvector. `SEMANTIC` and `HYBRID` exist in the port
signature only.

**Reasoning.** For a corpus under ~20–30K characters, a model reading the whole document
has **zero retrieval error**. Adding embeddings there buys nothing and introduces a chance
of fetching the wrong chunk. Paying for vector infrastructure before the corpus needs it is
paying for a defect.

**Why `KnowledgeBase` exists at all on day 1, when Skills would do.** The deciding factor
is not retrieval quality, it is the **update path**. A `SKILL.md` is a file in git; an
administrator changing a price is not going to open a pull request. Prices and hours belong
in a database with an admin surface regardless of their size.

**The line, stated once so it stops being re-litigated.** Skills = *how* to do things
(static, git, engineers). KnowledgeBase = *what* the business offers today (dynamic,
database, administrators).

**When to turn `SEMANTIC` on.** Per collection, when it outgrows `FULL_TEXT`. First move is
pgvector in the existing Postgres instance, not a dedicated vector database: Postgres gives
semantic *and* keyword retrieval in one query, which is what hybrid scoring needs.

**Target scale acknowledged.** Manufacturers and mid-to-large enterprises with varied
protocols, reaching thousands of documents. `SEMANTIC` and `HYBRID` are in the signature
for that reason; the day-1 default does not pay for it.

---

## D15 · The knowledge write path is a separate port

**Decision.** `KnowledgeBase` is read-only. Writes live on `KnowledgeAdmin`, which is never
injected into anything an agent touches. Every write method takes `AdminIdentity`, a type
that cannot be produced from a `CallerIdentity`.

**Reasoning.** This is the strongest available defence against corpus poisoning: **a prompt
injection cannot call a method that is not on the object the agent holds.** Structural, not
conventional.

The alternative — one port with read and write methods plus an `if is_admin` guard — is one
refactor away from being wrong, and when it is wrong nothing fails a test.

It also satisfies the port rule: *"What does the business offer?"* and *"change what the
business offers"* are two questions, and a port answers one.

**Consequence.** Updates create versions and never overwrite. That history answers *"why
did the agent quote the old price on Tuesday?"* — which for pricing is the difference
between a bug report and a dispute.

---

## D16 · A2A: align the shape now, adopt the protocol in D2

**Decision.** Model `AgentMailbox` and the peer domain types on Agent2Agent's concepts —
capability discovery, tasks, modality negotiation — without implementing the wire protocol
on day 1.

**Evidence.** A2A reached v1.0 under Linux Foundation governance, backed by Google,
Microsoft and Salesforce. Adoption is measured rather than universal. Hermes' own A2A
plugin is 2,345 lines.

**Reasoning.** Aligning the shape costs nothing now. Adopting the protocol later then costs
an adapter instead of a redesign. Implementing 2,000+ lines of protocol for a feature with
no users yet is the definition of paying early.

**What is NOT deferred.** The safety controls ship with the foundations, because they are
cheap now and expensive to retrofit: hop limits, a two-sided peer allowlist, visibility
redaction defaulting to `NONE`, and untrusted-content wrapping on peer answers.

---

## D17 · Agent-to-agent reuses the suspension mechanism

**Decision.** `ask_peer` is an externally-executed deferred tool. No new suspension
machinery.

**Reasoning.** Third user of the same pattern, after approvals (D9) and evidence requests.
The answering agent suspends its own turn on `HumanGateway`, giving two nested suspensions
over one mechanism.

**The product consequence, which is the part that gets missed.** Compounding latency means
a customer can be waiting for hours while a chain of agents and humans resolves a question.
So a suspension must be **communicable**: the agent tells its own user it is checking,
before it suspends. A silent suspension looks like a hung system.

---

## D18 · Transcripts store everything and filter on read

**Decision.** One write path. `Audience` decides what a reader sees. `TranscriptReader` is
a read-only projection over rows `ConversationStore` and `AuditSink` already write.

**Reasoning.** An audit that stored a redacted copy cannot answer a question nobody
anticipated. Two write paths drift, and the one that drifts is always the one nobody reads
until an incident.

**The non-obvious requirement.** A user must not learn *which* tool is pending, but must
see *that* something is. Hiding suspension entirely makes the conversation look broken and
the user leaves — so `PENDING_PLACEHOLDER` is part of the user projection.

**Open decision.** Reasoning traces are bulky and contain discarded speculation about the
user. Their retention window is not yet decided — tracked as `t-f10-05`.


---

## D19 · Coalesce messages, serialize per session — DBOS partitions do both

**Decision.** One turn at a time per session, and messages arriving in quick succession are
merged into that single turn.

**Why coalescing and not queueing each message.** People split one thought across sends:
*"hola"* / *"quería preguntar algo"* / *"sobre mi pedido 123"*. Three separate turns would
answer the first two pointlessly and pay for three model calls to produce one useful answer.

**Why serialization is not optional.** Two concurrent turns on one session both load the
same history and both append: the second silently overwrites part of the first's context.
That is data corruption, not a race to tune, and it fires the first time a user
double-sends.

**Verified: DBOS supplies both primitives, so this is configuration, not code.**

| Need | DBOS mechanism |
|---|---|
| One turn at a time per session | `partition_concurrency=1` with `queue_partition_key=<session_id>` |
| Coalescing window | `delay_seconds` on enqueue |
| Second message joins the pending turn | `deduplication_id` with `duplication_policy="return-existing"` |
| Global cap on concurrent turns | `global_concurrency` |
| Per-tenant rate limit | `limiter={"limit": N, "period": S}` |
| Durable timeout | `SetWorkflowTimeout`, persisted across restarts |

**The flow.**

1. A message arrives. Append it to a pending-input buffer row for the session.
2. Enqueue the turn workflow on the partitioned queue with `delay_seconds ≈ 1.5` and
   `deduplication_id = f"turn:{session_id}"`, policy `return-existing`.
3. Further messages inside the window append to the buffer and get the existing handle back —
   no second workflow.
4. When the delay expires, the workflow's **first step drains the buffer** and runs one turn
   with the concatenated text.

**Messages that arrive mid-turn become a follow-up turn**, which `partition_concurrency=1`
serializes behind the running one. That is the wanted behaviour and it needs no extra code.
It is what OpenClaw calls `collect` mode.

**The cost, stated plainly.** A fixed debounce adds its full window to *every* message,
including single ones. Mitigation where the channel supports it: extend the window while a
typing indicator is active and fire immediately when typing stops. Start near 1.5s and
measure — do not tune it by intuition.

**Interruption (`steer`) is explicitly out of scope.** Restarting a turn mid-flight discards
a model call already paid for and may leave tool side effects already applied. Hermes needs
dedicated interrupt machinery for it (`interrupt_control.py`, `interrupt_compat.py`, plus an
interrupt scaffold marker inside its turn loop). Not before there is a demonstrated need.

---

## D20 · Profiles are versioned, and every turn records which version served it

**Decision.** `AgentProfile` carries a version. Every turn persists `profile_version`. Old
versions stay readable.

**Why.** The profile holds the persona, the budgets and the approval rules — change it and
behaviour changes. Without the version on the turn, an audit six months later shows the tool
calls and the decisions but **not the instructions in force at the time**. It answers *what*
the agent did and not *what it had been told to do*.

That is the hole in the property this design cares most about, and it cannot be filled
retroactively: the turns are already written.

**Shape.** `profile_version` is monotonic per profile id, bumped whenever the YAML changes.
Load-time hashing of the file is enough to detect a change; the version and the resolved
profile snapshot are stored so a reader never has to trust that the file on disk today is
the file that ran.

**Consequence for the transcript API.** `GET /turns/{turn_id}` returns the profile version
and can render the persona and rules as they were. A transcript that cannot show the
instructions is an incomplete record.

---

## D21 · No Redis. The reason is atomicity, not throughput

**Decision.** DBOS queues over Postgres. No Redis, no dedicated broker.

**The throughput argument is not the interesting one, but it settles quickly.** A turn is
dominated by a model call of roughly 5–40 seconds. A hundred concurrent turns is about five
enqueues per second; a thousand is about fifty. Postgres handles thousands of small
transactions per second. **The model provider is the bottleneck by orders of magnitude, and
the queue is nowhere near it.**

**The real argument is atomicity.** DBOS keeps queue state and workflow state in the *same*
Postgres transaction: enqueueing and the state change commit together or not at all.

Put the queue in Redis and the state in Postgres and they can disagree after a crash — the
job exists in Redis while the state says it never started, or the reverse. That is
split-brain, and it is precisely the class of bug DBOS was chosen to eliminate (D4).

**So adding Redis would not merely add a process to operate. It would remove the property
DBOS was selected for.**

**The one future case where Redis earns its place.** Pub/sub fan-out for streaming tokens to
browsers across several application instances — gap A4. Even there, Postgres `LISTEN/NOTIFY`
is the first thing to try, with its known limits (payload around 8KB, no persistence,
connection-bound). Redis pub/sub is the second step, and only for that fan-out — never for
the work queue.

**Revisit this decision if** sustained enqueue rates exceed a few thousand per second, or a
non-Python worker must consume the same queue. Neither is on the roadmap.


---

## D22 · The coalescing window is time-based, and resets on arrival

**Decision.** A debounce window that resets on each incoming message, with a maximum total
wait. No dependence on typing indicators.

**This corrects an earlier recommendation.** The design first proposed extending the window
while a typing indicator was active. **That is not possible on the two channels that matter
most**, and the correction is recorded rather than quietly dropped:

- **Telegram Bot API** lets a bot *send* `sendChatAction`. User typing is **not exposed to
  bots** at all — neither by webhook nor by `getUpdates`.
- **WhatsApp Cloud API** lets a business *send* a typing indicator, using the inbound
  `message_id`, for up to 25 seconds. There is **no inbound user-typing webhook**.

Both are webhook push over HTTPS, not WebSockets. Telegram additionally permits long-polling
`getUpdates`, which is the bot pulling rather than the platform streaming.

**The design that works everywhere.**

```
first message  -> start window (~1.5s), buffer it
next message   -> buffer it, RESET the window
                  unless total wait >= max_wait (~6s), then fire now
window expires -> drain buffer, run ONE turn
```

`max_wait` is not optional: without it, a user sending five short messages in a row keeps
postponing their own answer.

**Where inbound typing exists** — an own app, or Chatwoot's own web widget — it may extend
the window as an optimisation. It is never load-bearing.

**Outbound typing is still wired**, for a different purpose: it is the cheapest partial answer
to the communicability requirement in D17. It buys 25 seconds on WhatsApp and about 5 on
Telegram before a running or suspended turn starts looking like a hung system.

**Both numbers are starting points to measure, not settled values.** A debounce adds its
window to every message, including single ones, so it is a latency decision with a real cost.


---

## D23 · Own channel adapters in phase one; delivery is composition wiring, not a new port

**Two decisions, recorded together because the second follows from the first.**

### Ownership: Option A — Agent-Core owns the channel layer in phase one

**Decision.** One driving adapter per channel — WhatsApp Cloud API webhook, Telegram
webhook, own app — built and maintained by Agent-Core. **Option B (Chatwoot as the channel
layer) is rejected for phase one**, not dropped: Chatwoot is deferred to phase two, and when
it arrives it is only a channel adapter, or at most a read-side projection over the
transcript — never a second write path. `ConversationStore` and `AuditSink` remain the only
writers (D18); an AgentBot integration that persisted its own state would be exactly the
two-write-path drift D18 exists to prevent.

**What this keeps deferred.** Human handoff (gap A5) stays deferred to D2, as already
recorded there — Option B would have pulled it forward by adopting Chatwoot's inbox instead
of building handoff ourselves, but not at the cost of a second writer. Every channel is our
integration to build until then.

**What is preserved from Option B's evaluation, should phase two revisit it.** Chatwoot
shows *messages* — no tool calls, reasoning, policy denials, knowledge hits or per-turn cost
— so it can only ever replace the user-facing half of the F10 transcript viewer, never the
admin half; `TranscriptReader` stays required either way. Its one capability nothing else
supplies is inbound typing state from its own web widget, and only there (D22).

### Shape A: a channel registry plus one delivery step — no sixteenth port

**Decision.** Outbound delivery of a finished `TurnResult` goes through a channel registry
wired in `composition.py`, plus one generic `_step_deliver` step in
`adapters/driving/workflow/turn_workflow.py`. **No new port.** The port count stays exactly
as `docs/ARCHITECTURE.md` §3 states it (see D24 — that section is the only place a count is
stated). The already-planned `ChannelHumanGateway` (named in `composition.py`'s pseudo-code)
shares this same registry rather than inventing its own: one channel-id → adapter lookup,
used by both the mid-turn human-wait path and the end-of-turn delivery path.

**Shape B — a sixteenth port — was considered and rejected.** Two independent arguments,
not one:

- `HumanGateway` cannot absorb this instead. Its own docstring scopes it to one question —
  *"who do I ask, and how do I wait for the answer?"* (`ports/human_gateway.py:1`). Delivering
  a finished result is a different question; folding it in is the two-questions-one-port
  mistake `CLAUDE.md`'s layer rule calls a port cut wrong.
- A new port does not clear the bar D15 sets for when one is worth it: it would not add a
  question the domain asks, only a dispatch step over channels already carried on
  `CallerIdentity.channel` (`domain/turn.py:68`). D10 already established the precedent for
  this shape of problem — MCP composes into `ToolProvider` instead of becoming its own port,
  because it does not change the question `ToolProvider` answers. `docs/ARCHITECTURE.md` §3
  is explicit that this is the general pattern here, not an exception: every capability added
  since the first draft — compaction, skills, MCP, media, knowledge, peers, transcripts —
  entered as data and composition, and the invariant the architecture defends is that a
  vertical touches only `ToolProvider` and the rows behind `ToolPolicy`, not that the port
  count never moves. Moving it anyway is not free regardless: D24's addendum found a
  stale-count defect that had already spread to five files from one earlier move.

**Why delivery cannot ride the phase-one polling contract instead.** D13's contract is
`POST /turns` returning 202, consumed via `GET /turns/{turn_id}`. That assumes a caller able
to poll us. D22 already verified, for the two channels that matter most, that this does not
hold: WhatsApp Cloud API and Telegram Bot API are webhook-push in the direction that matters
here too — the platform POSTs inbound to us, and answering means us calling out to *their*
send API, never them calling back into ours. Neither channel can be served by a model where
the client fetches its own answer; the answer has to be pushed.

**Consequence.** `t-f0-00` closes: the first driving adapter is no longer written under an
undecided assumption. The webhook adapters and the delivery registry itself are still
unbuilt — tracked as new tasks in F3, where `ChannelHumanGateway` already lives (see
`docs/TASKS.md`).

---

## D24 · A root-level task runner, and no duplicated facts

**Two decisions from one incident.**

### The runner

`Makefile` and `.github/workflows/ci.yml` at the repository root declare how this project is
tested. `Core/scripts/check_imports.py` backs the cheapest check.

**Why it is not just convenience.** Tooling that inspects a workspace looks for a root-level
declaration of a test command. Without one it concludes there is none — which is how this
repo's fakes-first convention got reported as *"no strict TDD"* when the only real problem
was that pytest lives under `Core/`. The gate was tripped by a working directory, not by a
design choice.

`make imports` (and the first CI step) runs **before dev dependencies are installed**, on
purpose: most functions here raise `NotImplementedError`, so a largely-skipped suite proves
very little, and a broken import in a stub is exactly the class of breakage it cannot catch.

### No duplicated facts

The port count moved from eleven to fifteen during design and left **four stale strings**
behind: `Core/pyproject.toml`, the `## 3.` heading in `ARCHITECTURE.md`, a sentence in D10,
and the docstring of `tests/fakes/ports.py`. Each was individually harmless; together they
made the repo contradict itself, and the `ARCHITECTURE.md` heading contradicted the table
directly beneath it.

**Convention.** `docs/ARCHITECTURE.md` section 3 is the single source of truth for the port
count. Everywhere else states the **invariant** instead — *only two ports change when a
vertical is added* — because that is what has not changed and will not.

The lesson generalises: a number repeated in five files is five things to update and four
opportunities to be wrong. State the invariant, not the count.

---

## D24 addendum · the drift inventory had drifted

D24 states that the port count "left **four** stale strings behind". A later adversarial
pass found a **fifth** in `docs/ARCHITECTURE.md` — the section-2 layer diagram read
`ports/  eleven protocols`, ninety-seven lines above the section-3 table in the *same file*
that correctly said fifteen.

And more instructive: **`README.md` had already drifted the same way**, within hours of D24
being written to prevent exactly that. It claimed "D1–D23" against 24 decisions and "90
anchored tasks" against 91. `Core/pyproject.toml`'s pytest marker still enumerated
"(F0..F7, D2)" after the roadmap grew to F10.

**So the convention is stronger than first written, and it applies to every count, not just
the port count:**

> Do not state a count that can change. State the invariant, or point at the document that
> owns it.

The counts are now removed from `README.md`, from the layer diagram, and from the pytest
marker. `docs/ARCHITECTURE.md` section 3 owns the port count because that table *is* the
count — nowhere else needs to repeat it.

This addendum is kept rather than folded into D24 because the failure is the point: a
decision record written to prevent drift drifted, and a hand-maintained inventory of stale
strings is itself a stale string waiting to happen.

---

## D25 · Four-eyes: an approval must come from a second person

**Decision.** `DecideApproval` refuses an approval whose `subject_id` is the human who
started the turn. The "requester" is that human — `TurnRequest.caller.subject_id` — never
the agent. The rule gates `approved=True` only, treats an unknown requester as a refusal,
and compares `subject_id` alone, stripped and casefolded.

**Evidence — the agent cannot be the requester.** `CallerIdentity` is the only human
identity a turn carries, and `PendingRequest` (`domain/turn.py`) carries none at all: it
holds `kind`, `tool_call_id`, `tool_name`, `arguments` and `reason`. There is nothing to
compare an approver *against* on the agent side, so a rule phrased "the approver must
differ from the agent that asked" could never fire. It would be a control that passes by
construction — the same defect `t-f10-07`'s visibility table had, and for the same reason:
the assertion restates its own input.

**Evidence — the channel does not prove identity.** `decide_approval.py`'s own STEP 2 note
already listed the three ways it fails: a shared inbox, a forwarded message, a group chat.
That is why the comparison is on `subject_id` and not on the tuple with `channel`: the same
person answering from WhatsApp instead of the web is still the same person, and matching on
the tuple would let a channel switch defeat the rule.

**Reasoning — why this rule at all.** The suspension exists because `ToolPolicy` returned
NEEDS_APPROVAL: policy already decided this action needs a human. If the human who asked is
also the human who answers, the second signature carries no information the first did not —
the request already expressed their intent. D11 chose delivery as the first vertical
precisely so the core is not shaped by fraud's regulatory weight, but this control is the
one piece fraud will need unchanged, and retrofitting it after approvals exist means
auditing every approval already granted.

**Refusal is not gated, and that is deliberate.** Four-eyes stops a human *granting*
themselves a permission. Refusing your own request removes one. Gating it would leave the
turn asleep until it expires with the one person who wants it stopped unable to stop it —
a worse outcome, produced by a stricter rule.

**Unknown requester fails closed.** When the lookup returns None the approver may well be a
second person, but nothing here can show it. An approval that cannot be demonstrated is not
one. `FourEyesError` covers both halves for that reason.

**Casefold and strip, not exact match.** The two mistakes are not symmetric. An exact-match
rule is defeated by whichever spelling of their own id the requester can persuade a channel
to send (`U-1`, `u-1 `); over-matching costs one identity provider that issues two distinct
subjects differing only by case, which is a defect there rather than a decision here. Fail
in the direction that refuses.

**Where the requester comes from.** An injected `RequesterLookup` callable, bound by the
composition root — the same arrangement, and the same two reasons, as `DecisionSignal` in
the same file: `application/` may not reach for a repository, and a narrow callable gives
the use case nothing it could wait on. No port changes; the invariant holds.

**The refused attempt is not audited, and that is a gap.** `AuditSink` has four members and
none of them records "somebody tried and was refused". Filing the attempt through
`record_human_decision(approved=False)` would read in the trail as a refusal the human never
made, which is worse than silence, so the check runs *before* the audit row and writes
nothing. This is the one place D25 differs from the DENY path in
`PolicyEnforcement.before_tool_execute`, which writes its row and then raises. A
`record_rejected_decision` member is owed and has no anchor.

**Still open.** Nothing constructs `DecideApproval` in `composition.py`, so the `requester`
seat has a default of `None` and an unwired use case does not evaluate the rule. That is a
deployment defect, not a permitted mode. `Core/tests/unit/test_four_eyes.py` pins the
unwired behaviour deliberately — an unenforced rule nobody tested looks exactly like an
enforced one from the outside.

---

## D26 · Evidence travels to the model as bytes, never as a signed URL

**Decision.** `request_evidence` results reach the model as inline bytes. A signed URL is
used only where the evidence is known to carry no personal data, and that branch must be
opted into per profile, never defaulted.

**Evidence.** Verified against the installed Pydantic AI and recorded in `docs/TASKS.md`
under F7: given `ImageUrl` or `AudioUrl`, Pydantic AI does **not** fetch the file. It sends
the URL to the provider, and the provider downloads it.

**Reasoning.** That single fact changes what the two options are. It is not "bytes over the
wire versus a pointer over the wire" — it is *who fetches*. With a URL the provider's
infrastructure makes an outbound request to our storage, so:

- The disclosure is to a third party. Evidence in this system is a photo of a package, an
  identity document, an invoice. Handing a provider a URL to it discloses the content to
  whoever operates that provider and to whatever egress sits in front of them, in a request
  we do not make, cannot see, and did not log.
- The signature outlives the turn. A signed URL is valid for its whole TTL to anyone
  holding it, and it is now in the provider's request logs. Bytes in a request body have no
  independent lifetime.
- The failure is silent and remote. A provider that cannot reach the URL produces a model
  answer about a picture it never saw, not an exception on our side. Bytes either arrive or
  the call fails here, where the traceback names the culprit.
- Our storage becomes reachable from the provider's network. `adapters/driven/media_fs/`
  is content-addressed local storage; a signed-URL path means it must be publicly routable,
  which is a deployment requirement the bytes path does not create.

**Cost of the decision, stated honestly.** Bytes are re-uploaded on every turn that replays
the evidence, they count against the request size limit, and they inflate the compaction
budget that D8 exists to manage. That is the price, and it is a bill, not a disclosure.

**Where the branch lives.** `t-f7-07`, in `adapters/driven/agent_pydantic/runner.py`. This
entry is the ruling only; no code lands with it.

---

## D27 · Reasoning is kept 30 days, deleted by a scheduled sweep

**Decision.** Rows in `turn_reasoning` are deleted 30 days after `created_at`, by a
scheduled sweep that owns no other table. The window is configuration with a 30-day
default, not a constant, and shortening it must not require a migration.

**Evidence — what is in the table.** `reasoning_migration.py` (0015) stores the model's
`reasoning_content` verbatim, and `docs/FIELD-NOTES.md` records that MiniMax M3 returns it
in **both** modes — 128 characters on a default call, 91 with `reasoning_effort="medium"`,
in the same smoke test. Every turn produces a block. This is not an occasional artefact
whose volume can be ignored; it is one row per turn, forever, by default.

**Evidence — what those rows are.** `domain/transcript.py` answers REASONING as
`USER: False, ADMIN: True`. The migration's own docstring says why the table is separate:
reasoning holds the model's private speculation about the person it is speaking to, and a
`messages` row would put it one forgotten WHERE clause from that person. A store that is
this sensitive and this deliberately walled off is a store whose value decays fast and
whose liability does not.

**Reasoning — why 30 days.** The stated purpose (`t-f10-06`, the operator endpoints) is
letting an operator page a conversation to understand what the agent did. That is an
incident-response and complaint window, and incidents are looked at in days, not quarters.
Nothing in F10 reads reasoning for training, analytics, or reproduction — `t-f1-19` proves a
turn is reproducible from the **profile version** and the audit trail, neither of which is
in this table. So the window is the one that keeps the operator endpoint useful and gives
back nothing else. Thirty days covers a monthly review cycle with margin; the audit trail
and the transcript survive independently, and deleting reasoning does not make a past turn
unauditable.

**A window nobody wrote down is an unbounded one.** That is the whole reason this entry
exists. The migration explicitly declined to invent one and pointed here.

**What deletes them.** A scheduled sweep — the same scheduler as `t-later-01`
(`adapters/driving/scheduler/cron.py`) — issuing a bounded, repeated
`DELETE FROM turn_reasoning WHERE created_at < now() - <window>` in batches. Not
`ON DELETE CASCADE` from any parent, because there is no parent that expires; not a
partition drop, because the table is small enough that the operational cost of monthly
partitions exceeds the delete cost; not application-side deletion on read, because a row
nobody reads is exactly the row that must still go.

**A defect this decision found, and does not fix.** `reasoning_migration.py`'s docstring
claims "`created_at` is indexed so a retention sweep is a range delete rather than a table
scan". The index it created is
`ix_turn_reasoning_session_created (session_id, tenant_id, created_at, id)` — `created_at`
is the *third* column. It serves the per-session read it was designed for and does **not**
serve a global sweep by age, which is a sequential scan of the whole table. The sweep needs
its own `created_at`-leading index, or it must run per session. That belongs to whoever
owns the sweep; the claim in the docstring is wrong today and is recorded here so the sweep
is not written against it.

## D28 · Deferred calls of one model step: collect every result, resume once

**Defect.** Pydantic AI continues a run that stopped on N deferred calls only when it is
handed results for all N (`UserError: Tool call results need to be provided for all
deferred tool calls`). The workflow woke on each `PeerAnswer` and resumed with that one
answer, so the first reply to a two-ask step failed the turn. A vertical masked it in the
tool (one `ask_peer` per step); the cause is in the workflow.

**Decision.** When a round has any peer ask (`PendingKind.DELEGATION`) - one ask, several, or
mixed with a human's approval or evidence request - the workflow collects every result and
runs a single `_step_resume_round` once all are in, in `tool_call_id` order. Results are
peer answers, gate refusals, peer timeouts and the humans' decisions (`HumanAnswer`). All
asks are dispatched before the wait, so the peers run concurrently and the round costs the
slowest call.

- **Two deadlines, one loop.** Peer asks: the asking profile's
  `PeerPolicy.reply_timeout_seconds`, counted from the moment the asks left; an ask
  unanswered at the deadline gets an error text for that call only and the answers that
  arrived are delivered. Humans: `THREE_DAYS` from the same moment, unchanged in meaning -
  the peer timeout is never applied to the person, and a person who has not decided by
  then expires the turn (nothing is resumed on a partial set). Each wait is bounded by the
  nearest live deadline; time comes from a recorded step (`_step_round_clock`), so replay
  recomputes the same remaining time.
- **Late replies.** A reply to a timed-out ask is skipped by later waits (its call was
  already answered), instead of resuming the turn on it.
- **Re-ask after a timeout.** The mailbox port is idempotent per `(target, question)` and
  has no verb to close or version a row, so a word-for-word re-ask in a later round gets
  the SAME correlation id. Keying the id by round would need a port change; instead the
  re-dispatched handle leaves `timed_out` and its stored answer, if the reply already
  landed, is read at once (`_step_peek_peer_answer`); otherwise the reply (late or fresh)
  is accepted by the new round. Nothing marks a handle dead for good.
- **Unchanged.** Hop gate per ask, mailbox idempotency, answer wrapping at one place, audit.

**Still on the per-answer path (superseded by D32/C6, which collects human-only rounds too).**
A round with no peer ask (one or several human requests) kept the previous wait: one
`THREE_DAYS` wait per answer. Several human-only requests in one step had the same
partial-resume defect. Timed-out ask rows stayed open in the mailbox (no close verb; D31 added
`cancel`).

**Deploying D28 (replay shape).** D28 removed the steps `_step_refuse_peers` and
`_step_resume_peers`, added `_collect_round` (a new step order for single-ask and mixed
rounds) and renamed the clock step to `_step_round_clock`. A turn suspended on the old code
would replay against a different step sequence. The repo pins the DBOS application version
(`composition.PINNED_DBOS_APPLICATION_VERSION`, t-f2-12) so recovery survives ordinary
deploys; this change is the incompatible case that pin's rule names, so the version moved
`agent-core-turns.1` -> `.2`. Consequence: turns still suspended at deploy time (waiting on
a peer or a person) are NOT recovered by the new build; they stay PENDING under `.1`. Before
deploying, drain them (let in-flight turns finish or expire) or accept that they are
abandoned. A unit test pins that the version is no longer `.1`.

**Ignored on purpose.** A `HumanAnswer` for a call the current round is not waiting on (a
double click or retried POST arriving after its round resumed) is logged and skipped, like a
duplicate peer wake-up; it never fails the turn. D30 narrows this: only a duplicate of a call an
EARLIER round issued is ignored; an id no round issued fails the turn.

## D29 · Peer asks have deterministic ids, and a peer turn is a durable workflow

**C2 · deterministic ids.** The correlation id of an ask used to be `secrets.token_urlsafe`, made
idempotent only by the mailbox's `(turn_id, target, question)` constraint, and the callee's
session was `peer~<root>~<uuid4>`. Both are now DERIVED, so a DBOS replay of the asking step, or a
second worker picking up the same row, names the same objects and cannot double-post or fork the
callee's conversation:

- `AgentMailbox.ask(..., request_key=None)` is new on the port. `_step_ask_peers` passes the
  `ask_peer` tool call id. With a key, `correlation_id = uuid5(NS, json([turn_id, request_key,
  target, question]))` (`domain.peers.peer_ask_id`). Target and question are in the name so a
  provider that reuses a tool call id for a different question cannot collide on the primary key;
  JSON encoding keeps one field from shifting into the next. Without a key the old random id is
  kept (callers typed on the port before this change).
- The mailbox stays idempotent per `(turn, target, question)` (D28): a word-for-word re-ask in a
  later round, under a NEW tool call id, still returns the first row. Only the primary key and
  that constraint can conflict, and both conflict together on a replay, so the `DO UPDATE` branch
  returns the existing id instead of raising.
- The A2A adapter sends the derived id as the message's `messageId`.
- The callee session is `peer~<root origin>~<uuid5(NS, correlation_id)>`
  (`domain.peers.peer_session_key`, `new_peer_session(key=...)`). Root-origin unwrapping and the
  `~` validation are unchanged.

Replay shape: step names and order are unchanged, only the id a step returns differs. The pinned
application version is not bumped for this.

**C1 · the peer's turn is a DBOS workflow.** The worker used to claim a row and run the callee's
turn through `DirectTurnRunner` (`StartTurn` directly, not durable). Two holes followed: a callee
that suspended (its own human's approval, or its own ask) recorded nothing, so the asker waited
for its reply timeout; and a worker that died after the claim lost the row with the turn.

- `run_peer_turn_workflow(PeerTurnJob)` runs the callee's turn with `_drive_turn`, the body that
  `run_turn_workflow` was split into (same steps, same order: no replay-shape change for existing
  turns). It differs only at the end: no channel delivery or compaction (nobody is on the `peer`
  channel); instead `_step_record_peer_reply` writes the answer (`mailbox.answer`, first answer
  wins) and then `signal_peer_answer` wakes the asker. Record first, wake second.
- Its workflow id is `peer-<correlation_id>`, which is also the callee's turn id, so the callee's
  human approval is addressed to it and an asker that restarts or a worker that re-claims the row
  names the same workflow: DBOS returns the existing one and the callee does not run twice.
  It is enqueued on `agent-core-turns`, partitioned by the (fresh) peer session.
- The worker (`dispatch_next_ask`) claims, builds the request exactly as before (target profile,
  `peer-agent` identity and role, the hop count, the untrusted question, the root-origin session
  id), starts the workflow through the `PeerTurnStarter` seat (`DurablePeerTurnStarter`) and
  returns. It no longer answers started asks. Hop limits and the asking side's two-sided
  allowlist are untouched (`_step_ask_peers`). `answer_next_ask` and `DirectTurnRunner` remain
  for the console and for tests; `run_peer_worker` picks the path from the seat it is given.
- Errors: the callee raising records `PEER_ASK_FAILED_ANSWER` (fixed text, never the exception)
  and the workflow still fails, so the error stays visible. A callee turn closed unanswered
  (expired waiting for a person, too many rounds) records `PEER_ASK_CLOSED_ANSWER`. A failed
  start closes the ask with the failure text, as a failed turn always did. A cancelled task
  (engine shutdown) records nothing. If the asking turn is gone the wake fails and is logged;
  the answer is on the row.
- Replay shape: `_drive_turn` is a plain coroutine, so `run_turn_workflow` keeps the same step
  names and order. The new workflow and step do not affect turns in flight, so the pinned
  application version is NOT bumped (`agent-core-turns.2`).

**Known limits, owned by later tasks.** A crash between the claim and the workflow start still
leaves a `delivered` row with nothing running (C3: redelivery lease; the deterministic id makes
the retry safe). The callee's approval request is published through the same `HumanGateway` as
any turn, addressed by the peer session; showing it under the conversation the user is in is C16.
Cancelling a running callee when the asker times out is C4.

## D30 · Follow-ups of the D28 review: stale answers, stranded versions

**Stale or unknown human answers (`_collect_round`).** D28 logged and skipped any `HumanAnswer`
whose call the round was not waiting on. That conflated two cases. The body now hands the round
`earlier_calls`, every tool call id issued by previous rounds of the turn:

- an id in `earlier_calls` is a double click or retried POST whose first copy resumed that round:
  logged and ignored, as before;
- an id NO earlier round issued (or one issued to a peer, which no person can decide) is not a
  duplicate of anything. Skipping it left the sender believing a decision was taken while the turn
  waited on nothing until `THREE_DAYS`. It now raises `UnusableResumeAnswerError`, the same
  treatment a peer wake-up for a handle the turn never asked under already gets.

`earlier_calls` is derived in the workflow body from step results, so replay recomputes it.

**Stranded turns after a version bump (C0a).** D28's bump `.1` -> `.2` left turns suspended under
`.1` PENDING for ever, each holding its session's single queue slot, with no way for an operator
to even see them. `python -m agent_core stranded-turns` now lists the open workflows
(`PENDING`, `ENQUEUED`, `DELAYED`) whose `application_version` differs from the one the build
claims, by one SELECT on `dbos.workflow_status` of the DBOS system database. It exits 0 / 1 / 2
(clean / stranded / could not check) so a deploy script can gate on it. It reads only: launching
DBOS would start recovery in the checking process, and cancelling is a different act. The
drain/cancel procedure is in `docs/OPERATIONS.md` (`dbos workflow cancel`).

## D31 · Agent-comms v2, wave 1 follow-ups: boundary checks, answer secret, redelivery, cancel

Follow-ups of the approved review of 0e5dc8f (C1b-C1e) and the reliability tasks C3 and C4. One
DBOS application version bump covers the whole batch (`agent-core-turns.2` -> `.3`, see
OPERATIONS.md).

**C1b · a bad decision cannot kill a live turn.** D30 made a `HumanAnswer` for a tool call no
round issued raise inside the workflow, so one bad send was a denial of service. Two layers now:

- *API boundary.* `DecideApproval` takes a `pending` check (`PendingCheck`, bound in composition
  to `human_requests`): the (turn, tool call) the handle resolves to must be a live request
  published to a person (`kind <> 'delegation'`, unexpired). Otherwise it raises
  `UnknownCorrelationError` (a 404) BEFORE the audit row and BEFORE the signal. It is a
  composition-level SQL for the reason `_REQUESTER_LOOKUP_SQL` is: the `HumanGateway` port
  already answers its one question.
- *Workflow.* `_collect_round` logs at WARNING and skips a decision whose id this round is not
  waiting on, whether an earlier round issued it (a double click, as before) or no round did (was
  fatal). D30's `UnusableResumeAnswerError` for that case is gone; the peer-answer wake-up
  checks are unchanged.

**C1c · an answer needs more than the correlation id.** The id is `uuid5(turn, tool_call_id,
target, question)`: an address anyone with those inputs can compute, and answers were matched by
it alone. Each ask row now has a random 256-bit `answer_token` (migration `0026`), minted by
`PgAgentMailbox.ask` on first insert (a replayed insert is the `DO UPDATE` no-op and keeps the
first token, so replay never changes it), returned only on the claimed `PeerAsk`
(`repr=False`), carried in `PeerTurnJob`, and presented by `answer(..., answer_token=)`.
`AgentMailbox.answer` raises `PeerAnswerRefusedError` and writes nothing when it does not match;
`_step_record_peer_reply` logs the refusal and does not wake the asker. A retry of an answered
ask and an unknown id stay silent no-ops. The A2A adapter mints the token per message, sends it
as `metadata.answer_token` and checks it (constant time) on `answer`. Rows enqueued before the
migration have a NULL token and are answered with None, so asks in flight across the upgrade
complete; that is the only window where an id alone answers. Not covered: the `PeerAnswer`
wake-up on the durable topic is addressed by workflow id and still names only the correlation
id; a forged wake finds no recorded answer and the asker rejects it as before.

**C1d · an ambiguous start does not close the ask.** `dispatch_next_ask` wrote
`PEER_ASK_FAILED_ANSWER` on any exception or cancellation out of `starter.start`, even when the
enqueue had already committed (a lost acknowledgement, an engine shutdown): the asker then got a
failure while `peer-<correlation_id>` went on to answer. `PeerTurnStarter` gained
`exists(correlation_id)` (`DurablePeerTurnStarter`: `DBOS.get_workflow_status_async` is not
None). After a failed start the worker closes the ask ONLY when `exists` says the workflow
definitely does not exist, and re-raises either way (a `CancelledError` is re-raised after the
same decision). If `exists` itself fails the answer is "unknown", the ask is left `delivered`,
and the redelivery lease (C3) reclaims it if the workflow really was lost.

**C1e · readability.** The peer workflow decides "closed unanswered" from the typed
`_TurnEnd.closed` only; the comparison of the reply text with the `_NO_ANSWER` /
`_TOO_MANY_ROUNDS` sentences is gone (a genuine answer could read the same). That needed
`_collect_round` to return a `_TurnEnd`: a person not deciding inside a collected round now ends
the turn as `closed`, which was previously visible only through the text. Side effect for an
ordinary turn: expiry inside a collected round no longer runs the delivery and compaction steps,
exactly like the per-answer path (an expired turn is the turn giving up, not answering). The
private `_FAILED_ASK_ANSWER` alias in the worker was removed (the domain constant is used), and
`cli/stranded_turns.py` has the `first_line` helper and documents that
`dbos.workflow_status.created_at` is epoch MILLISECONDS (`row_to_stranded`).

**C3 · redelivery lease.** A claim marks the row `delivered`; a worker that dies before it enqueues
`peer-<correlation_id>` left a row nothing would ever run (D29's known limit). Now:

- `AgentMailbox` stays unchanged; the answering-side `PgAgentMailbox.reclaim_stale(target, *,
  lease, exists, now=None)` (reached through the `StaleReclaimer` protocol) lists rows that are
  `delivered`, unanswered and older than the lease (the database's clock unless `now` is given),
  asks `exists(correlation_id)` for each, and puts only those with NO workflow back to `queued` in
  one guarded UPDATE (`redeliveries` + 1, migration `0027`, plus a partial index on the delivered
  tail). A row whose workflow exists in any state is never touched: it is running, waiting for its
  human or finished, and that workflow answers it. An `exists` that raises is "unknown" and the
  row is left alone. A running/finished workflow of the same id would make redelivery a no-op
  anyway (the id is deterministic), so a reclaim is safe even if it races a late start.
- `exists` is `PeerTurnStarter.exists` (C1d), so the engine question is asked of DBOS, not of the
  database. `run_peer_worker(..., lease=)` reclaims for every target at the start of each pass,
  before claiming; it needs the durable starter and a queue that can reclaim, else `TypeError`.
- Configuration: `AGENT_CORE_PEER_LEASE_SECONDS` (`Settings.peer_lease_seconds`), default 300, a
  positive whole number or startup fails. The answer token (C1c) survives redelivery.
- Deliberately not done: a poison budget (the `redeliveries` counter is where C5 can read it) and
  reclaiming rows whose workflow exists but is stuck (that is C4/C5 territory).

**C4 · cancel and expire cascade.** D28 left timed-out ask rows open, so a callee kept running
(and paying for model calls) for an asker that had moved on. Now an ask the asker gives up on is
CANCELLED, both halves:

- *Row.* New end state `cancelled` of `peer_messages.state` (migration `0028` adds
  `cancel_reason`, `cancelled_at` and a partial index on open asks per turn). Port verbs
  `cancel(correlation_id, reason) -> bool` (True when this call ended an open ask; answered or
  already cancelled is False, idempotent), `cancel_open(turn_id, reason)` (every open ask of one
  asking turn, ids sorted) and `is_cancelled`. A cancelled row is never claimed, never reclaimed
  by the lease, and `answer` on it raises `PeerAnswerRefusedError` (not written; the peer
  workflow then does not wake the asker). The answer UPDATE excludes `cancelled`, so an answer
  and a cancel racing each other have exactly one winner.
- *Callee workflow.* `DBOS.cancel_workflow_async("peer-<id>")`. `_step_cancel_peer_asks` is ONE
  recorded step (row first, then the engine, idempotent so a retried step finishes the job; a
  workflow already gone is logged, not an error). `cancel_turn(turn_id, reason)` is the same
  cascade for a whole turn, run from outside a workflow: it cancels the asker first and then
  `cancel_open`. A bare `dbos workflow cancel` of an asker does not cascade.
- *When.* In `_collect_round`: at the reply deadline for exactly the asks still awaiting (an
  answered ask in the same round is untouched, "cancel only that ask"); and when a person does
  not decide in time and the turn expires, for every ask still outstanding. `fail_fast` (C10)
  calls the same step with the siblings of the failed ask.
- *Re-ask supersedes D28.* The mailbox is idempotent per `(turn, target, question)`, so a
  word-for-word re-ask after a timeout names the cancelled row. It used to be able to deliver
  late; it now resolves at once with a "cancelled earlier" text instead of waiting a second
  timeout for a reply that cannot come. Repeats become fresh asks with C13.
- *Limits.* A cancel that races the worker between its claim and `start` marks the row
  cancelled, but the workflow may still start and run; its answer is refused. The A2A adapter
  cancels locally only (the wire `tasks/cancel` is D2). Asks a cancelled callee made to third
  agents were not cascaded: `peer_messages.turn_id` was a UUID column and a callee's turn id
  (`peer-<id>`) is not one, so a nested ask (A -> B -> C) failed on INSERT with
  `InvalidTextRepresentation`, and `cancel_open` of such an id returned nothing. D32 (C1f) fixes
  both.

## D32 · Agent-comms v2, wave 1 reliability: nested asks, stranded rows, tidy-ups, budgets

Follow-ups of the review of ec60ee0 (C1f-C1h) and the reliability tasks C5-C7. One DBOS
application version bump covers the whole batch (see OPERATIONS.md).

**C1f · nested asks (A -> B -> C).** B's turn id is its workflow id `peer-<correlation id>`.
The ask B makes is filed under it, and the column was a UUID: the INSERT failed (observed RED:
`psycopg.errors.InvalidTextRepresentation: invalid input syntax for type uuid: "peer-..."`).

- Migration `0029` makes `peer_messages.turn_id` TEXT, holding the id verbatim. The unique
  constraint `(turn_id, target_agent_id, question)` and the open-asks index follow the column
  (`ALTER ... TYPE` rebuilds them). The `transcript_entries` view reads this column and Postgres
  refuses to retype a column a view depends on, so the migration drops the view and re-creates it
  from the shared `TRANSCRIPT_ENTRIES_VIEW_SQL`, whose `turn_id` is TEXT in every branch of the
  UNION (the consumer already stringified it).
- `cancel_open` no longer pre-checks for a UUID and its SQL no longer casts.
- *Cascade.* `_cancel_callees` (the body of the recorded cancel step and of `cancel_turn`) now,
  after cancelling a callee's row and workflow, also cancels the asks that callee made
  (`cancel_open("peer-<id>")`), breadth first and each id once, so a reply timeout, an asker
  that expires or `cancel_turn` reach every level. Known gap: a crash between `cancel_open`
  returning the nested ids and the engine cancel of those nested workflows leaves their rows
  cancelled (so nothing they write is accepted) but the workflows run on to the end of their
  turn; a retried step cannot see them again.
- **Finding, not fixed here.** Every other `turn_id` column (`turns`, `human_requests`, the
  `audit_*` tables, `turn_reasoning`) is still UUID, and a callee's `StartTurn` persists its
  turn under the workflow id `peer-<id>`. The durable peer workflow (C1/D29) is covered by
  integration tests whose store is an in-memory double; against the real Postgres store the
  callee's first `INSERT INTO turns` would be refused for the same reason. Fixing it means
  either TEXT turn ids everywhere (views, audit tables) or UUID workflow ids for peers
  (`uuid5(corr)`), both a decision for the owner of the schema.

**C1g · a version bump no longer strands peer asks.** D31's lease requeued a delivered row only
when no workflow `peer-<id>` existed, and `exists` was true for every workflow DBOS knew,
including one a PREVIOUS build started and left PENDING. DBOS recovers a pending workflow only for
the application version the recovering process claims, so that workflow would never run again and
the row stayed `delivered` for ever: OPERATIONS.md's claim that the lease reclaims such rows was
false. Chosen: automatic reclaim with a new deterministic workflow id (the explicit operator
command was the alternative; it needs the same machinery, and leaving the asker to time out is
what the current build does for every other stranded turn).

- `exists(correlation_id, generation)` is now "a workflow of this generation exists AND this
  build will drive it": an OPEN workflow (`PENDING`, `ENQUEUED`, `DELAYED`) under another
  `app_version` is False. A finished one stays True whatever its version.
- *Generation.* The row's `redeliveries` is the generation: the first attempt is `peer-<id>`,
  the n-th redelivery is `peer-<id>-r<n>` (`peer_workflow_id(corr, n)`). The claim returns
  `PeerAsk.redeliveries` and `PeerTurnStarter.start(..., generation=)` uses it. The callee's turn
  id is its workflow id, so a rerun is a new turn with new approvals.
- *Retire, then requeue.* `reclaim_stale(..., exists, retire)` calls `retire(corr, generation)`
  before the requeue for every row judged dead. `DurablePeerTurnStarter.retire` cancels the
  stranded workflow (kept, with its history, as `CANCELLED`) and the asks it made to third agents.
  This is required, not tidy: DBOS' partition probe is not scoped by version, and the new
  generation shares the old one's peer session (and so its partition), so an open stranded run
  would block it for ever. The requeue is guarded by the generation it judged, so a stale verdict
  cannot bump a row twice. A `retire` or `exists` that raises leaves the row for the next pass.
- *Cancel reaches every generation.* `_cancel_callees` walks `peer-<id>`, `peer-<id>-r1`, ... as
  far as DBOS knows them, so cancelling an ask cancels whichever generation is live.
- *Hazard, stated.* The rerun starts the callee's turn from the beginning, in the same peer
  session. A tool with side effects that the stranded run had already executed can run again;
  asks the stranded run made are cancelled, the rerun asks again. Approvals the stranded run
  published stay open in `human_requests` until they expire (nothing resumes on them).

**C1h · tidy-ups from the review.**

- *Non-durable answer path.* `answer_next_ask` (the console and tests) recorded the callee's
  reply with `queue.answer`, which a cancelled row refuses (D31, C4); the refusal escaped and took
  the worker loop down. It is now caught: logged, nothing written, nobody woken, and the report
  carries the new `AskDisposition.REFUSED` (also for a token mismatch).
- *A2A adapter follows the port for unknown ids.* `A2AAgentMailbox.answer` raised
  `PeerAnswerRefusedError` for a correlation id it never sent, while the port (and
  `PgAgentMailbox`) say an unknown id is a silent no-op. Refusal is now only for a KNOWN ask
  presented with the wrong token (or a cancelled one). Pinned by the shared conformance suite
  (`tests/contract/mailbox_conformance.py`, run against both adapters), which also found that the
  adapters differ on one point the suite deliberately does not assert: a word-for-word re-ask
  under a new tool call id is folded into the first row by Postgres (unique per turn, target,
  question, D29) and is up to the peer's `messageId` de-duplication over A2A.
- *One constant for the secret's strength.* `domain.peers.ANSWER_TOKEN_BYTES` (32) and
  `new_answer_token()` replace the private constant in the Postgres adapter and the literal in the
  A2A one.
- Test tidy: one helper (`_drain_queue`) empties `peer_messages` for the mailbox tests, and a
  comment in `test_peer_cancel.py` is a statement. D31's dangling "(see the report)" now states the
  finding inline (a nested ask failed on INSERT because `peer_messages.turn_id` was a UUID).

**C5 · poison-turn budget.** DBOS re-runs a pending workflow's body after every crash. A turn
whose step kills its process (a native crash, an out-of-memory kill) is therefore re-run by every
restart, for ever (DBOS' own dead-letter limit is 100 recoveries and dead-letters in silence).
OpenCode's `restart.ts` counts resumes durably before each one and terminalizes past a budget
(default 10); this is the same idea, shaped to DBOS.

- *Where the count lives.* A step runs once and a replay reads its recorded result, so a step
  cannot count body entries. The count is a row (`workflow_attempts`, migration `0030`, port-like
  `AttemptLedger`, adapter `PgAttemptLedger`) that the BODY touches on each entry with a plain
  call, outside the operation log. Both `run_turn_workflow` and `run_peer_turn_workflow` do it
  first, before any step, so no replay-shape change: the application version is NOT bumped for
  C5 (see C6 for the batch's bump).
- *What counts.* An entry counts +1 unless the previous entry ended PARKED on a wait, in which
  case it restarts at 1. The body calls `park` right before it blocks on the durable topic and
  `resume` right after it returns. This is what keeps a turn waiting for a person for weeks (a
  deploy restarts it every time, and DBOS' own counter would count each) from using up the
  budget, while a process killed while working, including right after the wake, counts. A restart
  out of a wait that crashes before its first step completes is, by construction, the
  indistinguishable case; the next entry counts.
- *Budget and config.* `Settings.max_turn_attempts`, `AGENT_CORE_MAX_TURN_ATTEMPTS`, default 10,
  a positive whole number or startup fails. Entry number `budget + 1` is refused.
- *What a refused entry does.* It runs no step and raises `PoisonTurnError(reason)`, so the
  workflow ends `ERROR` with the reason, and DBOS stops re-running it. The reason names the
  workflow, the attempts and the budget. For an ordinary turn it also cancels the peer asks it
  still has open (rows and callee workflows, with the D32 cascade). For a peer workflow it
  answers the asker with the fixed failure text (`PEER_ASK_FAILED_ANSWER`, record first, wake
  second, like any failed callee) and cancels the asks the callee made. A poisoned workflow keeps
  its ledger row as evidence; one that ends on its own terms deletes it.
- *Outside the log.* The settlement runs in an EMPTY `contextvars` context (`_outside_the_log`).
  A DBOS call made from the body (a send, a cancel) is recorded at the next position of the
  operation log, and a run that gives up before replaying its earlier steps would collide with
  what is recorded there (`DBOSUnexpectedStepError`); in an empty context the same calls behave
  as an external caller's, as `cancel_turn` and the worker already do.
- *Fails open.* A ledger that cannot be written is logged and the turn runs uncounted: a counter
  table being down must not refuse every turn. A workflow bound without a ledger is not counted.
- *Not done.* No message is delivered to the person whose turn was poisoned (a product decision:
  `_step_deliver` is a step and the channel text is F3's); the ERROR status, the log line and
  the ledger row are the operator's evidence.

**C6 · a round with only people is collected too.** Rounds with several human requests and no peer
ask still used the per-answer wait and resumed on the first decision, so a model step that stopped
on two approvals failed with `UserError: Tool call results need to be provided for all deferred
tool calls` (observed RED: the first approval alone reached `ResumeTurn`, and a second request that
expired still produced an answer instead of an expiry). Now `_drive_turn` sends EVERY round through
`_collect_round`:

- the humans' decisions are collected and `_step_resume_round` runs once, in `tool_call_id` order;
- humans keep the `THREE_DAYS` semantics of D28, counted from the round's recorded clock
  (`_step_round_clock`) rather than from each answer. A person who has not decided by then expires
  the turn (`_step_expire`) and nothing is resumed on the decisions that did arrive: no partial
  resume. Cost: with several requests the last one has what is left of ONE three days, not three
  fresh days after the previous answer;
- what the per-answer step refused is still refused, now by `_collect_round`: a `HumanAnswer` of
  another turn, a payload that is no answer, a peer wake for a handle the turn never asked under;
  a duplicate decision does not resolve a second call, and an uploaded file reaches the
  resolution unchanged;
- `_step_resume`, `_step_answer_peer` and `_answer_the_deferred_calls` had no caller left and are
  deleted; the reasoning about the resumed continuation's identity (`request.caller`, never the
  approver) moved to `_step_resume_round`. `test_durability.py`'s step-log fakes were updated
  deliberately: the clock is now a recorded step of the log, and an unscripted wake fails the test
  instead of being accepted as an opaque payload (the old per-answer wait never looked inside it);
  no assertion was weakened. The refusal test of `test_resume_turn.py` moved to
  `test_human_collect_round.py` with both its payloads.

**Deploying D32 (replay shape).** C6 added a recorded step to every human-only round and replaced
`_step_resume` with `_step_resume_round`, so the pinned application version moved
`agent-core-turns.3` -> `.4` (ONE bump for the whole batch; C1f, C1g, C1h and C5 changed no step
name or order). A turn suspended on a person under `.3` is stranded, not recovered: drain it or
cancel it (OPERATIONS.md). Peer asks stranded the same way are rerun under `.4` by the lease (C1g).

**C7 · crash/restart test kit.** Pi's `ControlledStorage` (a storage you can hold, fail and
crash) and OpenCode's `restart.ts` tests were the models. Four pieces, all under `Core/tests/`:

- `tests/fakes/memory_mailbox.py`: `InMemoryAgentMailbox`, a full in-memory implementation of the
  port AND the answering side's queue (claim, lease with generation, cancel), with an injectable
  clock. It is the reference behaviour; a test that needs a mailbox and not a database uses it.
- `tests/fakes/controlled_mailbox.py`: `ControlledMailbox(inner)` records every call
  (`calls`, `count(verb)`) and plans faults per verb: `fail(verb, error, on_call=, times=,
  after=)`, `hang(verb, ...) -> Gate` and `delay(verb, seconds, ...)`. `after=True` lets the
  verb take effect on the inner mailbox first and then fails or hangs, the shape of a lost
  acknowledgement and of a process killed right after a commit. `ProcessDied` is a
  `BaseException` so no `except Exception` under test can swallow it.
- `tests/contract/mailbox_conformance.py`: ONE conformance suite for the port and the queue side,
  run by `tests/contract/test_mailbox_conformance.py` against the in-memory mailbox, the same
  mailbox behind `ControlledMailbox` with no fault (proving the wrapper is transparent),
  `PgAgentMailbox` (disposable database) and `A2AAgentMailbox` (an in-process peer on
  `httpx.MockTransport`; the queue-side tests are skipped for it, its transport is the wire).
  It pins: enqueue idempotency, answer-token rules and first-answer-wins, unknown ids as silent
  no-ops, the untrusted wrapping, cancel semantics (including a callee's non-UUID turn id), claim
  exclusivity and order, and the lease (only a stale, unanswered, delivered row nobody is
  running; `retire` before the requeue; the generation; an `exists` that raises leaves the row).
  `tests/contract/test_attempt_ledger.py` does the same for the C5 ledger.
- `tests/fakes/peer_world.py` + `tests/integration/test_peer_restarts.py`: real DBOS and the
  Postgres mailbox behind `ControlledMailbox`. A death is a gate that is never released plus
  `DBOS.destroy()` under it (the workflows stay `PENDING`); the next phase relaunches the engine
  on the same databases and DBOS recovers them. `dying_process` keeps the teardown from writing
  a "failed" answer a killed process could not have written. Scenarios, each asserting that the
  ask was posted once, the callee started once, the answer recorded once and the asker's resume
  completed once: a kill between the post and the claim; between the claim and the workflow start
  (recovered by the lease); between the answer and the wake (B's recovery sends the wake, A is
  woken once); during the asker's resume (the interrupted step runs again, and only that one
  finishes).

What the suite found, kept honest: the two adapters differ on one point, a word-for-word re-ask
under a NEW tool call id (Postgres folds it into the first row, A2A leaves it to the peer's
`messageId` de-duplication), so the suite does not assert it. And a replayed asking step posts
again (idempotently) if the engine dies between the write and the recording of the step; the
restart test therefore waits for the step to be recorded before it kills.

## D33 · Agent-comms v2: UUID workflow ids for peers, and the slice 8 advisories

**C1i · a durable callee's turn id must be a UUID.** A durable callee's turn id is its DBOS
workflow id, and the id was `peer-<correlation_id>[-r<n>]` (D29, D32). `turns.turn_id` (the primary
key), `human_requests`, the `audit_*` tables and `turn_reasoning` are `UUID` columns, so the
callee's first `INSERT INTO turns` failed on real Postgres. Every earlier peer test gave the callee
an in-memory turn store and never saw it. Observed RED
(`tests/integration/test_peer_durable_callee_postgres.py`, real Postgres, the real `StartTurn` and
stores, a real `DurablePeerTurnStarter` workflow):
`psycopg.errors.InvalidTextRepresentation: invalid input syntax for type uuid: "peer-21e9104d-..."`.

Chosen (user decision 2026-10-12): the id becomes `uuid5(PEER_WORKFLOW_NAMESPACE,
"<correlation id>:<generation>")`; no table migration. The alternative, TEXT turn ids everywhere,
would have touched the transcript view, five tables and every adapter that reads them.

- Still deterministic, so the properties C1 relies on hold: a second claim or a racing worker names
  the same workflow. Each generation (C1g) has its own id.
- Not reversible. Nothing may recognise a peer workflow by its spelling: the cancel cascade, the
  lease (`exists`, `retire`), the redelivery and the docs compute the id with `peer_workflow_id`.
  There was no `startswith("peer-")` anywhere in `src/`; the only readers were the walkers that
  built the string themselves, and they now call the function.
- `PEER_WORKFLOW_NAMESPACE` is a constant that must never change (it is part of every id issued).
- *Migration 0029* (`peer_messages.turn_id` TEXT) is kept: it is a shipped migration id, its
  `ALTER` and the rebuilt `transcript_entries` view are harmless, and it still lets the port accept
  an asking turn id that is not a UUID (the conformance suite pins that). It is no longer REQUIRED
  by the peer workflow ids.
- *Replay shape.* No step name or order changed. But the workflow id of every in-flight peer
  workflow did: one started under `.4` as `peer-<id>` is unknown to the new scheme, and the lease
  would start a duplicate next to it. The pinned application version moved
  `agent-core-turns.4` -> `.5` ONCE for the whole D33 batch (C1j changes no step either), which
  strands the old workflows deliberately. Drain guidance is in OPERATIONS.md ("Deploying the UUID
  peer workflow ids").
- Not covered by the new test: a human request or an approval of the callee (its `turn_id` is the
  same UUID in `human_requests`, so the same fix covers it, but the scripted callee asks nobody);
  the audit trail of its tool call is asserted.

**C1j.1 · retiring a workflow of another version is opt-in (R1-001).** D32's `exists` treated a
workflow left OPEN under another application version as absent, and the lease then cancelled it
(`retire`) and reran the ask. During a rolling deploy the other version's executor can be alive:
that workflow may be running right now, or be parked on a person. DBOS offers no liveness signal
for an executor (the pinned version exposes an executor id on a workflow, no heartbeat), so the
evidence "the old executor is gone" can only come from the operator.

- `Settings.retire_other_versions` (`AGENT_CORE_RETIRE_OTHER_VERSIONS`, true/false/1/0, default
  false; anything else stops startup) is passed to `DurablePeerTurnStarter(retire_other_versions=)`.
  OFF, a workflow open under another version counts as existing and nothing cancels it (an ask
  behind it stays `delivered` until the operator acts). ON, the D32 behaviour: absent, retired,
  rerun as the next generation.
- Chosen over automatic detection (a version is "gone" when it is not the latest application
  version in DBOS' table) because that table records deploys, not live executors, and a wrong
  guess cancels a person's pending approval. The cost is that after the `.5` bump the operator
  must either drain or set the flag once the old executors are stopped (OPERATIONS.md).

**C1j.2 · retire-then-requeue converges.** `retire` is the engine cancel followed by the cancel of
the asks the stranded run made, and the requeue is a third write. A failure or a death between
them left the row `delivered` behind a `CANCELLED` workflow, and `exists` (true for every finished
workflow) kept the row out of reach for ever. Now `exists` is False for a workflow that ended
without a result (`CANCELLED`, `ERROR`); a `SUCCESS` is still True. `retire` can be run again on a
`CANCELLED` workflow and finishes the nested asks (`cancel_open` ends each ask once, so it is
idempotent). A cancelled workflow is requeued without the operator flag: no executor of any
version runs it. Tested with a mailbox that fails `cancel_open` once (the retry converges and the
rerun answers), and with a workflow cancelled directly (the death between retire and requeue).
`ERROR` is not a new risk: a callee that raises writes the failure answer first, so its row is not
stale. Known remaining gap: asks already ended by a `cancel_open` that then failed before their
workflows were cancelled are not found again (D32).

**C1j.3 · the cancel cascade reaches every generation.** `_peer_workflow_ids` walked generations
"until DBOS does not know one", so a lost generation (its workflow never started) stopped the walk
and a live later one was never cancelled. The row's `redeliveries` is the generation, so the port
gained `AgentMailbox.redeliveries(correlation_id) -> int` (0 when unknown, and always 0 for the
A2A adapter, which has no lease) and the cascade names `0..redeliveries`. Ids DBOS does not know
are cancelled harmlessly. Pinned by the shared conformance suite and by a real-DBOS scenario
(gen 0 retired, gen 1 lost, gen 2 live: `cancel_turn` cancels gen 2).

**C1j.4 · an A2A answer with no token on file is logged.** `A2AAgentMailbox` keeps the per-ask
answer tokens in process memory (it has no table, by design). After a restart a genuine late answer
finds no token, and the port says an unknown id is a no-op, so it was dropped without a trace. It
still is dropped (nothing can be verified), but `answer` now logs a WARNING with the correlation
id (an address) and never the token, and the OPERATIONS runbook says A2A tokens do not survive a
restart: an ask in flight over A2A across a restart is lost to its asker's reply timeout. Persisting
the tokens would add a table to the one adapter that has none and is a design change, not made here.

**C1j.5 · the attempt ledger borrows pooled connections.** `PgAttemptLedger` opened a new
connection per call (every workflow entry, park, resume and forget). It now takes the repo's
`ConnectionFactory` seam and production passes `PoolConnections(domain_pool)`, the pool every
other application-database adapter already uses; a pooled connection commits when its context
exits, still one statement per transaction and never the step's. Fail-open is unchanged: a closed
pool or a database that is down raises, the workflow body logs it and the turn runs uncounted.
(`PgAgentMailbox` also connects per call; not part of this advisory, left as is.)

**C1j.6 · migrations 0029 and 0030 are applied by the production entry point.**
`test_apply_all_migrations_creates_the_peer_turn_id_text_column_and_the_attempt_ledger` runs
`apply_all_migrations` on a fresh database and asserts both ids are recorded, `peer_messages.turn_id`
and the rebuilt `transcript_entries.turn_id` are TEXT, and `workflow_attempts` has its columns. It
is a characterization test: it passed when written (nothing was broken), so there is no RED.

**C1j.7 and C1j.8 · tidy.** `DEFAULT_MAX_TURN_ATTEMPTS` is defined once, in `turn_workflow` (where
the budget is enforced), and `composition` imports and re-exports it. Dead test helpers removed:
`_to_the_third_agent` (test_poison_budget), `now_plus` (mailbox_conformance), `utcnow`
(harnesses). The callee-budget test's `drive(launch_drop)` is now three named phases
(`first_process`, `second_process`, `third_process`) like the restart tests.


## D34 · Agent-comms v2: slice 9 advisories (C1k)

Non-blocking advisories of the slice 9 review. No step name, step order or workflow id of an
in-flight workflow changed, so the pinned application version stays `agent-core-turns.5`.

**C1k.1 · the lease and the cascade still see `.4` workflow names (R3-001/R4-001).** The uuid5
rename (D33) made `DurablePeerTurnStarter.exists` look up only the new id, so a callee a live `.4`
executor was still running (`peer-<corr>`, or `peer-<corr>-r<n>`) looked absent and the lease
requeued its ask even with `AGENT_CORE_RETIRE_OTHER_VERSIONS` off: the gate D33 added was bypassed
exactly during the rollout it protects. `legacy_peer_workflow_id` names the old spelling;
`exists` asks the attempt's current id first and then the legacy one, with the same rules (a
workflow open under another version exists unless the flag is on; `CANCELLED`/`ERROR` are absent;
`SUCCESS` exists). `retire` and the cancel cascade (`_peer_workflow_ids`) cover both spellings, and
`cancel_open` is called with the legacy id too (the old callee's own asks were made under it).
`legacy_peer_workflow_id` can be deleted once no `.4` workflow can exist.

**C1k.2 · a callee that keeps failing is not rerun for ever (R4-002).** `exists` reads `ERROR` as
absent (D33), and the poison budget is per workflow id, which changes with every generation, so a
callee that fails deterministically was requeued each lease without end (`redeliveries` was
uncapped). `AGENT_CORE_PEER_MAX_REDELIVERIES` (default 3, `0` = never rerun; a negative or
non-numeric value stops startup) caps it. The cap is applied where the ask is claimed
(`dispatch_next_ask(max_redeliveries=)`): a claim whose `redeliveries` exceeds the cap is closed
with the fixed failure answer through the same `_answer_failed_ask` path as a failed start (the
asker is woken) and reported as `AskDisposition.ABANDONED`. Chosen over capping inside
`reclaim_stale` because the closing needs the row's answer token and asking turn, which the claim
already carries, and the port stays unchanged; the price is one extra requeue-and-claim round trip
for the last generation.

**C1k.3 · the attempt ledger fails open quickly (R3-002).** It borrows `domain_pool`, whose
checkout timeout is 30 s, at every workflow entry. `PoolConnections` gained an optional
`timeout` and `attempt_ledger_for(pool)` builds the ledger with
`ATTEMPT_LEDGER_CHECKOUT_TIMEOUT_SECONDS = 1.0`: behind a saturated pool the call raises within a
second and the workflow body runs uncounted (C5, unchanged). A dedicated pool was rejected: one
more pool per process for a statement per entry.

**C1k.4 · tidy.** `retire` uses an early `continue` on a positive condition instead of the double
negation; OPERATIONS.md no longer says the lease always retires other-version workflows (it is
gated by D33) and its duplicated/garbled sentence about the workflow is fixed; the test
`test_peer_durable_callee_postgres.py` lost its dead `seen["peer_status"]`/`seen["report"]` and
opens the pools and DBOS inside the `try`, so a failed set-up still cleans up.

## D35 · Agent-comms v2: the structured peer result (C8)

A peer ask used to resolve to one of several unrelated strings (the answer inside the untrusted
delimiters, or one of five plain sentences of ours). Now every outcome is one `PeerResult`
(`domain/peer_result.py`), rendered as a compact tag the model reads:

    <peer id="billing" session="peer~case-7~..." status="ok" truncated="true">
    <untrusted-tool-output> ... </untrusted-tool-output>
    </peer>

- `status`: `ok` (answered), `error` (the callee failed or was closed: the stored answer is the
  workflow's own fixed failure sentence, recognised by exact match), `timeout` (no answer inside
  `reply_timeout_seconds`), `cancelled` (a repeat of an ask already cancelled), `refused` (never
  sent: policy, unknown peer, malformed call). `id` is the target as the model wrote it,
  `session` the derived peer session (absent for a refused ask), both escaped as attributes.
- The text is exactly what the model reads between the tags. Peer-written text keeps the single
  untrusted boundary the mailbox port applies (non-negotiable #10: nothing wraps twice); our own
  sentences stay unwrapped. The delimiter neutraliser now also removes `<peer`/`</peer` from a
  peer's text, so an answer cannot close or forge the envelope.
- **Size cap.** `peers.max_answer_chars` (new key on the asking profile, default 50000, a positive
  whole number or the profile does not load). `mailbox.cap_wrapped_answer` takes the port's
  wrapper apart by its exact shape, cuts the peer's text and re-wraps it, so the boundary stays
  whole; the result carries `truncated="true"`. The stored row is never touched. Chosen over a
  new `read_answer(max_chars=)` port parameter, which would have changed nine test doubles and the
  A2A adapter for no gain in behaviour.
- **Replay shape: unchanged.** No step added, renamed or reordered; `_step_read_peer_answer` and
  `_step_peek_peer_answer` take two more arguments (the request and the target), which DBOS does
  not validate on replay. An in-flight turn replays its already recorded step results, so the
  pinned application version is NOT bumped by C8.
- **Content hashes.** `max_answer_chars` is a field of `PeerPolicy`, so every profile's content
  hash changes once and the registry assigns each profile its next version at the first load
  (D20). Nothing else changes for them.
- Not in the envelope yet: `data` (C9 adds typed answers) and `usage` (the callee's token use is
  not persisted on the row; carrying it needs a column and a port change). Both are additive
  changes to `PeerResult.render`.

## D36 · Agent-comms v2: repeat questions are fresh asks (C13)

`peer_messages` was unique per `(turn_id, target_agent_id, question)`. The same question asked again
in a LATER round of the same turn (legitimate: the situation changed, or an earlier ask timed out
and was cancelled) was folded into the first row and returned the first answer, or the `cancelled`
end state of the ask that timed out. Two identical calls in ONE model step collapsed to one handle
too, which left one of the two tool calls without a result.

- **The key.** `_step_ask_peers` now files every ask under `r<round>.s<position>.<tool call id>`
  (`_ask_key`): the round of the turn's collect loop, the position in that round's tool-call-id
  order, and the provider's call id. All three are values the body already holds, so a replay of
  the same round derives the same key. The key is the `request_key` that already derived the
  correlation id (C2) and the A2A `messageId`; no port signature changed.
- **The constraint.** Migration `0031` adds `peer_messages.request_key` and drops
  `uq_peer_messages_turn_target_question`. A keyed ask is idempotent through its primary key
  (`ON CONFLICT (correlation_id)`), so a replay is still one row, one claim, one peer run. An UNKEYED
  ask (random id, no `request_key`) keeps its old idempotence through a partial unique index over
  the rows whose `request_key IS NULL`. The in-memory reference mailbox does the same; the A2A
  adapter already sent a per-key `messageId`, so every adapter now treats a repeat as fresh and the
  conformance suite asserts it (`test_the_same_question_under_a_new_request_key_is_a_fresh_ask`).
- **What stays.** The round's defence for an adapter that hands back a handle it already gave out
  (`timed_out` re-ask: read the stored answer or resolve as `cancelled`) is kept; no shipped
  adapter triggers it any more and its tests use a mailbox that folds on purpose
  (`fold_repeats`).
- **Replay shape: `agent-core-turns.5` -> `.6`, ONCE for the C13 batch.** No step name or order
  changed, but the correlation id (and so the peer workflow id) a re-executed ask step derives
  changed: an ask step interrupted under `.5` (row inserted, step not recorded) would post a second
  row under `.6` and run the callee twice. In-flight `.5` turns are stranded on purpose; the
  runbook is OPERATIONS.md "Deploying the ask keys". Migration `0031` breaks the `.5` ask statement
  (it names the dropped constraint), so it is applied only by a `.6` build, after the drain. C14 adds
  nothing to the replay shape and did not need a second bump.
- **Known quirk left.** Rows written before `0031` have `request_key IS NULL`, so they stay under the
  old per-question uniqueness; a keyed ask never collides with them.

## D37 · Agent-comms v2: a dynamic peer catalogue (C14)

The `ask_peer` tool used to be one static function: `target: str`, no list of agents. Personas
carried hard-coded lists of who to ask, so a new specialist meant a prompt edit, and a peer the
policy forbade was still offered and then refused at call time.

- **Per-turn tool view.** `ask_peer` is now a `Tool` with a `prepare` hook (Pydantic AI runs it
  before every model request with the run's deps). It reads the turn's `peer_catalogue` from
  `TurnContext` and returns a tool definition whose description appends `Available peers:` with one
  line `- <id>: <what it is for>` per peer and whose `target` schema is an `enum` of exactly those
  ids. The cached `Agent` is unchanged and shared; only the per-run view differs, so a catalogue
  never crosses callers. With nobody available the hook returns `None` and the tool is not offered.
  A turn with no computed catalogue (`peer_catalogue is None`: a runner built without
  `peer_profiles=`) keeps the old static shape.
- **Who is listed** (`adapters/driven/peers/catalogue.py::build_peer_catalogue`): the asker's
  allowlist entries whose profile is loaded, that `hop_limit.authorise_hop` would allow (both
  sides enabled, both allowlists, with `hop=0`), and that `ToolPolicy.decide(rules, "ask_peer",
  {"target": id})` does not DENY. NEEDS_APPROVAL stays listed. Today rules match on the tool name,
  so one verdict covers every target; per-peer rules (C15) plug into the same call and hide a denied
  peer with no further change here. The hop COUNT is not consulted: a resumed turn does not carry
  it, and the gate still refuses too deep an ask at call time with a readable reason.
- **`AgentProfile.description`** (new optional string key, part of the content hash): what the
  agent is FOR, written for other agents. The catalogue line is that text, else the display name
  from the asker's allowlist, plus the allowlist's capability hints. The four shipped profiles got
  one. `PydanticAgentRunner(peer_profiles=)` is wired by `build_container` with the loaded profiles.
- **Replay shape: unchanged.** The tool view is computed per model request, which DBOS records as
  a step result; no step was added or reordered. No version bump beyond `.6` (D36).
- **Follow-up outside this repo state:** the `vertical/glazed` orchestrator persona still names its
  specialists. It can drop that list once it runs on this branch and gives each specialist profile
  a `description`; this change did not touch that branch.

## D38 · Agent-comms v2: slice 10 advisories (C1l)

Non-blocking advisories of the slice 10 review. No workflow step name, step order or workflow id
changed, so the pinned application version stays `agent-core-turns.6`.

**C1l.1 · the outcome of a peer answer is data, not a text match.** `<peer status>` used to be
`error` when the stored text equalled one of our two fixed failure sentences, decided AFTER the
`max_answer_chars` cut. A peer (or a hostile page it read) could write that sentence and look like
a failure, and a short cap cut a real failure sentence into an `ok` answer. Now the workflow records
the failure on the row beside the text: `AgentMailbox.answer(..., failed=True)` (new keyword, default
False) and `AgentMailbox.answer_failed(correlation_id)` (new verb) read it back; `_peer_envelope`
asks the row BEFORE it caps the text. Written with `failed=True`: the callee workflow that raised,
the one that closed unanswered, the poison-budget settle of a callee and the worker's failed-start
close. Migration `0032` adds `peer_messages.answer_failed BOOLEAN NOT NULL DEFAULT false` (rows
written before it read as ordinary answers, which is what they were unless a callee failed; the
asker of such a row sees `ok` with the sentence, not a regression in safety). The first answer
decides the flag, like the text. The A2A adapter keeps the flag in process memory with its answers.
Chosen over a typed prefix inside the text (still text, still spoofable) and over classifying
before the cap only (fixes the cut, not the spoof).

**C1l.2 · only callee failures count toward `AGENT_CORE_PEER_MAX_REDELIVERIES`.** Every lease
requeue bumped `redeliveries` and the cap read it, so a worker that died between claim and start,
or a workflow retired during a deploy, used up the cap of an ask whose callee never failed.
`redeliveries` stays the generation (it names the workflow id); migration `0032` adds
`callee_failures INTEGER NOT NULL DEFAULT 0`, claimed rows carry it as `PeerAsk.failures`, and the
cap compares `failures`. `reclaim_stale(failed=)` takes the starter's verdict on a generation it
judged dead, asked BEFORE `retire` rewrites its status: `DurablePeerTurnStarter.failed` is true only
for a workflow that ended `ERROR` (both id spellings). A starter without the probe
(`CalleeFailureProbe`) counts nothing, which only makes the cap more lenient. Trade-off: an ask
whose worker keeps dying is no longer abandoned by this cap; the asker's `reply_timeout_seconds`
still bounds how long it waits.

**C1l.3 · tidy.** `test_migrations` proves `0031` and `0032` (and their columns) are applied by
`apply_all_migrations`; the stale `_step_peek_peer_answer` docstring and the re-ask comments now say
that since C13 a repeat is a fresh ask (the handle-given-out-again path is the defence for an
adapter that folds repeats); `test_uuid_peer_workflow_ids_moved_the_pinned_application_version_to_five`
is removed (the `== .6` test and the ">= 4" test cover the same ground); the dead `answers`
parameter of `test_peer_catalogue._profile` is gone.

## D39 · Agent-comms v2: the fan-out policy (C10)

A model step that asks several peers always waited for every one of them (`_collect_round`, D28),
even after one had already failed and the others' answers could no longer help. `peers.fan_out` on
the ASKING profile chooses (`domain.peers.FanOutPolicy`, part of the content hash, an unknown value
refuses to load):

- `all_settled` (default): the behaviour before C10.
- `fail_fast`: when an answer arrives that the callee recorded as a failure (`answer_failed`, C1l),
  the asks of the same step still out are cancelled with the C4 cascade (`_step_cancel_peer_asks`:
  the row first, then the callee workflow and what it asked in turn) and resolved as
  `<peer ... status="cancelled">` with a sentence saying why; the round then resumes once. An ask the
  cascade did not end (it was answered in the meantime) stays awaited and its answer is read as
  usual. A cancelled sibling's late wake is skipped like a timed-out ask's.

Decisions inside it:

- **What trips it: a callee failure (`error`) only.** A timeout needs no trip (every ask of a round
  shares the deadline, so the siblings time out together and are already cancelled by the C4 timeout
  path). A `refused` ask never left (policy, an unknown peer, the parallel cap of C11) and its live
  siblings were requested knowingly. The re-ask peek path (a mailbox that hands back a handle it
  already gave out; no shipped adapter does since C13) does not trip it either.
- **A person's approval in the same round is still waited for.** `fail_fast` stops the AGENTS, not a
  human's decision; the turn resumes once, when the person has decided.
- **The policy is read in a recorded step.** `_step_peer_fan_out` runs right after
  `_step_peer_reply_timeout`, so a replay after a profile edit takes the path the first run took.
  `_step_read_peer_answer` now returns `(tool_call_id, envelope, failed)`.
- **Replay shape: `agent-core-turns.6` -> `.7`, ONCE for the C10/C11/C15/C17 batch.** A new step in
  the default path and a changed step result: a turn suspended under `.6` replays into another
  sequence, so it is stranded on purpose (OPERATIONS.md "Deploying the fan-out policy"). Nothing was
  deployed under `.3`-`.6`, so on this branch it is a formality; C11, C15 and C17 add no step and need
  no second bump.

## D40 · Agent-comms v2: parallel and concurrent ask caps (C11)

A model step could fan out any number of asks and every one of them ran at once, each a full turn
with model calls. Two caps on the ASKING profile (`PeerPolicy`, positive whole numbers or the profile
does not load, both part of the content hash), with Pi's numbers as defaults:

- **`max_parallel_asks` (default 8): refuse the excess.** `_step_ask_peers` counts the asks that
  would leave; the first N, in `tool_call_id` order, are asked, and each one beyond it resolves to
  `<peer ... status="refused">` saying which limit it hit and that it can be asked in a later step.
  An ask refused for another reason (policy, an unknown peer, a malformed call) holds no slot, so it
  cannot push a good ask out. No step changed: the check lives inside the existing ask step.
- **`max_concurrent_asks` (default 4): queue the rest, on the dispatch side.** The answering side is
  where "running" is known and where an ask is turned into work, so the cap is enforced in
  `claim_next`, not in the workflow (which would need a new wait and a new step). The asking policy's
  value is stored on the row (`peer_messages.max_concurrent`, migration `0033`; NULL = no cap, for
  rows of older builds) because the claiming worker holds neither the asker's profile nor its turn.
  A queued ask is claimable only while its ASKING turn (`turn_id`, across every target) has fewer
  than `max_concurrent` asks `delivered` and unanswered. The count runs under a per-turn advisory
  lock held to the end of the claim transaction, and the claim itself is one guarded `UPDATE ... WHERE
  state = 'queued'`, so two workers racing cannot both take the same ask nor both see the last free
  slot. The old single-statement `FOR UPDATE SKIP LOCKED` claim could not express the cap; the
  conformance suite (Postgres, in-memory, controlled) now asserts it. A nested callee has its own
  turn id and so its own cap.
- **What a waiting ask costs.** It stays `queued`, so the lease (which only looks at `delivered`)
  leaves it alone, but the asker's `reply_timeout_seconds` keeps running: a cap low enough to keep an
  ask waiting past it times the ask out. A `delivered` row whose worker died holds its slot until the
  lease requeues it (C3). The A2A adapter has no claim side (the remote peer owns its queue), so it
  does not enforce the concurrent cap.
- **Replay shape: unchanged** (`agent-core-turns.7` stays). Migration `0033` is additive.

## D41 · Agent-comms v2: per-peer permission rules (C15)

Policy rules matched on the tool name only, so one verdict covered every peer: an operator could not
say "billing freely, the fraud desk only with a person's agreement, payroll never". A rule may now
carry a `resource` (`PolicyRule.resource_pattern`, YAML `resource:`, stored in the `definition`
jsonb as `resource_pattern`), narrowing it to what a call acts on. The resource of a call is
`domain.policy.resource_of(tool, arguments)`: today only `ask_peer`, whose `target` is the peer id.

- **Same grammar and same precedence as the rest of the engine.** The pattern is exact or a trailing
  `*` prefix, lowercased, like `tool_pattern`. Verdicts reduce by `EFFECT_PRECEDENCE`, DENY beats
  NEEDS_APPROVAL beats ALLOW, so `allow ask_peer` plus `ask fraud` means fraud asks a person and
  every other peer is allowed. The task text said "ordered, last match wins" (OpenCode); that was NOT
  adopted: the rule store is an unordered set of rows (file order is not stored and "decides
  nothing", `load_policy_rules`), a later allow overriding an earlier deny is the very thing D-policy
  forbids, and a second ordering rule next to the first would be the silent-bug shape CLAUDE.md warns
  about. "Ordered" would be a product change to the whole engine, not to peers.
- **A narrowed rule is about the peer, not the tool.** It matches only a call that names a resource
  that fits. A call without a target matches no peer rule (the workflow refuses a malformed ask
  anyway); a rule without `resource` matches every call, so every rule stored before C15 means what
  it did. The effect word `ask` is accepted by the rule file as a spelling of `needs_approval`; the
  stored value is unchanged.
- **`ask` is the existing approval, before dispatch.** `PolicyEnforcement.before_tool_execute`
  already calls `decide` with the call's arguments, so a NEEDS_APPROVAL on `ask_peer`+`fraud`
  suspends that call for a person (`PendingKind.APPROVAL`, with the rule's reason) while its
  siblings are asked as usual; once approved the call becomes the usual delegation. No new
  workflow step.
- **`deny` hides and refuses.** The C14 catalogue already drops a peer whose
  `decide(rules, "ask_peer", {"target": id})` is DENY, so it leaves the `target` enum; a model that
  names it anyway is refused by the hook with the rule's reason as the tool result.
- **Offering the tool.** `filter_toolset` decides with no arguments, where no resource is named. A
  tool whose only grants are per-peer rules would match nothing and be dropped before the model could
  name a peer, so a tool that no rule matched, but that a resource rule allows or asks for, is kept
  (`RuleSet.grants_some_resource`); a tool-wide DENY still removes it.
- **Spawn of subagents (W5)** will call the same `decide` with its own resource argument: add the
  tool to `RESOURCE_ARGUMENTS`.
- **Replay shape: unchanged.**

## D42 · Agent-comms v2: a single topology source (C17)

The shape of "who may ask whom" was written in three places that nothing compared: each profile's
`peers.peers` allowlist, its `peers.max_hops`, and the policy rules for `ask_peer`. When they drifted
(a specialist allowing another specialist, a one-sided entry, a limit of 1 under a two-deep chain, a
rule denying an edge the profiles promise) the only symptom was a refused ask on a live turn, with
the cause three files away.

- **Where it is declared: `peers.topology` on the hub/root profile.** Options were a separate
  "team file" or a key on the orchestrator profile. The repository has no team file concept and
  adding one means a new loader, a new directory and a second thing to keep in step with the
  profiles; a key on `PeerPolicy` rides the existing profile loader, content hash and review. Exactly
  one profile declares it (two declaring is itself a violation); everything reachable from it over
  allowlist entries is checked. Values: `star`, `tree`, `mesh`; absent = no check, so every profile
  that exists today starts as before. The key changes every profile's content hash once (a new
  field), like `fan_out` and the caps did.
- **Entries are mutual, because the gate is.** `authorise_hop` lets A ask B only if both list each
  other and both `max_hops` exceed the hop count. So a star spoke LISTS its hub (else the hub's asks
  to it are refused) and must list nobody else; in a tree a profile lists its parent and its own
  children; in a mesh every entry must be reciprocated. The hop limit is checked at the depth an edge
  is made (0 for a hub or a mesh, the parent's depth in a tree) against the smaller of the two limits.
- **Policy rules: only what is certain.** With a managed policy directory the rules are loaded at
  startup (`load_policy_rules`, files only: the database is not read, `build_container` does no I/O)
  and each asking edge needs a rule that can allow `ask_peer` to its target, with no rule that DENIES
  it for every caller. A deny limited to roles, channels or a tenant is a narrowing and is not drift;
  with no policy directory the rules live only in the database and are not checked.
- **Where it runs and what it prints.** `composition._refuse_topology_drift`, right after the
  toolset check and before anything is wired, raising `TopologyDriftError` (a `ValueError`) with every
  violation, each naming the profiles and the file key to change. The pure check is
  `domain/topology.py::topology_violations`.
- **Not done:** deriving the allowlists from the topology (the declaration validates, it does not
  generate); checking rules that depend on the caller's roles, channel or tenant (a caller is not
  known at startup).
- **Replay shape: unchanged.**

## D43 · Agent-comms v2: slice 11 advisories (C1m)

Decisions taken while closing the review advisories of the caps / rules / topology slice.

- **The claim holds one turn lock, never two.** The claim used to take the per-turn advisory lock of
  every candidate it scanned, skipped ones included, and keep them until commit; two workers (other
  targets) scanning the same two turns in opposite order could each wait for the lock the other held.
  Now the transaction picks the oldest claimable ask in SQL, locks only THAT ask's turn, recounts the
  running asks under the lock and claims. If the recount finds the turn full after all, the
  transaction ends, the turn is excluded and the claim starts again. Rejected: `pg_try_advisory_xact_lock`
  and skip (two workers claiming asks of the same turn would see each other's lock and return nothing
  although an ask was claimable, which the "claims are exclusive" conformance test caught in design).
- **No scan window.** The 100-ask window is gone: the candidate query already leaves out the asks of
  a turn at its cap (an unlocked count that the recount under the lock confirms), so a backlog of
  capped asks cannot hide an uncapped turn's ask. Bounded by one query per claim, not a list.
- **A2A: the task state carries the failure.** An A2A task reported `failed` (or `rejected`) in the
  `message/send` response is recorded as a failed answer (the status message text, or a fixed
  sentence), so the asker sees `status="error"` instead of waiting out its timeout. A failure
  delivered later through `answer(failed=True)` already worked.
- **`max_parallel_asks` order is `tool_call_id` order.** D40 said so and the code does it: the body
  sorts a step's pending calls by id (non-negotiable #7) because the runner may build them from a
  dict. "Call order" in ARCHITECTURE was loose wording and is corrected; model emission order is not
  used since it is not replay-stable by itself. Test: non-sorted ids refuse the last id.
- **Callee expiry is not a redelivery failure.** A callee that expires waiting for a person returns
  normally and answers the asker with the closed text flagged as a failed answer (the asker sees
  `error`: its question was not answered). The workflow does not raise, so the row is answered and
  never requeued, and the redelivery cap (which counts workflows that ended in ERROR) is not touched.
  Pinned by a test; no code change.
- **Replay shape: unchanged** by this item.

## D44 · Agent-comms v2: typed answers (C9)

A specialist often has to hand back a record (a risk score, a list of rows), and prose makes the
asker parse it back. `answer_schema` on a profile makes the contract explicit.

- **Where it lives: top level of the profile, as canonical JSON text.** The callee owns the shape of
  what it answers, so it sits on the callee's profile (not in `peers`, which describes who this
  profile may ask). It is stored as sorted compact JSON so the frozen, hashed profile is the same
  value whatever order the YAML listed the keys in; it is part of the content hash. A schema
  whose root is not an object refuses to load; one whose body is not valid JSON Schema stops startup
  (`composition._refuse_unusable_answer_schemas`), because `domain/` imports no validator
  (`jsonschema` lives in `adapters/driven/peers/typed_answer.py`).
- **The callee runs with structured output; one retry, then it fails.** The runner registers an
  output tool built from the schema (`StructuredDict`) and no longer accepts prose as a way to end the
  turn. Pydantic AI only advertises a `StructuredDict`, it does not check it, so an output validator
  checks it with `jsonschema` and a miss goes back to the model through the library's own retry
  (default one retry). A second miss raises, and the peer workflow answers "failed" exactly as for any
  callee that raises. The turn's text is the object as compact JSON. This applies to every turn of
  that profile, not only peer ones: a typed profile is meant to be a specialist behind `ask_peer`.
- **The asker checks again, and does not show a broken answer.** `_peer_envelope` validates the stored
  text against the callee profile's schema before it adds `data="json"` to the envelope. The answer
  crossed a mailbox, and over A2A it can be somebody else's code; the label is not taken on trust. A
  mismatch is `status="error"` with OUR sentence and none of the peer's content, and trips `fail_fast`
  like any error. A typed answer cut by `max_answer_chars` is plain text (`truncated="true"`): cut JSON
  is no object.
- **`data` is not out of the untrusted wrapper.** The JSON stays between the untrusted delimiters,
  since its strings were still written by another agent (non-negotiable #10).
- **Dependency.** `jsonschema` was already installed through the model libraries; it is now declared,
  and mypy ignores its missing stubs.
- **Replay shape: unchanged.** No step added or reordered; the profile content hash of every profile
  changes once (a new field), as with earlier peer keys.
- **Not done:** listing the schema in the `ask_peer` catalogue so the asking model knows the shape
  before it asks; a per-ask schema chosen by the asker; A2A servers publishing their own schema.

## D45 · Agent-comms v2: session continuation (C12)

A follow-up question to a specialist used to start from nothing: every ask ran in a fresh session
(`peer~<root>~uuid5(correlation)`), so "and the second invoice?" reached an agent that had never seen
the first. `ask_peer` now takes an optional `continue_session`.

- **Opt-in on the asking profile: `peers.allow_continuation` (default false, a strict boolean).** The
  parameter is not offered to the model unless the switch is on, and the workflow refuses it from a
  profile that did not opt in whatever the model writes. It is the asker's switch, as `fan_out` and
  the caps are: the callee's conversation is kept either way.
- **Ownership is checked against rows, not strings.** A session id is a string anybody can write. A
  new column `peer_messages.peer_session_id` (migration `0034`, set on every new row: the derived name
  for a fresh ask, the continued one for a follow-up) lets `PgAgentMailbox.ask` ask "did this asker
  create that session?": the name must be `peer~<root of the asking session>~<key>`, and a row with
  that session must have the same tenant, the same target and the same asking agent, and the same
  case. Every failure is the same `PeerContinuationRefusedError` with one message, so a guess learns
  nothing. An asker of unknown identity owns nothing. Rejected: trusting the root origin embedded in
  the name alone (two tenants can use the same session id; the name says nothing about the tenant or
  the target).
- **The asking agent must match too, beyond the brief's "same root origin".** Inside one case, a
  specialist's session with the orchestrator is not another agent's to read through a follow-up.
- **The worker files the callee's turn under the row's session** when it differs from the derived
  one, in the asker's tenant. No new workflow shape: it is only the session id of the callee's turn.
- **The envelope names the session the ask ran in** (`session="..."`), so a follow-up of a follow-up
  works with the id the model already has. A refused continuation holds no slot of the parallel cap.
- **A2A refuses.** The remote peer owns its sessions and this adapter cannot make it honour one; a
  silent fresh start would be worse than a refusal.
- **Port:** `AgentMailbox.ask` gains `continue_session` (default None); `PeerAsk` gains
  `peer_session_id`. The contract tests that freeze the signature were updated on purpose.
- **Replay shape: unchanged** (no step added or reordered; the existing steps carry one more value).
- **Not done:** expiring or capping the length of a continued session (it grows until the context
  engine compacts it like any session); letting the callee refuse being continued.

## D46 · Agent-comms v2: child approvals visible to the user (C16)

A specialist that suspended on a person was invisible to the person's own conversation: its request
was filed (and addressed on the channel) under the specialist's session, `peer~<case>~<key>`, which
no inbox lists, so the case looked stuck with nothing to approve.

- **The chain is data on the row, not guessed from names.** `peer_messages.root_turn_id` (migration
  `0035`) is the turn a person started that the ask hangs from: the asking turn for a direct ask, the
  root the asking callee was itself started under for a nested one. `mailbox.ask` takes it as
  `root_turn_id` (the workflow passes `request.owner.root_turn_id`, or its own turn id when it has no
  owner). The asking turn, the asking agent and the call were already on the row.
- **`TurnRequest.owner: PeerOwner | None`.** The worker builds it where the claimed row is in hand
  (`peer_turn_request`), as it does the hop count; a model never supplies it. `_step_publish` passes
  it to `HumanGateway.publish(..., owner=None)`, which stores it on the `human_requests` row (four
  nullable columns).
- **Addressing.** A request raised inside a peer session is sent to the case the session belongs to
  (`peer_root_origin`), not to the id of the specialist's own session.
- **Where the approval list lives.** There is no `GET /approvals`; the surface a person's UI reads is
  the transcript of the conversation. So `transcript_entries` lists such a request under the ROOT
  turn's conversation, with the chain in `payload.owner` for the admin audience; a user still gets
  the placeholder with only `since` (#11: not which tool, not which agent). `transcript_conversations`
  counts the request when it decides `is_suspended`, so the inbox's suspended queue shows the root
  case. Both views are re-created from the 0016/0029 definitions with one branch patched, asserted at
  import. A new endpoint was rejected: it would be a second place the redaction rule has to hold.
- **Answering flows back unchanged.** `POST /decisions/{corr}` correlates to the callee's turn id and
  wakes its workflow (C1); the callee then answers the asker. Nothing new.
- **Replay shape: unchanged** (the publish step receives one more value; no step added).
- **Not done:** the existing inbox view still collapses sessions by `session_id` alone, so two tenants
  using the same session id would share a row (pre-existing; the tenant predicate on `page` holds).

## D47 · Agent-comms v2: context visibility (C18)

`PeerPolicy.visibility` (`none | summary | full`) was declared in F9 and consulted by nobody: the
port's own comment promised "redact per `policy.visibility`" and the question always went alone.

- **Applied on the asking side, where the conversation is.** `none` (default) is unchanged byte for
  byte. `summary` appends the Core summariser's text (`ModelSummariser`, the one the context ladder
  uses) over the asker's messages; `full` appends the messages themselves, the newest whole messages
  that fit, with a marker when older ones were dropped. Both are cut to `peers.max_context_chars`
  (new, default 4000, a positive whole number, part of the content hash).
- **Read as the USER audience.** The peer is an agent, not an operator: it is told what the person and
  the agent SAID (user prompts and agent text), never tool calls, tool returns, system prompts,
  reasoning or policy rows. That is also the narrowest projection that exists and it is tenant-scoped
  in SQL, so no new query was written (`TranscriptReader.page`). Rows in the old `{"text": ...}`
  shape are read too.
- **Inside the untrusted wrapper.** The text is fenced with `wrap_peer_answer` (the one boundary,
  forged delimiters neutralised) after a lead-in in our own voice outside the fence: it is the asker's
  data about what was said, not instructions. The conversation holds what a person typed, so it is as
  untrusted as a tool result.
- **A step of its own.** `_step_peer_context` runs once per model step that asks a peer and its text
  goes to every ask of the step. A step because the summary is a model call and the question is part
  of the ask's id (`peer_ask_id`): a replay must hand `_step_ask_peers` the same text. No step for a
  turn that asks nobody.
- **The failure direction is "tell less".** An empty conversation, `visibility: none`, a deployment
  with no source, a failed read, `summary` with no summariser and `summary` when the Core summariser
  fell back to its marked verbatim extract (its model was unreachable) all send the bare question: that
  extract is right for compaction and would be `full` under another name for a peer. A failed read is
  logged.
- **The shipped `support_triage` profile already said `visibility: summary`** ("the peer is told WHY
  it is being asked, not the whole conversation"); it was inert until now and is not any more. Set
  `none` there to keep the old behaviour. `billing_specialist` says `none`.
- **Replay shape: `agent-core-turns.7` -> `.8`, ONCE for the C9/C12/C16/C18 batch.** Only C18 adds a
  step. Stranded `.7` turns: OPERATIONS "Deploying peer context". Nothing deployed under `.3`-`.8`.
- **Not done:** the asker choosing per ask how much to share; a callee-side cap on what it accepts;
  A2A carrying the context as a message part instead of question text (it travels in the text, so it
  crosses the wire unchanged).


## D48 · Agent-comms v2: slice 12 advisories (C1n)

- **The gateway resolves the recipient before it records.** `ChannelHumanGateway.publish` used to
  write the correlation row and then compute the recipient; a recipient that raised left a row that
  made every retry of the step silent, so the person was never asked. The recipient is now settled
  first, and a session id with a `~` that is NOT `peer~<origin>~<uuid>` is addressed as itself
  (`peer_root_origin` still refuses it elsewhere) instead of failing the publish.
- **The asker's conversation is read newest first.** `TranscriptReader.page` gains
  `newest_first: bool = False` (the cursor walks backwards too). `TranscriptAskerContext` reads from
  the end and reverses, so the bound (`_MAX_PAGES` x `_PAGE`) drops the OLDEST messages; it used to
  read from the start and drop the newest, the opposite of "newest kept".
- **Migration 0035 no longer slices migration 0016's text.** The inbox view is its own constant
  (`TRANSCRIPT_CONVERSATIONS_VIEW_SQL`), 0016 is built from it, and 0035 patches that constant, so a
  statement later appended to 0016 is never re-run by 0035.
- Tidying: `_peer_envelope` binds the peer seat once; the mailbox's refusal text is
  `_CONTINUATION_REFUSED_REASON` (the workflow keeps `_CONTINUATION_REFUSED`); the "to_seven" version
  test is named for what it checks; the claim-lock test waits on `pg_locks` for the claiming backend to
  be waiting instead of sleeping.
- **Replay shape: unchanged.**

## D49 · Agent-comms v2: background asks (C19)

A slow specialist used to hold the asking turn for as long as it took. `ask_peer(..., background=true)`
returns a handle at once and the answer comes back later as a new message of the same conversation.

- **Opt-in on the asking profile: `peers.allow_background` (default false, a strict boolean) and
  `peers.max_background_asks` (default 4).** Like `allow_continuation`, the parameter is only offered
  to a profile that opted in and the workflow refuses it from one that did not, whatever the model
  writes. A non-boolean value is a malformed ask. The cap is per CONVERSATION (tenant and session),
  read from the rows (`background_asks`), so it holds across turns; a background ask also takes one
  slot of `max_parallel_asks` in its step, and `max_concurrent_asks` still applies when it is claimed
  (an ask over the cap waits queued). Per-peer rules (C15) and the two-sided gate run unchanged: a
  background ask is the same call to the same step.
- **The asking turn resumes at once.** The ask is posted like any other, but its deferred call is
  resolved in the same round with `<peer ... handle="<correlation id>" status="running">` (the new
  `running` status, `PeerDispatch.started`), so the round waits for nobody and nothing is ever
  signalled to the asking turn for it. Waking a turn that moved on would fail it (an answer for a
  handle it never awaited).
- **How the answer enters the conversation: a new turn.** The callee workflow records the answer
  and then enqueues ONE turn on the owner conversation (same tenant, same session, the asking
  profile, hop 0) through `enqueue_turn`, so it queues behind a turn that is running instead of
  interleaving with it. Its input is text: a lead-in of ours (sent by the system, not the person; the
  reply is data, not an instruction) and the `<peer ... handle=...>` envelope, which is built by the
  same `_peer_envelope` as every other answer (the asker's `max_answer_chars`, the callee's
  `answer_schema`, the untrusted boundary). Rejected: an "inbox" row the next user turn drains (the
  answer would wait for the person to speak again) and waking the original turn (it has ended).
- **Exactly once.** The follow-up's turn id is `background_followup_turn_id(correlation_id)`
  (a uuid5), so a replayed callee workflow, a second one that raced it and a retried delivery all
  name the same turn and DBOS runs it once; the enqueue is in the workflow body like
  `run_turn_window`'s, recorded against the workflow's log, and the text is built in a recorded step.
  A restart between the answer and the enqueue replays into the enqueue. A failing delivery is
  raised, not swallowed (the answer would be recorded and told to nobody); it is not retried by the
  engine, the answer stays on the row (`peer_status` shows `answered`).
- **Whose identity.** The row keeps the asking turn's `CallerIdentity` (subject, channel, roles,
  modality; the tenant is the row's) in a new JSONB column (`peer_messages.background`, migration
  `0036`), because the asking turn has ended by the time the answer exists. The follow-up runs as that
  caller on the channel it asked on, never as a `peer` and never with more than it held when it asked
  (it is a snapshot, not a lookup). The callee itself still runs as a `peer`. A row with no recorded
  asker has no return address and none is invented from the session.
- **An ownership boundary.** `cancel_open` (the asking TURN expired, was cancelled or poisoned) does
  not touch a background ask; `turn_workflow.close_conversation(session, reason)` ends them: rows
  first, then the callee workflows and their nested asks (the C4 cascade, `_cancel_callees`). It is an
  operator/driving entry like `cancel_turn`; no HTTP route was added. A reply timeout does not apply
  (nobody is waiting): the callee's own limits do, and `peer_stop` (C20) ends one.
- **A failed, closed or poisoned callee is reported** as an `error` envelope in the same follow-up.
  An answer the row refused (the ask was stopped) delivers nothing.
- **A2A refuses** (`PeerBackgroundRefusedError`, a refused result for the model): its answers live in
  this process's memory, so it cannot honour a late delivery after a restart.
- **Port.** `AgentMailbox.ask` gains `background_caller`; `background_asks(session)` and
  `cancel_background(session, reason)` are new (frozen by `test_ports_agent_mailbox.py` on purpose);
  `PeerAsk.background_caller`; `PeerOwner.background` carries the return address to the callee workflow.
- **Replay shape: `agent-core-turns.8` -> `.9`, ONCE for the C19/C20 batch.** `PeerDispatch` and
  `PeerOwner` (both pickled) gained fields and the peer workflow records `_step_background_follow_up`
  for a background ask. Stranded `.8` work: OPERATIONS "Deploying background asks".
- **Not done:** a per-ask timeout for a background child; labelling the follow-up in the user
  transcript (it is stored as the turn's input, which the user audience reads as a message);
  retrying a failed follow-up enqueue; an HTTP/console command for `close_conversation`.

## D50 · Agent-comms v2: managing background agents (C20)

A handle from a background ask (D49) is only useful if the conversation can do something with it.
Four tools, the Pi example-23 set, granted with `peers.allow_background`: `peer_list()`,
`peer_status(handle)`, `peer_message(handle, text, mode)` and `peer_stop(handle)`.

- **Ordinary tools behind a seat, not deferred like `ask_peer`.** `ask_peer` suspends the turn
  because another agent must run before there is a result. A listing is a read, and a stop or a
  message is one idempotent write the seat reports at once, so these run inside the turn's recorded
  model step like any tool (a replay re-runs them: a stop twice is a no-op, a message twice is one
  message). The seat (`tools/background.py::BackgroundAgents`) is not a port, for the reason
  `PendingInputs` is not; its implementation, `WorkflowBackgroundAgents`, lives in the workflow
  package because a stop cancels the child's workflow and `dbos` may be imported nowhere else.
- **Their own toolset, granted by the switch.** `composition._grant_peer_mechanism` adds the
  `peer_background` toolset to a profile with `peers.enabled` and `peers.allow_background`; a profile
  that never asked for background asks does not carry the names. `prepare` also hides the tools when
  the runner has no seat or no owner for the turn. They need `ToolPolicy` rules like every tool
  (`policy/rules.yaml`); none is shipped because no shipped profile opts in.
- **Scoped to the owner conversation (tenant AND session id), in SQL.** The mailbox returns the
  background asks of the turn's conversation and a handle is looked up in that list and nowhere else:
  another conversation's handle, another tenant's and one that never existed all read "no background
  agent with that handle", and `stop` never reaches them (tested). The owner (session, profile,
  caller, turn id) comes from the TURN, never from an argument the model wrote.
- **`peer_stop` is the C4 cascade for one ask** (`turn_workflow.stop_background_ask`): the row first
  (a late answer is refused), then the callee workflow and what it asked in turn. A finished or
  already-stopped ask answers "nothing to stop". A stopped child answers nobody: no follow-up turn.
- **`peer_message` is a new background ask that continues the child's session; `steer` is NOT
  mid-turn steering.** A running turn is one recorded model step and cannot be interrupted, so the
  message is delivered the way every ask is: a turn of the child, in the child's own peer session,
  which the queue partition (one turn per session at a time) runs after the turn in flight. That is
  the "queued for the child's next step" the design allowed. `steer` and `follow_up` differ only in
  the framing the child reads ("steering note ... changes how to do the work" versus "follow-up ...
  adds to it"). It goes through the same gate as any ask: both profiles' switches and allowlists and
  the hop limit (`authorise_hop`, with the child's own depth read off its row, since a resumed turn
  has no hop at hand), `allow_background`, `max_background_asks` (a message takes a slot: with a cap of
  one a message to the running child is refused until it finishes or is stopped), and the mailbox's
  C12 ownership check. It does NOT need `allow_continuation`: the owner started the child. Its
  request key is the tool call id, so a replayed call is one message. Its answer comes back as a
  follow-up turn, tagged with the NEW handle. A message to a FINISHED child starts a new turn in its
  session (a follow-up of a follow-up); to a stopped one it is refused. If the child is parked on a
  person's approval, the message waits behind it.
- **What the model reads is ours.** Handles, states, targets, times and its own question, never a
  peer's words: an answer reaches the model only as the follow-up turn, inside the untrusted boundary.
- **Replay shape: unchanged by C20** (the `.9` bump was taken by C19; these are tools, not steps).
- **Not done:** true mid-turn steering; cancelling a queued message by its handle other than with
  `peer_stop` on it; showing the owner conversation's background agents in the transcript API.

## D51 · Agent-comms v2: slice 13 advisories for background asks (C1o)

- **A follow-up turn may not start a background ask (the chain cap is zero).** The follow-up runs
  the asking profile as a fresh turn at hop 0, so nothing stopped it asking in the background
  again, for ever. Two options were weighed: carry a chain generation on the follow-up and cap it
  (`peers.max_background_chain`), or refuse. Refusing is the simplest safe choice: a generation
  needs a new column on the row, a new field on the pickled `TurnRequest`/`BackgroundReturn` and a
  runner path (`resume` carries no request), and a cap of N still permits N rounds of cost nobody
  asked for. The follow-up can still ask WITHOUT `background` (it waits, bounded by the reply
  timeout) and can still `peer_list`, `peer_status`, `peer_stop`. `peer_message` is a background
  ask, so it is refused there too. The check is `AgentMailbox.is_background_followup(session,
  turn_id)`: a follow-up's turn id is `background_followup_turn_id(correlation_id)`, so the turn is
  recognised from the conversation's ANSWERED background asks with no new column, request field or
  runner change, on the replay path and after an approval resume alike. A product that wants real
  chains later adds a generation and lifts the refusal behind a profile switch.
- **Counting, lookup and the listing are three queries.** `background_asks` is a window (default
  `BACKGROUND_LIST_LIMIT`, the newest 50) and the cap, the handle lookup and the stop used to read
  through it, so a conversation with more than 50 asks could always start another and could not
  stop its oldest. `count_background(session, open_only=..., excluding=...)` counts in SQL,
  `background_ask(session, handle)` looks one up by id (still scoped by tenant and session), and
  `peer_list` shows its newest 20 and says how many older ones exist from the store's count.
- **Replay-idempotent cap.** The step that starts background asks counts the conversation's open
  asks leaving out the rows THIS turn already keyed with the keys the step is about to use
  (`BackgroundKeys`: turn id AND request key), then counts its own asks as it re-asks them. A step
  that crashed after its INSERT and runs again gets its handle back instead of a refusal of the
  ask it already made. `peer_message` does the same with its key `msg.<tool call id>`.
- **Poisoned callee uses the one delivery path.** `_settle_poisoned_peer` called an unrecorded copy
  of the follow-up builder. It now calls `_deliver_background_answer`, and a failed enqueue is
  logged under the ask's handle (`ERROR`) without skipping the cancel of what the callee had asked
  in turn. The failure answer stays on the row, as for any lost delivery (OPERATIONS).
- **Smaller:** `_PAGE_NEWEST_FIRST_SQL` is written out instead of derived by `str.replace`;
  the tools' `_seat` is `_background_seat` and returns `tuple[BackgroundAgents, BackgroundOwner]`;
  a missing asker profile in `peer_message` is reported as such, not as an unknown handle.
- **The follow-up still runs with the role snapshot of the asking turn** (`CallerIdentity` kept on
  the row). Roles revoked between the ask and the answer are NOT re-checked: the follow-up carries
  at most the authority the person held when they asked, never more (D49), and no port reads the
  current roles of a subject, so a re-check would need a new one. Documented, not done.
- **Replay shape: unchanged.** No step was added, renamed or reordered (`_step_ask_peers` records
  a result of the same shape); the pin stays `.9`.

## D52 · Agent-comms v2: progress events (C21)

Turns publish what they are doing, so a UI, a voice front-end or an operator can follow a tree of
agents without polling tables.

- **Four facts, one port.** `EventPublisher.publish(ProgressEvent)` (driven port,
  `ports/event_publisher.py`). Kinds: `started`, `tool_called`, `finished`, `failed`. An event
  carries tenant, session, turn, agent (profile), time, the OWNER chain of the turn
  (`EventOwner`: root turn, asking turn, asking profile, ask handle; none for a turn a person
  started), the tool NAME and the policy verdict on `tool_called`, `elapsed_ms` on
  `finished`/`failed` and a short `detail` (`closed` when a turn gave up, the exception CLASS on
  `failed`). No model text, no tool arguments, no exception message: an event is safe to show.
- **Published from every turn, not only peers.** The brackets sit in `_drive_turn`, which both
  `run_turn_workflow` and `run_peer_turn_workflow` run, so a root turn and every callee of its tree
  emit the same facts and the feed of a root (C22) shows all of them. Subagents (W5) reuse the
  same helper.
- **Steps, not side effects in the body.** `_step_progress(started)` right after the turn id,
  `_step_progress(finished|failed)` after the turn. A step is recorded, so a replay publishes
  nothing again and the elapsed time of the closing event is computed from the RECORDED start; the
  event id is `uuid5(turn_id, kind)` and the store keeps one row per id, so even a step re-run
  across a crash leaves one event. The steps are always in the log (a missing publisher just
  skips the I/O), so the replay shape does not depend on configuration. **Replay shape: `.9` ->
  `.10`, ONCE for C21-C23** (OPERATIONS "Deploying progress events").
- **Fail open.** A publisher that is absent or raises costs the event, logged at WARNING, never
  the turn (tested). Events are observability, the audit trail stays the record.
- **`tool_called` rides the audit trail.** `EventingAuditSink` decorates `AuditSink`: the audit row
  is written first, then the event. The runner's policy hook already reports every call to the
  sink before it runs, so no runner or port changed. The hook holds neither the chain nor the
  profile, so the Postgres adapter files such an event under the chain of the turn's `started`
  event (one indexed lookup). A model step is recorded and not re-run on replay, so a call is
  published once per execution of the step (at least once across a crash, like its audit row).
- **Postgres: a table plus `NOTIFY` of the sequence.** Migration `0037` `turn_events`
  (`seq BIGSERIAL` the total order, `event_id` UNIQUE, indexed by `(tenant_id, root_turn_id,
  seq)`). One statement inserts with `ON CONFLICT DO NOTHING` and calls `pg_notify('turn_events',
  seq)` only for a row really written; the payload is the number alone (far below NOTIFY's 8000
  bytes), a listener reads the row. The table also serves replay and snapshots for C22.
- **Not done:** retention (rows are never deleted; OPERATIONS gives the statement), publishing from
  the poison path (a given-up turn publishes no `failed`), events for compaction or delivery.

## D53 · Agent-comms v2: a bounded event feed over SSE (C22)

`GET /turns/{id}/events` streams the events of D52 for the tree under a turn.

- **One broker per process, one bounded queue per subscriber.** `EventFeed` (application layer)
  reads new rows once (`EventReader.after(seq)`, woken by `LISTEN turn_events`, with a poll as the
  floor: a lost NOTIFY costs latency, not events) and hands each to the subscribers of that
  (tenant, root). Every subscriber owns a queue of at most `queue_size` (64) events.
- **A slow client is collapsed to the newest snapshot (Pi), never buffered (OpenCode).** When a
  subscriber's queue is full it is emptied and replaced by one lag marker; everything offered
  while the marker is unread is dropped on purpose. The subscriber's next item is a SNAPSHOT of
  the tree as of now (each turn's state, tools called, elapsed time), after which live events
  continue; events at or below the snapshot's sequence are skipped. Memory per subscriber is
  bounded, the broker never waits on a client, and the client is always TOLD (a snapshot replaces
  what it held) instead of silently missing events.
- **Reconnect resumes from a snapshot.** Every connection, first or `Last-Event-ID`, starts with a
  snapshot (`id:` is its last sequence). The header is accepted and not needed: replaying deltas
  would be a second catch-up path to build and to get wrong, and a snapshot is small. A tree that
  has settled (nothing running) is a snapshot and an `end`, so a reconnect after the end is cheap.
- **Authz is reading the turn.** Same identity headers, same `TurnLookup` with the caller in it,
  same 404 for an unknown turn and another tenant's; then every read is scoped by the authenticated
  tenant in SQL (`EventReader.tree`). The path never names the tenant. Events carry no model text,
  arguments or exception messages (D52), so the feed shows nothing the transcript would hide.
- **Frames:** `event: snapshot|started|tool_called|finished|failed|end`, JSON data, `id:` = the
  store's sequence, `: keepalive` comments every 15 s so proxies do not close a quiet stream
  (`X-Accel-Buffering: no`, `Cache-Control: no-cache`).
- **Known limit.** Order is the sequence at INSERT. Two publishers in different transactions can
  commit a lower sequence after a higher one was read, and the broker does not go back; any
  snapshot corrects it. Events of a turn are published seconds apart from different steps, so the
  window is tiny. Consumers must not build exactness on the feed (the audit trail is the record).
- **Not done:** a per-tenant cap on open streams, authentication beyond the shared header scheme,
  C26 streaming of the final answer (it will publish token chunks as events through this feed),
  trimming of old events (OPERATIONS).
- **Replay shape: unchanged by C22** (the `.10` bump was taken by C21).

## D54 · Agent-comms v2: the task graph view (C23)

`GET /turns/{id}/graph` answers "which agents did this turn ask, what did they ask in turn, and how
is each doing" (Pi's `taskGraph()`), from the mailbox, with no new table.

- **The rows are the graph.** Every ask is a `peer_messages` row naming the turn that asked, the
  target, the asker, its state and its times. A callee's own turn id is a pure function of its row
  (`peer_workflow_id(handle, generation)`, now in `domain/peers.py`, re-exported by the workflow
  module), so a nested ask, whose asking turn IS that id, is linked to its parent with no stored
  parent pointer. The builder (`domain/task_graph.build_task_graph`) is pure; the reader
  (`TaskGraphReader.asks_under(tenant, root)`, `PgTaskGraphReader`) is one tenant-scoped query.
- **Shape.** The root is the turn a person started; children are the asks it made; each child's
  children are what that callee asked, to any depth. Each node carries agent, handle, state
  (`queued|running|answered|failed|cancelled`, the background-ask vocabulary), `background`,
  `asked_at`, `queued_ms` (asked to claimed) and `run_ms` (claimed to answered or cancelled). All
  redelivery generations of a callee (and the `.4` spelling of its id) are searched for children.
- **Unknown ancestry is not guessed.** An ask whose asking turn matches no known callee (a row
  older than the owner chain, a parent beyond the read limit of 500) is returned under `unlinked`,
  never hung on the root, which would claim an ownership nobody recorded. A row that points at
  itself cannot loop (each ask is placed once).
- **Authorised as reading the turn**, like the feed (D53): the same lookup and 404, then the
  authenticated tenant in SQL. Migration `0038` adds `ix_peer_messages_root (from_tenant_id,
  root_turn_id)` for the search (additive).
- **Not in the graph yet:** the root turn's own state and agent (the rows do not know them; the
  agent is the asker of its first ask and `state` is null: C21 events carry both, a later change can
  join them), the follow-up turns of background asks, subagents (W5 adds them as the same kind of
  node), and the live view (the feed of D53 is the live one).
- **Replay shape: unchanged by C23** (the `.10` bump was taken by C21).


## D55 · Agent-comms v2: slice 14 advisories for the feed and the graph (C1p)

The review of C21-C23 approved them with advisories; this closes them.

- **The end of a stream is decided from a snapshot, never from the live events alone.** A missed
  `finished` (a lost wake-up, a read that failed) used to leave the stream open for ever, and a
  root that had finished while a background ask still ran ended it too early. Now every idle period
  (`min(keepalive, reconcile_seconds)`, default 5 s) reads a snapshot; one that differs from what
  the client holds is yielded (it replaces the client's state), and one that shows the tree settled
  is followed by `end`. A snapshot counts as settled only when no turn is running AND no ask under
  the tree is `queued` or `running` (`open_asks`, read through the `TaskGraphReader` port the feed
  now takes; the field is in the snapshot JSON). Cost: one extra tree read and one mailbox read per
  idle subscriber per 5 s, both indexed.
- **A bounded read says what it left out.** `EventReader.tree` returns an `EventWindow` (the NEWEST
  `limit` events, 2000 by default, plus `omitted`, the count of older ones); it used to keep the
  oldest and drop the newest, which froze a long tree at its early state. `TaskGraphReader.asks_under`
  returns an `AskWindow` (the OLDEST `limit` asks, parents before children, plus `omitted`). Both
  counts come from `count(*) OVER ()` in the same statement. The snapshot JSON carries `truncated`
  and `omitted_events`, the graph JSON `truncated` and `omitted_asks`. Over a truncated event
  window a turn whose `started` was cut is still shown (running until a `finished` or `failed`
  says otherwise), so a partial view is never reported settled; `tools_called` is then a lower
  bound. `open_asks` counts only the asks that were read.
- **No connection per call.** The publisher (one write per tool call), the feed's reader and the
  graph reader check connections out of `EventConnections`, one small pool (`min 1`, `max 4`,
  5 s checkout) of their own, opened on first use so building the container still connects to
  nothing. It is not the domain pool or the audit pool: an event is a courtesy copy and must
  neither queue behind a turn's connections nor take one. The wake-up holds ONE long-lived LISTEN
  connection in a background task that sets an event on every notification, so a notification
  published between two waits is no longer lost and a wait costs no connect; it reconnects after
  an error. `Container.close_pools()` closes the three pools and the feed's broker closes the
  listener when it stops.
- **Smaller corrections.** An event keeps at most `MAX_LABEL_CHARS` (200) of any free-form label
  (agent, tool, asker, call, detail; cut with an ellipsis) because the publisher stores them in a
  row the feed re-reads; the NOTIFY payload stays the sequence number alone and the channel name is
  the bound parameter `NOTIFY_CHANNEL`, no longer a literal in the statement. `EventingAuditSink` no
  longer forwards unknown attributes to the sink it wraps (nothing read them); `ProgressEvent`'s
  documentation of `root_turn_id` is corrected (the property is never empty: the owner's root or
  the event's own turn). `migrations.apply_migration` is the one body of the per-module
  `apply_*_migration` test helpers (the task graph's uses it; the others can follow when touched),
  and `routes.require_readable_turn` is the one authorisation rule of `GET /turns/{id}/events` and
  `/graph`.

## D56 · Agent-comms v2: model tiering per role (C24)

Most of what a specialist does when another agent asks it something is mechanical: one lookup, one
format. A smaller model does that as well for much less latency, and the main model stays for the
work that needs it.

- **Declared on the profile, routed in one seat.** `model_fast` (a model id) and `fast_steps` (the
  kinds of step that use it, default `[peer_answer]`; the only kind today). `StartTurn._runner_profile`
  (the one place the runner's profile was already chosen) asks `AgentProfile.for_step(step_kind(request))`
  and the runner gets a copy whose `model` is the fast one. Everything else about the profile,
  the content hash and the stored version included, is unchanged, so policy, audit and the version
  of a turn still name the profile the operator wrote. The runner's agent cache is already keyed on
  the model, so the two models do not share an agent.
- **What a "peer answer" is.** A turn with an owner (`request.owner is not None`): another agent
  started it to answer an ask, foreground or background. It is not "makes at most one tool call":
  the number of calls is not known before the model runs, and switching a run's model half way
  would break its message history. The conservative cut is the kind of turn, and an operator who
  finds a specialist needs the main model removes `model_fast`.
- **A person's turn is never routed.** It is the conversation the person judges the agent by.
  Adding it later is a new `StepKind` value, not a new mechanism.
- **A resumed turn stays on the main model** (resume has no request and no owner at hand, and a
  turn that waited for a person is not the hot path). The history is provider-neutral, so the
  switch is safe; the cost is that a tiered answer that needed an approval finishes on `model`.
- **Validated at load.** `fast_steps` without `model_fast`, an unknown step kind or a blank
  `model_fast` refuses the profile: a typo must not route nothing and look as if it did. The
  preflight checks the fast model's credential and reachability like the main one.
- **Measurement hook.** No new event field: C21's `finished.elapsed_ms` plus the turn row's profile
  version is enough, because turning `model_fast` on is a new profile version (OPERATIONS has the
  query).
- **Replay shape: unchanged by C24.** Routing happens inside the existing model step.

## D57 · Agent-comms v2: the peer answer cache (C25)

A specialist asked the same thing again within a short while (a voice user repeating a question, an
orchestrator retrying) paid for a whole peer turn each time. Remembering the answer makes the second
ask a lookup. The design is conservative on purpose: wrong-but-fast is worse than slow.

- **Two opt-ins, off by default.** The deployment (`AGENT_CORE_PEER_ANSWER_CACHE`) builds the
  adapter; the ANSWERING profile (`peers.answer_cache_seconds`, 0 = off, at most a day) says its
  answers may be reused, because only it knows. Either alone caches nothing.
- **The key is (tenant, target, audience, normalised question, version).** The audience is the
  asking AGENT plus the asking SUBJECT by default (`answer_cache_scope: subject`), or the asking
  agent plus the sorted roles (`roles`) for a specialist whose answers do not depend on the person.
  The default keeps "my balance" from being served to somebody else of the same tenant; the price
  is a lower hit rate across people, which the voice-repeat case does not need. The version is the
  answering profile's content hash plus `AGENT_CORE_PEER_CACHE_DATA_VERSION`, so a changed profile
  or a data load makes older entries unreachable instead of stale. The question is normalised
  (Unicode fold, case fold, whitespace, edge punctuation) and nothing else: no fuzzy matching.
  Rejected: semantic/embedding matching (a near-miss served as a hit is a wrong answer).
- **The gate runs first.** The lookup sits after `authorise_hop`, the continuation, background and
  parallel-cap checks, so a peer the asker may not ask stays refused whatever the cache holds, and
  an ask that needs an approval still gets it (it is resolved before the workflow sees the call).
- **What is cached.** Only an `ok` answer from a non-failed callee, wrapped as untrusted content as
  the mailbox returned it, at most 50k characters, stored UNCAPPED and capped for each asker at
  read time. A typed answer is checked against the schema again on a hit (a miss if it no longer
  fits). Never: an error, a timeout, a refusal, a cancellation, a background ask (delivered later
  as a follow-up turn), a follow-up ask (depends on the earlier exchange), an ask that carries the
  asking conversation (`peers.visibility` other than `none`: the question no longer identifies the
  answer).
- **A hit resolves the deferred call at once** with the usual `<peer>` envelope marked
  `cached="true"` and no `session` (there is nothing to continue). It is recorded in the same
  step result as a refusal or a background start, so replay is unchanged; the step result gained two
  fields that are read tolerantly (`getattr`), so a workflow waiting on peers across the deploy
  replays its old record. **No application-version bump** (`.10` stays).
- **Postgres, because peers run in other processes.** An in-process cache would miss across the
  serve and peer-worker processes and its invalidation would be a lie. `peer_answer_cache` holds a
  digest of the key (not the question), the tenant and target for invalidation, the wrapped answer
  and `expires_at` from the database clock; a write also removes expired rows. Fail-open: any
  cache error is a miss or a skipped store, logged.
- **Invalidation hooks.** The TTL, the profile hash, the data version label, and
  `python -m agent_core peer-cache purge --tenant T [--target A]` (port verb
  `invalidate(tenant, target?)`; a tenant is always required). Rejected: an HTTP admin route (the
  admin surface is a separate identity design, not touched here).
- **Not done:** per-entry hit counters or a metrics export (the hit shows as the absence of a peer
  run), a size cap on the table beyond the TTL purge, caching across different asking agents.

## D58 · Agent-comms v2: streaming the final answer over the event feed (C26)

A voice front-end should start speaking on the first sentence, not after the whole answer. The
root turn's reply now travels over the C22 feed while the model writes it.

- **A side channel; the recorded answer is the truth.** The runner passes Pydantic AI's
  `event_stream_handler` to `agent.run`; the handler cuts the text parts of the response into
  sentence-sized chunks (`domain/reply_chunks.py`: a chunk ends at `.`/`!`/`?`/`…` followed by space,
  a newline or a CJK stop, never inside `3.50`; no sentence is held past 240 characters; the chunks
  put back together are exactly the text) and publishes each as a `delta` event. What the run
  returns, what the step records and what `GET /turns/{id}` shows are unchanged. The chunks are
  published from INSIDE the existing model step, so **no workflow step was added or changed and
  `PINNED_DBOS_APPLICATION_VERSION` stays `agent-core-turns.10`** (no drain note). The step can run
  twice after a crash; each run has its own id in the event (`detail = <run>:<response>:<index>`), a
  snapshot keeps only the latest run's latest response, and a client discards text when the run or
  response changes.
- **`delta` is the one event kind that carries model text.** D52 said events carry none; the
  reasons it is acceptable here: the text is the reply the turn is about to record for the same
  person who may read the feed (it is authorised exactly as `GET /turns/{id}`, D53), it is bounded
  (2000 characters per event, chunks are at most 240), and it carries no tool arguments, no
  reasoning/thinking parts and no tool output. Stored in a new nullable column, `turn_events.text`
  (migration `0040`, additive); every other kind keeps it NULL.
- **Only a person's turn.** `request.owner is None`. A peer's text is another agent's input and is
  never put on the feed; a RESUMED turn is not streamed either (resume has no request, so it cannot
  tell the two apart, and a turn that waited for a human is not the voice path). Streaming a resumed
  turn needs the owner to reach `resume`: a port change, deferred.
- **Snapshots carry the reply.** A subscriber that lagged or reconnected is handed `reply_text` (the
  reply so far) in the snapshot instead of the chunks it missed, the same collapse-to-newest rule
  as every other state (D53), so a slow voice client resumes mid-sentence correctly.
- **Off by default (`AGENT_CORE_STREAM_REPLIES`).** It changes the model call for every
  person-started turn from a plain request to a streaming one. A provider or proxy that does not
  stream would fail those turns, and usage accounting through a stream depends on the provider
  reporting it; both are deployment facts to check before switching on, not defaults to impose.
  This is a conservative reading of the task (the spec does not say off): it can be flipped to on
  by changing one default once a deployment has been checked.
- **Fail-open and quiet.** A publisher that fails disables the stream for the rest of that run
  after one log line; the turn is unaffected. No streaming at all when no publisher is wired.
- **Text before a tool call is streamed too.** If the model writes a sentence and then calls a
  tool, the sentence is a `delta` of response 0 and the final answer is response 1; the snapshot's
  `reply_text` shows the last response only. A client that speaks as chunks arrive may speak the
  preamble; the persona (not this layer) decides whether the model writes one.

## D60 - Slice 15 advisories (C1q): quiet failures, honest counts, a publisher that never waits

Five small corrections to the event stream; none adds a workflow step or changes a recorded value,
so `PINNED_DBOS_APPLICATION_VERSION` stays `agent-core-turns.10`.

- **A failed snapshot read does not end the stream.** The idle snapshot (and the lag and
  terminal-event snapshots) are read through one guard: a failure is logged and the stream carries
  on; the next idle period reads again, and that snapshot is what decides whether the tree settled.
- **A truncated ask window is not settled.** The ask read is oldest-first and bounded, so the asks
  it omitted are the newest ones and none is known to be finished. `open_asks` is the open asks
  read plus `omitted`. This errs towards keeping the stream open (it may report an ask as open that
  has finished); the alternative errs towards telling a client nothing is left while a peer works.
  Counting in SQL was not needed.
- **The reply is published off the model's path.** `ReplyStreamHandler` queues chunks (at most 32)
  and a background task publishes them; the end of each model-response stream flushes what is
  pending, bounded by 5 s (a store that does not answer by then disables the stream for the run,
  like a failing one). When the queue is full, new text is merged into the newest pending chunk of
  the same response up to the 2000-character delta bound; past that it is dropped with one log line.
  The stream is a courtesy copy, so a hole under a stalled store is acceptable; memory is not.
- **The response counter counts model responses.** A response starts at its part index 0 (every
  response has exactly one); the text parts of one response share its number and their chunk
  indexes continue. Before, each text part was a new "response", so a reply with text on both sides
  of a tool call inside ONE response showed only its last part in a snapshot.
- **The publisher has a pool of its own.** `Container.events_publisher_connections` (max 4) serves
  `PgEventPublisher`; `events_connections` (max 4) serves the feed's reader and the task graph. A
  slow read can no longer make a publish wait for a checkout. The LISTEN wake-up already had its
  own connection.

## D61 - Agent modes (C27)

A profile says what it is FOR with `mode: primary | peer | subagent | all` (default `all`, so a
profile that never says behaves exactly as before; a misspelt mode refuses to load, because falling
back to `all` would silently widen who may reach the agent). The mode is part of the content hash.

| mode | a channel may start it | another agent may ask it | a parent may spawn it |
|---|---|---|---|
| `primary` | yes | no | no |
| `peer` | no | yes | no |
| `subagent` | no | no | yes |
| `all` | yes | yes | yes |

- **A mode never grants a reach, it only removes one.** Asking still needs both sides' allowlists and
  switches (`authorise_hop`), spawning needs the parent's `subagents` allowlist (D62).
- **Asking: the gate and the catalogue.** `authorise_hop` takes the callee's mode and refuses with
  `callee_mode_not_askable` (after the feature switches, before the hop limit). The workflow turns that
  into the usual `refused` C8 envelope, so the asking model reads the reason; the catalogue (C14) builds
  from the same gate, so a `primary` or `subagent` agent is not in the `ask_peer` enum.
- **Starting: the door and the backstop.** `POST /turns` refuses a non-entry profile with 422 before a
  turn exists (`create_app(channel_startable=...)`, wired from the container's profiles; an unknown id
  passes and fails later in the profile lookup as it always did). `StartTurn` is the backstop for the
  doors that have no profile map (the scheduler, pull channels, the console): it raises
  `ModeRefusedError` before anything is written or any model call is made, so the turn fails visibly.
- **What is NOT a channel start.** The turn of an agent another agent started (`request.owner` set)
  and the follow-up turn that carries a background answer home. The follow-up runs as the agent that
  asked, which may be a `peer` or a `subagent`; it is recognised through the mailbox
  (`AgentMailbox.is_background_followup`, asked only when the mode already says no), because its turn id
  is a hash and cannot be recognised by shape. With no check bound a turn nobody owns counts as a
  channel start (the conservative reading).
- **Product decision taken conservatively: `peer` is not channel-startable.** The task text only says a
  `subagent` cannot be started by a channel. A `peer` agent exists to be asked; letting a channel start
  it would make `peer` the same as `all` minus spawning. If a deployment wants an agent that is both an
  entry point and askable, that is `all`.
- **Not done here:** the A2A boundary (an inbound A2A task to a `primary`) is a transport adapter that
  does not exist in this codebase as an inbound server, so there is nothing to refuse there yet.

## D62 - Subagents (C28)

`spawn_subagent(agent, task, background?, continue_session?)` starts a profile as a CHILD of the
current turn. It is the second delegation tool, not a second mechanism.

- **Shared machinery, one gate of its own.** The call is a deferred `DELEGATION` like `ask_peer`; the
  workflow's `_step_ask_peers` handles both, so the mailbox row, the idempotent request key, the
  parallel and concurrent caps, `fan_out` (`fail_fast`), the reply timeout, the cancel cascade, the
  background rules (opt-in, cap, a follow-up turn refuses further background), continuation, the
  progress events and the C8 envelope are the peers' own. The child runs as a durable child workflow
  of the parent (`peer_workflow_id`), so two spawns in a step run in parallel and the parent resumes
  ONCE; aborting the parent cancels both (the same `cancel_turn`, expiry and timeout paths). Only the
  gate differs (`authorise_spawn`): the spawner's `subagents.enabled`, the spawner's
  `subagents.agents` allowlist (empty means nobody) and the spawned agent's `mode`
  (`spawnable`). `peers.enabled` and the peers' allowlists do not apply to a subagent; the caps,
  `fan_out`, timeouts and background switches are read from the SPAWNER's `peers:` block, because that
  is where they already live and a second copy of each would drift.
- **The row says what the child is.** Migration `0041` adds `peer_messages.kind` (`peer` | `subagent`,
  default `peer`) and `subagent_depth` (default 0). They travel on `PeerAsk` and on `PeerOwner`
  (`kind`, `subagent_depth`), so the worker starts the child knowing both; `AgentMailbox.ask` gained
  `kind` and `subagent_depth` (passed only for a subagent). The A2A adapter refuses a subagent
  (`PeerSubagentRefusedError`: a peer in another process cannot be a child this turn owns).
- **The envelope names it.** `<peer id=".." kind="subagent" status="..">`: the same tag as a peer's,
  with a `kind` attribute only on a subagent, so a peer's envelope is byte-for-byte unchanged.
- **The task is the whole brief.** A peer ask can carry the asker's conversation (C18); a subagent
  receives its task and nothing else, and its result is untrusted content like any peer answer.
- **No answer cache.** A subagent is a worker for one task, not a specialist whose answers repeat.
- **Policy is C15's.** `spawn_subagent` rules narrow by `resource` (the `agent` argument, via
  `RESOURCE_ARGUMENTS`), `deny` wins, `needs_approval` routes to the human approval, and a denied
  agent is absent from the tool's menu (a per-turn enum, like the peers').
- **The restricted toolset.** A turn that is itself a subagent is not offered `spawn_subagent`
  (C29 makes that depth-aware); the profile's other tools are its own. Its toolset is its own
  profile's, not the parent's.
- **Messaging a running subagent is refused.** `peer_message` would be an ask, and a subagent is not a
  peer to be asked. Stop it (`peer_stop`) and spawn again with the corrected task. Conservative; it can
  be relaxed once there is a use for it.
- **The peer worker polls subagent queues** (`answerable_targets`): any profile with `mode: subagent`
  and any agent a profile with `subagents.enabled` lists, besides `peers.enabled` ones.
- **`PINNED_DBOS_APPLICATION_VERSION` is `agent-core-turns.11`** (once for C28 and C29): `PeerOwner`
  gained fields that are pickled into the peer workflow input, and the round's recorded steps gained a
  `kind`. OPERATIONS has the drain note.
- **Profile key deviation, recorded.** The task text names the depth `subagent_depth` on the profile;
  it is `subagents.depth` inside the `subagents:` block that also carries the switch and the allowlist
  (D63).

## D63 - Depth and ownership limits for subagents (C29)

- **Depth is a count that travels, like `hop` (t-f11-47).** The depth of a turn is the number of subagent
  links in the chain down to it. A spawn writes `parent depth + 1` on the ask row (`subagent_depth`,
  migration `0041`), the worker copies it onto `PeerOwner`, and the child's own asks read it from
  there; a PEER ask passes the chain's depth on unchanged, so a subagent that asks a peer, and a peer
  that then spawns, still count the link above. The task text says "by walking the owner chain":
  every turn's owner already IS the chain's summary (it is built from the row that started it), so
  nothing is walked at spawn time and no query is made inside the replayed step. Rejected: walking
  `peer_messages` upwards at each spawn. A callee's turn id is a hash of its row, so the walk would
  need the parent's call stored on every row, which is a second copy of what the owner already holds.
- **`subagents.depth` (default 1), the stricter side wins.** A top-level turn (depth 0) may spawn a
  subagent (depth 1); that subagent may not spawn (`1 + 1 > 1`), refused with
  `subagent_depth_reached`. Both the spawner's and the spawned profile's limit apply and the smaller
  one decides, for the reason `max_hops` works that way: a limit read off one side is a limit that
  side can raise. The tool's menu hides `spawn_subagent` on a turn whose own profile's limit is
  reached (the restricted toolset of D62); the gate is what enforces both sides.
- **A spawn is a hop.** It travels with `hop + 1` and is refused (`hop_limit_reached`) at
  `min(spawner.peers.max_hops, spawned.peers.max_hops)`, so delegation of every kind (ask or spawn) is
  bounded by one number per profile and one profile cannot be used to launder depth through the other.
  The cost: a profile with `peers.max_hops: 1` cannot spawn a subagent that then asks a peer. Raise
  `max_hops` on the profiles of a deployment that wants both. The default is 2 and all shipped
  profiles keep it.
- **Order of refusals.** switch, allowlist, mode, depth, hop: the first cause that can be fixed in
  one profile is the one the model (and the audit row) reads.

## D64 - Slice 16 advisories (C1r): the worker consents, resume keeps its menu, nothing orphaned

- **R1. A spawn is checked on both sides, like an ask.** `subagents.accept_from` (new key, default
  empty = nobody, strict list like `agents`) is the SPAWNED profile's consent. `authorise_spawn`
  takes the spawner's id and refuses with `callee_does_not_accept_spawner`, after the mode check and
  before the two limits (the first cause fixable in one profile is the one reported). It applies
  whether or not the worker has `subagents.enabled`. The menu (`build_subagent_catalogue`) hides a
  worker that would refuse. Deny still wins: the policy rules (C15) are checked on top, unchanged.
  Rejected: reusing `mode: subagent` as the consent. A mode says what a profile IS; it cannot say
  WHO may use it, and any spawner that listed a worker could otherwise run it. Breaking change for
  profiles that spawn (branch undeployed): add `accept_from` to each worker; none ship in-tree.
- **R2-003 / R3. A resumed subagent keeps the menu its first half had.** `AgentRunner.resume` and
  `ResumeTurn.execute` take `owner: PeerOwner | None = None` (keyword, last); `_step_resume_round`
  hands `request.owner` on and the runner asks the same `_can_spawn(owner, profile)` as `run`, so a
  subagent at its depth limit is not offered `spawn_subagent` after a person approves. The gate
  already refused the spawn; this stops offering a tool that cannot work. Port change: both fakes,
  the frozen-port stub and every `execute` fake of the resume seat gained the keyword.
- **R3 / R4 / R2-004. The reply publish worker is always reaped.** `ReplyStreamHandler.__call__`
  consumes the stream, flushes, and in a `finally` cancels AND awaits the worker (`_reap`), so a
  model stream that raises, a cancelled turn and a flush timeout all leave no task publishing the
  text of a finished turn. The pending queue holds a small `_Chunk` dataclass instead of an untyped
  `[response, index, text]` list.
- **R4. The feed's open-ask count is exact and its snapshot retry is bounded.** `TaskGraphReader`
  gained `count_open(tenant, root)` (`state <> 'cancelled' AND answered_at IS NULL`, tenant-scoped,
  the predicate of `background_state`). When `asks_under` returns a truncated window the feed asks
  for the count instead of treating every omitted ask as open (C1q's rule, which made a tree with
  more asks than the window unable to ever settle); a complete window is still counted from its
  records, with no second query. `EventFeed(max_snapshot_failures=5)`: after that many consecutive
  failed snapshot reads (idle, lag or terminal-event reads) the stream yields `FeedKind.ERROR`
  (SSE `event: error`) and ends; a success resets the count. Rejected: exponential back-off, since
  the retry is already paced by the idle period (at most 5 s) and the bound is what stops it.
- **R4. The `kind` CHECK is added without the long lock (0041 edited, 0042 added).** A plain
  `ADD CONSTRAINT ... CHECK` scans `peer_messages` under a write-blocking lock. 0041 now adds it
  `NOT VALID`; 0042 runs `VALIDATE CONSTRAINT`. Two migrations, not one: a migration string is one
  implicit transaction, where the validation would still run under the first statement's lock.
  Editing 0041 is acceptable because the branch was never deployed; a database that applied the old
  0041 holds a valid constraint and 0042 is a no-op there.
- **Readability items (R2-001, R2-002, R2-005, R2-006), behavior unchanged.** The nested conditional
  that built the subagent keywords of `mailbox.ask` is `_delegation_fields(kind, depth)`; the three
  toolset grants of `_grant_peer_mechanism` are one table of (switch, toolset); the `SpawnRefusal`
  docstring lists the causes in gate order; the end-to-end test's `_kind` (which returned a target
  agent) is `_target_of` and its generator no longer unpacks an unused tuple.

## D65 - Tool replay policy (C30)

- **What a crash can repeat.** A turn step that dies mid-run is run again by DBOS and nothing of the
  run is persisted until it ends, so a tool whose work was done (or half done) can run a second
  time. The repeat that can be recognised is the one that keeps the provider's `tool_call_id`:
  above all the approved call executed by the resumed turn (the id was fixed when the turn
  suspended). A fresh model generation after a restart issues new ids, those are new decisions, and
  they are not recognised (the model, not the journal, then chooses to repeat a send).
- **Tool metadata `replay: safe | interrupted`, default `interrupted`.** The task text says
  "default per existing semantics"; the existing behaviour (run again) is the `safe` one, but a
  default that re-runs a tool nobody vouched for is the unsafe one, and the codebase fails closed
  elsewhere (empty allowlists mean nobody). An unknown value also reads as `interrupted`. The
  built-ins that only read or only defer are declared `safe`, so the default costs a journal write
  only for tools with possible side effects: `peer_message`, `pricing_apply`, `case_notes_append`,
  `freeze_account` and anything a deployment adds. Rejected: default `safe` (silent duplicates).
- **A journal, not the audit row.** The audit sink records intent and verdict and never the outcome
  (port FROZEN, CLAUDE.md #6), so it cannot say "started and not finished". New port `ToolJournal`
  (`begin` -> fresh | started | finished, `finish`, `abandon`), Postgres adapter over
  `tool_executions(turn_id, tool_call_id, tool, started_at, finished_at)` (migration `0043`), keyed
  `(turn_id, tool_call_id)`. `begin` is one atomic upsert, so two attempts racing for one call
  cannot both see it fresh. Rows live on the domain pool; none are deleted (OPERATIONS has a trim).
- **A finished call is not repeated either.** The task text covers started-and-not-finished. A call
  whose end was recorded but whose turn step repeated before the turn was stored would run its side
  effect a second time, which is the same harm, and the result was not kept (storing results is
  more than this task asks), so both get the `interrupted` result; the text says the call "may or
  may not have taken effect".
- **Hook placement.** `before_tool_execute` (after the policy verdict, so a denied or suspended
  call is never journaled) begins the call; `wrap_tool_execute` ends it: an `Exception` or a
  return finishes it, `ApprovalRequired`/`CallDeferred` from the body drop the row (the call never
  ran; the approved run must find it fresh), and a `BaseException` (cancel, kill) leaves it
  started. `after_tool_execute`/`on_tool_execute_error` were not used because the framework routes
  deferrals and retries around them.
- **Recorded steps unchanged**, so `PINNED_DBOS_APPLICATION_VERSION` stays `agent-core-turns.11`
  and there is no drain note; migration `0043` is additive and ignored by a running older build.
- **Wiring.** `PydanticAgentRunner(tool_journal=...)`; `build_container` passes
  `PgToolJournal(PoolConnections(domain_pool))`. `None` (tests, other composers) keeps every call
  as it was.

## D66 - Recorded provider cassettes (C31)

- **Where the recording happens: the HTTP transport.** The model is an `OpenAIChatModel` over an
  `openai` SDK client over `httpx`, so an `httpx.AsyncBaseTransport` sees exactly the request the
  adapter builds and hands back exactly the bytes the SDK parses. `model_for`, `library_mode_model`
  and `proxy_model` gained a keyword-only `http_client` (default `None`: the SDK builds its own, as
  before), the single production-code change of C31.
- **Test support, not product code.** `tests/support/http_cassette.py` (`CassetteTransport`) and
  JSON cassettes under `tests/fixtures/cassettes/`. Nothing in `src/` imports it.
- **Matching is method + redacted URL + JSON body, exactly.** A request that matches nothing raises
  `CassetteMiss` (an `AssertionError`), never falls through to a network call. The cost: a
  pydantic-ai upgrade that changes the request body needs a re-record, which is what a request
  contract test should say. Rejected: matching on the last user message only (hides a changed
  tool schema or model name).
- **Recording is opt-in by environment** (`AGENT_CORE_RECORD_CASSETTES=1`), is the only mode that
  uses an upstream transport, and writes after every exchange. **Redaction happens on the way out**
  (allowlisted headers, masked credential headers, masked credential-looking query parameters,
  body/URL strings scrubbed of the request's own credential, of secret-named environment values and
  of key-shaped text), so a cassette is committable as written. The limit is stated in OPERATIONS:
  a secret that is only ordinary text and in no environment variable cannot be recognised.
- **The shipped cassette is hand-made** (`minimax_chat_hello.json`): the request body is the one
  pydantic-ai really sends (captured against a mock transport), the response follows the documented
  `chat.completion` shape. No real key or account was used. Not a real provider capture, and its
  `note` field says so.
- **A finding, recorded.** The first import of `litellm` downloads its model-cost map from the
  internet. A test that must be offline sets `LITELLM_LOCAL_MODEL_COST_MAP=True` and imports
  litellm before closing the sockets; production behaviour is unchanged (an operator who wants no
  boot-time fetch sets the same variable).

## D67 - Eval harness (C32)

- **Separate from the unit suite, by location.** `Core/evals/` (a package next to `src` and `tests`)
  is outside `testpaths = ["tests"]`, so `pytest` never collects it. `make eval` runs
  `python -m evals`. Ruff and mypy cover it (`pyproject.toml` lists it; `TID251` is allowed there as
  in `tests/`). Nothing under `src/` imports it.
- **Opt-in twice.** A real run needs `AGENT_CORE_EVAL=1` AND the provider's key (the key variable is
  derived from the model id's provider and checked with the same `credential_present` the model
  builder uses). Either missing is a clean skip (exit 0, a line naming what to set, no report).
  `AGENT_CORE_EVAL_MODEL=scripted` runs the machinery offline with the T9 `ScriptedModel`; it needs
  neither. Rejected: skipping through pytest markers (the evals are not tests of this code, they
  are measurements of a model).
- **What a scenario is.** An async function that drives agents through the PRODUCTION
  `PydanticAgentRunner` (real `ask_peer`, real untrusted-content wrapping) over in-memory ports and
  returns named checks. The workflow's hand-off between turns (run the asked agent, resume the asker
  with a `<peer>` envelope) is done in-process in a few lines; DBOS and Postgres are not part of an
  eval, the integration suite owns them. The first scenario, `front_desk_asks_inventory`, checks
  that the front desk delegates, that the specialist states the number its persona holds and that
  the final answer relays it.
- **Repetitions and failure shape.** Each scenario runs N times on a fresh model source. A trial
  that raises or exceeds its timeout is a failed trial carrying `Type: message` only (a traceback
  can hold request bodies). The headline is the pass rate; `AGENT_CORE_EVAL_MIN_PASS_RATE` makes it
  an exit code for CI.
- **Dev tooling may import the test fakes.** `evals` uses `tests.fakes` (scripted model, in-memory
  ports) rather than copying them; the fakes are the ones the suite already trusts.
- **Not verified here.** The real-model path (`RealSource` through `litellm_model_factory`) was
  not exercised: the task forbids calling a paid model in verification, and only the skip path and
  the scripted path were run.

## D68 - Slice 17 advisories (C1s)

- **Journal key = response ordinal + provider call id (R3-journal-id-reuse, R4-journal-key-collision).**
  D65 keyed a call on `(turn, tool_call_id)`. Some providers number calls per response, so a later
  legitimate call could meet an earlier call's row and be answered `interrupted`. The runner now
  passes `r<n>:<tool_call_id>`, `<n>` being the count of `ModelResponse` messages in the run's
  history when the call runs. A repeated step or resume rebuilds the same history, so the same call
  lands on the same key (the approved-call-across-a-crash test still holds). No schema change: the
  key lives in the existing `tool_call_id` TEXT column, so there is no new migration (the port
  gained only the `ToolJournalUnavailableError` type; its three methods are unchanged). Rejected: a per-run call counter (depends on the order calls are dispatched in) and
  the SDK's per-run `run_step` (a resumed run is a new run, so it was not relied on). Rows written under the old
  bare-id key are not recognised after the upgrade (OPERATIONS has the drain note); the recorded
  steps are unchanged, so `PINNED_DBOS_APPLICATION_VERSION` stays `agent-core-turns.11`.
- **Journal checkout is bounded and fails closed (R4-journal-pool-checkout).** The journal uses
  `PoolConnections(domain_pool, timeout=5 s)` (`TOOL_JOURNAL_CHECKOUT_TIMEOUT_SECONDS`), longer than
  the attempt ledger's 1 s because it cannot fail open. A timeout or an unreachable database surfaces
  as `ToolJournalUnavailableError` (port module): from `begin` the tool is not run and the step is
  retried; from `finish`/`abandon` the result is kept (the call happened) and the failure is logged
  (D69).
- **Open-count predicate (R3-open-count-predicate).** Did not hold: the SQL predicate and
  `background_state` agree. A test now runs both over the same rows, including shapes the happy path
  never writes, so they cannot drift apart.
- **Eval env parsing (R2-002, R3-eval-env-parse).** `AGENT_CORE_EVAL_REPETITIONS` was already strict;
  `TRIAL_TIMEOUT` and `MIN_PASS_RATE` were `float(...)` (a typo was a traceback, `nan` or a negative
  timeout was accepted). All three are now parsed first, before the skip decision, and refused by
  name. `MIN_PASS_RATE` must be in [0, 1] (so `1.5` is no longer a way to force a failure).
- **Readability (R2-001, R2-003).** The feed's repeated "what the client now knows" assignment is a
  local `shown(snapshot)`; the tool-journal migration helper uses the shared `apply_migration`.
- **Test (R2-004, R3-source-grep-test).** The source-grep on `_step_resume_round` is a behavioural
  test: the step is driven with a recording `ResumeTurn` and the owner it was handed is asserted.

## D69 - Slice 18 advisories (C1t)

- **Key is versioned (R3-key-history-stability, R4-journal-key-version-skew).** The call key is now
  `j<version>:r<n>:<provider id>` with `JOURNAL_KEY_VERSION = 1` in the runner. `<n>` counts the
  `ModelResponse` messages in the run's history, so it holds only while the history keeps one
  response per model call; a build that changes what `<n>` counts (a compaction that merges
  responses) bumps the version, and its keys never equal an older build's rows. The cost is the one
  D68 already accepted: a row written under another key shape is not recognised, so the call looks
  fresh (OPERATIONS keeps the drain note). Safe now because `r<n>` keys have not left this stack.
  No schema change, no app-version bump (`agent-core-turns.11` stays: recorded steps are unchanged).
- **A failed abandon is reported, not swallowed (R3-abandon-swallow, R4-abandon-swallow-observability).**
  Still non-fatal (the deferral must reach the person), but a failed `abandon` leaves a `started`
  row that the approved run will answer `interrupted`, so it is logged at ERROR as
  `tool_journal_abandon_failed` with turn, tool and key; a failed `finish` is a WARNING
  (`tool_journal_finish_failed`) with the same ids. The codebase has no metrics sink and the audit
  sink records tool calls and decisions, not infrastructure faults, so the stable log token is the
  signal; adding a metrics port is a separate decision. The key is computed once per hook.
- **The journal narrows what it calls unreachable (R3-operationalerror-breadth).** `PoolTimeout`,
  `PoolClosed` and an `OperationalError` without a SQLSTATE (the connect failed) or with class 08 or
  57P01-03 become `ToolJournalUnavailableError`. A deadlock, a cancelled query or a lock timeout
  propagates as itself: the store answered, and calling it unreachable would mislead the operator.
  Consequence: such an error after the tool ran now fails the step instead of being logged.
- **Wording (R2-d68-port-untouched-claim, R2-tool-journal-for-export).** D68 no longer says the port
  is untouched (it gained the error type). `tool_journal_for` is the public builder beside
  `attempt_ledger_for`, kept public so a test can assert which pool and timeout it checks out with;
  its docstring says so.
- **Tests (R3-abandon-untested).** The abandon path is covered behaviourally: a deferred call is
  fresh for the run that executes it, and a failing abandon keeps the suspension and is logged.

## D70 - Addon manifest and where addon code lives (A1)

Phase 2 (roadmap section 5). Addons live in the monorepo at `Agent-Core/addons/<name>/` (user
decision 2026-10-13) and are described by `addon.yaml`.

- **Placement: `adapters/driven/addons_fs/`.** Reading manifests off a disk, parsing YAML and
  validating with pydantic are all adapter work (the layer rules ban `yaml` outside `adapters/`,
  and `domain/` imports nothing external). Nothing in `domain/`, `application/` or `ports/` asks
  for an addon: the composition root resolves the enabled addons once at startup and passes the
  result on as data, the same reason there is no profile-loading port. A port would be a protocol
  with one caller that is itself the wiring. Rejected: a top-level `agent_core/addons` package
  (it would need its own import rules to stay out of the inner layers) and putting the model in
  `domain/` (it needs pydantic and YAML).
- **Strict schema.** Unknown keys are errors (a typo must not silently drop an extension point);
  `name` is lower-case words joined by single underscores (a double underscore separates the
  segments of an addon's setting environment variable, D72); `version` is a PEP 440 version and
  `core` a PEP 440 specifier set (`packaging`, now a declared dependency); every path is relative
  and cannot leave the addon directory; a bridge (`auto_install`) must depend on something.
- **`audit.allow` / `audit.presence_only` are `tool -> [argument]` mappings**, not the bare lists
  of the roadmap example: that is the shape `collect_audit_policy` takes, and a list cannot say
  which argument of which tool is stored. Deviation recorded.
- **Errors name the addon and the field and never echo a value** (a setting can be a secret).
  A directory must carry the name of the addon it holds.
- **Core's own version is `0.0.0`** (`pyproject.toml`), so a manifest written today declares
  `core: ">=0.0,<1"`; the roadmap's `>=1.0,<2` becomes meaningful when Core is versioned 1.0.


## D71 - Addon loading and resolution (A2)

- **Explicit list, with one exception that is not a scan.** `AGENT_CORE_ADDONS=a,b` (the roadmap
  writes `ADDONS`; the `AGENT_CORE_` prefix is every other setting's) is the only way an addon is
  enabled. A bridge (`auto_install: true`) activates when ALL its dependencies are active, and
  never otherwise, so it cannot be listed-by-presence. To find bridges the loader reads the
  `addon.yaml` of the OTHER directories of the addons root, only far enough to see
  `auto_install: true`; an unlisted addon that is not a bridge is never validated, imported or
  able to break startup. A bridge found this way is validated in full and a broken one FAILS
  startup (staying silently off would hide it). Rejected: requiring bridges in the list (then
  `auto_install` means nothing) and a second allow-list (two lists to keep in step).
- **Dependencies are never enabled for you.** `top` needing an unlisted `base` fails with
  "add it to AGENT_CORE_ADDONS"; enabling something is always a visible decision. The same applies
  to a bridge that is listed explicitly.
- **Order.** Dependencies first; unrelated addons keep the listed order; bridges follow in
  activation order. A cycle is reported as its path (`a -> b -> a`).
- **Core version.** Checked for every active addon, bridges included, against
  `importlib.metadata.version("agent-core")` as a PEP 440 specifier (prereleases accepted, so a dev
  build is not locked out). A bridge that would activate but does not fit fails startup.
- **Paths a manifest points at must exist.** `profiles`, `fragments`, `migrations` (directories)
  and `policy` (a file) are checked at load, after symlinks are resolved, so a path cannot leave the
  addon directory.
- **Root.** `AGENT_CORE_ADDONS_DIR`, default `<repository>/addons` (monorepo, user decision). An
  installed wheel has no such directory; set the variable there. With nothing enabled the root is
  not touched.
- **Sample addons are committed** in `addons/` (`sample_base`, `sample_extra`, and the bridge
  `sample_bridge`), the smallest worked example for the authoring guide and A8. Variants that must
  be broken (cycles, bad manifests) are built in `tmp_path` by the tests.
- **Entry point.** `load_addons_from_environment(env)` returns the active addons in activation
  order. The composition root will call it in A4; nothing is registered yet.

## D72 - Addon settings are a declarative schema (A3)

- **Choice: the schema is data in `addon.yaml`'s `settings:` block, not a pydantic class the
  manifest points at.** Pointing at a class means importing addon code while the manifest is being
  read, i.e. running an addon before it is enabled, resolved or checked, and it would make "no addon
  touches `domain/`, `application/` or `ports/`" impossible to prove from the manifest. A
  declarative block needs no import, is validated with pydantic `TypeAdapter`s built from it, and
  is what a vertical author reads. Cost: no custom validators; an addon that needs one validates in
  its own code at its hook (A7). Rejected: a JSON-Schema document (a second vocabulary for four
  types, and `jsonschema` would not give per-key messages without echoing values).
- **Grammar.** A scalar is a default (its type is inferred); a mapping without `type` is a group;
  an explicit field has `type` (`str|int|float|bool|list|object`), `default`, `minimum`/`maximum`,
  `choices`, `items`, `fields`, `description`. No default means the deployment must set it.
  Keys are lower case words joined by single underscores. Validation is strict (no `"5"` for an
  int, no `true` for a number).
- **Sources, lowest to highest:** manifest default, the deployment file
  (`AGENT_CORE_ADDONS_CONFIG`), environment `AGENT_CORE_ADDON__<ADDON>__<KEY>` (a double underscore
  separates segments, hence the naming rule). A bool in the environment is `true` or `false`; a
  list is comma separated.
- **Conservative failures.** Unknown keys, settings for an addon that is not active (a typo, or a
  bridge that did not activate), a named-but-missing deployment file and a bad default in the
  manifest all stop startup. A value is never echoed. The resolved settings are read-only
  (`MappingProxyType`, tuples) so an addon cannot change another's.


## D73 - Slice 19 advisories: the journal hook never lets a write replace a decided outcome (A1b)

- **Journal hook.** `_record_end` now handles any `Exception` from the journal write, not only
  `ToolJournalUnavailableError`. The adapter deliberately lets a failure the database answered (a
  deadlock, a cancelled statement) propagate as itself (R3-operationalerror-breadth), so such an
  error used to escape the hook and replace the `ApprovalRequired`/`CallDeferred` it was recording
  (the turn failed instead of suspending) or a tool's result. Same logging as before (ERROR token
  `tool_journal_abandon_failed` for an abandon, WARNING for a finish); `BaseException` still
  passes through untouched.
- **Settings.** A setting that declares `choices` and `minimum`/`maximum` now holds both: a
  `Literal` cannot carry bounds, so a choice outside the declared bounds is a `ManifestError` on
  `settings.<key>.choices`. Rejected: a runtime validator on top of the `Literal` (it would accept
  a manifest whose declared choices can never all be valid).

## D74 - Addon registration goes through one value and the existing seams (A4)

- **Choice: `register_addons` returns one `AddonContributions` and the composition root hands it
  to the seams it already had.** Toolsets are merged into `TOOL_PACKAGES` (a new mapping only when
  an addon contributes one, so no-addon behaviour is the very same object), audit goes through
  `collect_audit_policy(contributions=...)` so the no-redefinition and cross-map guards are not
  re-implemented for addons, policy rules are appended to the set `start_container` reconciles and
  `_refuse_topology_drift` checks, fragments and profile templates go to `load_profiles`, channels
  to `build_channel_registry`. Rejected: letting each addon mutate the registries (order-dependent,
  and nothing would prove it stayed inside what `provides` declared).
- **Addon code runs here, and only here.** A toolset is `toolsets/<name>.py` inside the addon,
  imported by file path under `agent_core_addons.<addon>.<name>` once the addon is enabled and
  resolved. The module may not declare `AUDIT_ALLOW`/`AUDIT_PRESENCE_ONLY`: audit lives in the
  manifest (one file to review), and a module attribute `collect_audit_policy` would read is
  refused rather than ignored.
- **Ownership is strict.** An addon audits only the tools of its own toolsets and may `allow` only
  its own tools (a pattern that also reaches a foreign tool is refused); `deny` and
  `needs_approval` may narrow anything, because `EFFECT_PRECEDENCE` makes them only restrictive.
  Toolset, tool, fragment, profile id, channel and rule id are global names: a collision with Core
  or another addon fails startup naming the addon (no namespacing: a profile must keep naming a
  toolset by the same word everywhere).
- **Policy needs file-managed policy.** Reconciling deletes undeclared rules, so an addon that
  contributes rules in a deployment with no policy directory is refused instead of silently
  disarming the rules kept in the database. Rejected: creating the directory implicitly.
- **Fragments are the existing `include:` names**, looked up in the profile's fragments directory
  first and then in the addons' (collisions are refused, so the order decides nothing). Inserting
  at named slots of another profile is A5.
- **Channels are pull-mode ids only** (no code), merged with `AGENT_CORE_PULL_CHANNELS`; an id the
  operator already lists is a collision rather than a silent merge.

## D75 - Failures an addon causes are always `AddonError`s naming it (A4b)

- **Choice: nothing an addon's files can do escapes registration as a raw exception.** A
  `build_toolset()` that raises is an `AddonRegistrationError` naming the addon and the toolset
  (chained with `from`, so the traceback keeps the cause); a malformed addon profile YAML document
  is an `AddonRegistrationError` naming the addon and the file, like a profile that breaks a rule
  already was (`PROFILE_FILE_ERRORS` is exported by `profiles_fs` so `yaml` stays an import of the
  adapter). Core's own profile files keep raising what they always raised.
- **A declared directory must exist and hold something.** The loader checks the directory when the
  manifest is read; registration re-checks (it must not trust that it still holds) and refuses a
  `fragments:`/`profiles:` directory with no `*.md`/`*.yaml`, instead of registering an empty
  contribution. Rejected: warn-only (a declaration that yields nothing is a mistake to name).

## D76 - Profile inheritance and named slots resolve on mappings, before the domain (A5)

- **Choice: `adapters/driven/profiles_fs/inheritance.py` merges profile documents as mappings and
  `AgentProfile.from_mapping` validates the result.** The domain learns nothing new: a resolved
  profile is validated exactly like a hand-written one, versions and hashes are assigned from the
  RESOLVED content (editing a parent moves the child's version), and `AgentProfile` still has no
  `extra` escape. `load_profiles` reads every document first (Core's files, then the addons'), so a
  profile may inherit one that loads after it, and a Core profile may inherit an addon template.
- **Merge rules (`inherit: <id>` plus a patch).** Scalar: the child's replaces. List: parent's
  items then the child's not already present (so a child can add a toolset, never silently drop
  one). Mapping: merged key by key, recursively (`peers: {max_hops: 3}` keeps the parent's
  allowlist). `replace: [field]` swaps named top-level fields wholesale; it may name only a field
  the patch carries and never `id` or `approval_rules` (a child may add approval requirements, it
  may not drop its parent's). `persona` is a scalar: replacing it drops the parent's `include:`
  text; the child's own `include:` is appended to whichever persona results. Not inherited: `id`,
  `public_alias` (names one profile) and `peers.topology` (declared once on the hub, C17; copying it
  would be a topology drift). Rejected: a free-form patch language (JSON-patch/jq) - the four rules
  are what reviewers can hold in their heads - and positional list edits.
- **Bounds.** At most `MAX_INHERIT_DEPTH` (4) parents; a cycle, an unknown parent, an `inherit`
  that is not a string and a `replace` without `inherit` fail at load naming the profile (and the
  addon, wrapped as `AddonRegistrationError`, when an addon's file caused it).
- **Named slots (the xpath analogue).** A profile declares `slots: {name: [fragment, ...]}` and puts
  a line `{{slot:name}}` where the slot goes in its persona. An addon declares
  `provides.inserts: [{profile, slot, fragment}]`; the fragment must be one of the addon's OWN
  (`provides.fragments`), so an insert adds the addon's text, never another's. Inserts append
  after the slot's default fragments in ACTIVATION order (dependency order, then the list), which
  makes the result deterministic. A child inherits the parent's slots including what was inserted,
  and may add fragments. Unknown profile, unknown slot, a declared slot with no marker, a marker
  with no slot, and a slot marked twice are errors. An empty slot leaves no blank hole.
- **No new authority.** Inheriting and inserting change persona text and the profile's own fields,
  nothing policy reads: tools still need a policy `allow`, `deny` still wins, and the topology and
  answer-schema checks run on the resolved profiles. With no `inherit`, `slots` or `insert` the
  documents go through the same `resolve_includes` `parse_profile` uses, so the result is the one
  it always was (a literal `{{slot:x}}` in a profile that uses none of this is left alone).
- Console `:reload` and `preflight` call `load_profiles`, so they resolve inheritance and slots too.
  Open, reversible: inserts may only name the addon's own fragments; lifting that would let a
  bridge insert a dependency's text.

## D77 - Slice 21 advisories: slots never lose content silently, the machinery is local (A5b)

- **A child persona must carry the markers of inherited slots that hold content.** The child's
  `persona` replaces the parent's (D76), but the parent's slots, their defaults and every addon
  insert stay. If the new persona has no `{{slot:x}}` line for a slot that holds content, startup
  stops with an error naming the slot, the profile it came from and who put content there (the
  profile's defaults, `addon 'x'`). Chosen over "append the lost content at the end" because the
  position of an insert is part of what the author of the persona decides; placing it elsewhere
  would be a second, silent rule. An inherited slot nothing was put into may be omitted (nothing is
  lost; a later insert would then hit the error above). A slot a document declares itself, and every
  slot of a profile with no parent, still needs its marker.
- **An empty slot removes its own marker line only** (plus one adjoining blank line, so the gap
  closes). Before, every run of three newlines in the whole persona collapsed.
- **Includes errors in an inheriting addon profile name the addon and the file** (a malformed
  `include:` raised an unwrapped error, so startup failed without saying which addon).
- **The slot machinery is decided per profile, not per deployment.** It applies to a profile that
  has `inherit`, `slots:` or an insert aimed at it. Any other profile is resolved by
  `resolve_includes` alone, so a literal `{{slot:x}}` line in it is text whether or not some other
  profile uses slots. Before, the same file was left alone in one deployment and rejected in
  another. Rejected: validating markers in every profile (it would break a profile that merely
  quotes the syntax, and the "nothing used, nothing changes" guarantee of D76). Consequence: a
  profile that uses the machinery cannot hold a literal marker line; no escape syntax is added
  (reversible: add one if an author needs it).

## D78 - Tool wrappers wrap in the toolset, below every hook (A6)

- **Choice: `provides.wrappers: [{tool, wrapper}]`; `wrappers/<wrapper>.py` defines
  `wrap(original, arguments, ctx)`.** `await original()` is the `super()` analogue: it runs the
  tool, or the wrapper loaded before this one. A wrapper may transform the result, run code
  around the call or not call the original. The wrapper function may be sync or async.
- **Mechanism: a `WrappedToolset` (`adapters/driven/tools/wrapping.py`) around the toolset that
  holds the tool, applied by `LocalToolProvider`.** It sits below every runner hook, so the tool
  the model is offered is the original's - name, schema, description, metadata (hence the replay
  class) - and policy (`before_tool_execute`, on the tool name), audit and the tool journal run
  exactly as before: a denied or approval-pending call never reaches a wrapper. Rejected:
  replacing the tool's function (loses the context/positional-argument handling of the framework
  and needs a second schema) and registering a second tool under a new name (policy and audit
  would key on a name the model never calls).
- **`original()` takes no arguments; `arguments` is a read-only copy.** The policy decided on, and
  the audit recorded, the model's arguments; a wrapper that could change them could turn an
  approved call into a different one. Reversible: allow an explicit, schema-validated override
  if an addon needs it (it would have to re-run the policy).
- **Scope.** A wrapper may wrap a tool of Core or of an addon in its dependency closure
  (`depends`, transitively); not its own tool (edit it), not an unrelated addon's, not an unknown
  one: `AddonRegistrationError` naming the addon and `provides.wrappers` at startup. MCP tools are
  not wrappable. It grants nothing: `provides.policy` `allow` rules still name only the addon's
  own tools.
- **Chaining.** Wrappers of one tool nest in activation order (dependency order, then the enabled
  list): the first loaded is innermost, the last outermost, like a later Odoo module's override
  calling the earlier one's.
- **Audit identity.** One audit row per call, as before. The runner adds the wrapper addon names
  (innermost first) to the audited arguments under `_wrapped_by`; the sink keeps that key whatever
  the tool's allowlist says, only when every entry has the shape of an addon name
  (`domain/tool_wrapping.py`). No new column or port signature.
- **Failure.** An exception raised by wrapper code becomes a `ToolFailed` the model reads (addon
  and tool named, the exception's text left out and logged); control-flow exceptions (retry,
  defer, approval) pass through; what the original raises is left alone, as without a wrapper.
  Nothing crashes the turn.
- With no wrappers `wrap_toolsets` returns the toolsets it was given, so the provider is
  byte-identical to before. Sample: `addons/sample_shout` wraps `sample_echo` of `sample_base`.


## D79 - Slice 22 advisories: the runner owns the wrapper marker, markers sit on their own line (A6b)

- **`_wrapped_by` in the audit is the runner's, never the model's.** A tool that accepts extra
  arguments let a model send `_wrapped_by: ["some_addon"]`, which reached the sink and was kept
  (the key survives the allowlist by design). The runner now drops any `_wrapped_by` from the
  model's arguments before building the audited ones and adds its own only for a wrapped tool.
  Rejected: stripping in the sink (it cannot tell the runner's key from the model's).
- **A slot marker must be alone on its line.** `{{slot:x}}` inside a sentence, beside other text or
  twice on a line is an error naming the slot and the profile (before: a declared slot reported
  "no marker", an undeclared one passed silently as text). A slot holds paragraphs, so there is no
  honest mid-sentence placement. Inside the machinery only; a profile outside it keeps the literal
  text (D77). Reversible: inline replacement could be added with an explicit rule.
- **The original tool's error stays itself through a wrapper chain.** Any exception the original
  raised, including the last failure of a wrapper that calls `original()` again (a retry), reaches
  the caller as that exception; before, only the first failure was recognised and a later one was
  reported as a wrapper failure. Only errors the wrapper code itself raises become `ToolFailed`.
- **A wrapper reaches a tool behind any toolset kind.** `wrap_toolsets` used to wrap only
  `FunctionToolset`s and silently left a combined, prefixed or other toolset unwrapped. Every other
  toolset now goes behind a `WrappedToolset`, which wraps the tools it turns out to have, so a
  declared wrapper is never silently skipped. Registration still requires the addon's own toolsets
  to be function toolsets and refuses wrapping an unknown tool, naming the addon (D78); MCP tools
  keep their `mcp_<server>_` names, which no manifest can name.

## D80 - Install state: migration files and a hook per addon, recorded under an advisory lock (A7)

- **Tables (migration `0044_addons`).** `addons (name PK, version, installed_at, upgraded_at)` is
  the `ir.module.module` analogue; `addon_migrations (addon, file, version, applied_at; PK
  (addon, file))` records each addon migration file once, with the addon version it ran under.
  Additive, never read by Core's turn path. With no addon enabled both stay empty and nothing
  else changes.
- **Versioned migrations are a flat, ordered directory.** `migrations: migrations/` holds
  `*.sql` files that run in file-name order, each in its own transaction with its record; recorded
  per addon and FILE, not per version directory as Odoo's `migrations/<version>/`. A file added
  without a version bump still runs. Rejected: version-named subdirectories (two ways to order
  the same files, and a bump would be required to ship a fix). Open, reversible: record a checksum
  and refuse an edited, already-applied file (today it is documented, not enforced).
- **`post_init` is the install and the upgrade hook.** `hooks.post_init: module:function` is
  `function(context)` with `previous_version` (`None` on first install). It runs when the recorded
  version differs from the manifest's, in the same transaction as the version record and through
  the same connection, so its database writes and the version commit together. Its Python side
  effects cannot be rolled back: a hook must be safe to repeat, because a failure re-runs it.
  Rejected: separate `post_init` and `post_upgrade` hooks (one callable with the previous version
  covers both with less surface).
- **Failure leaves the failed step undone and retryable.** A failing file rolls back alone and the
  files before it stay applied; startup stops with an `AddonInstallError` naming addon and file.
  A failing hook leaves the version unrecorded. Chosen over all-or-nothing per addon because DDL
  that already committed cannot be un-run by a later failure, and pretending otherwise would hide
  it.
- **Downgrade refuses startup, for the whole pass, before anything runs.** A recorded version newer
  than the manifest's means old code over a newer schema. Hooks that cannot be found or imported
  also fail before any database work.
- **One process at a time.** The pass holds `pg_advisory_lock` on its connection and reads the
  recorded state only after taking it, so two processes starting together each find the other's
  work done. Core's own migrations take no lock (idempotent SQL); addon files need not be
  idempotent, hence the lock. Held only by the install pass, released on close.
- **No uninstall.** Removing an addon from `AGENT_CORE_ADDONS` leaves its rows and tables (a
  returning addon continues). `python -m agent_core addons list` is the read-only view (current,
  installs, upgrades, refuses, kept). Uninstalling is a documented manual step: a destructive path
  nobody asked for is not worth shipping.
- `PINNED_DBOS_APPLICATION_VERSION` is unchanged: no workflow shape moved.

## D81 - Slice 23 advisories: no un-run hook is recorded, the received argument is audited, database failures are named (A7b)

- **An async `post_init` is refused when the addon is loaded.** A coroutine function called
  without being awaited "succeeds", runs nothing and the version was then recorded as installed.
  The hook is synchronous by contract (D80), so `_import_hook` rejects a coroutine function (also
  one wrapped in `functools.partial`) with an `AddonInstallError` that says what to write. Chosen
  over awaiting it: awaiting would need an event loop inside the install thread and a second
  transaction story for no addon that needs it. Reversible if an addon ever needs async I/O.
- **The audit shows what the tool received.** The runner owns `_wrapped_by` (D79) and used to drop
  whatever the call carried under that name, so a tool that really received such an argument had
  it missing from the trail. The runner now audits it as `_wrapped_by_received`
  (`WRAPPED_BY_RECEIVED_AUDIT_KEY`), and the sink applies the tool's own audit decision for
  `_wrapped_by` to it (by value, presence only, or dropped). Both reserved keys stay the runner's:
  a model-sent `_wrapped_by_received` is dropped. Rejected: refusing the call (changes tool
  behavior), and a new audit port parameter (touches `ports/`).
- **Install-time database failures are `AddonInstallError`s naming the step.** An
  `OperationalError` while connecting, locking, reading the recorded versions or running one
  addon's steps used to escape as the driver's exception. It is wrapped (field `database`, the
  addon when one was running); Core's own migrations keep `_refused_to_start`.
- **Versions are compared by number everywhere.** The downgrade check already used
  `packaging.version`; the "unchanged" test compared strings, so `1.0` against `1.0.0` re-ran the
  hook. Both now use `Version`; an invalid recorded version stops startup; an invalid manifest
  version already failed at manifest load.

## D82 - The Phase 2 contract: what an addon is, what proves it (A8)

- **The exit criterion is met by tests, not by prose.** `tests/contract/`:
  `test_addon_phase2_contract.py` (a bridge activates only when ALL its dependencies are enabled,
  the samples together exercise every extension point and reach every seam of the composition
  root, an addon cannot allow, audit, wrap, insert or name beyond what it owns, an undeclared
  file is never imported), `test_addon_phase2_install.py` (a sample installs against Postgres:
  migration and `post_init` recorded, bridge recorded, a restart changes nothing, a version bump
  runs only the new file and the hook again) and `test_addon_boundary.py`.
- **"No addon touches `domain/`, `application/`, `ports/`" is a static AST scan of every `.py`
  under `addons/`**, plus the reverse check that those layers never import the addon adapter.
  Forbidden: importing the three packages or anything under them, and `__import__`, `importlib`,
  `import_module`, `exec`, `eval`, `sys.modules`, `builtins`. The ALLOWED SURFACE is exactly
  `agent_core.domain.tool_replay` (pure helpers to mark a tool replay-safe); adding to it is a
  reviewed change to `tests/contract/addon_boundary.py` and `docs/ADDONS.md`. This is a guard
  against careless addons, not a sandbox: addon code runs in the Core process, so enabling an
  addon is a deploy-level decision and addons are reviewed like code. Rejected: runtime import
  hooks or subinterpreters (a large new mechanism that would still not make arbitrary Python safe).
- **The sample addons are the fixture and the documentation.** `sample_install` joins
  `sample_base` (toolsets, audit, policy, fragments, profiles, inheritance, slots, settings),
  `sample_extra` (channels, a list setting), `sample_shout` (wrapper) and `sample_bridge`
  (auto_install, inserts) so the set carries migrations and `post_init` too; a contract test fails
  when an extension point is no longer exercised.
- **The addon contract in one place:** [ADDONS.md](ADDONS.md). Open, reversible defaults of the
  phase (D74-D82): an addon policy without a policy directory is refused; an operator channel id
  clashing with an addon's is a collision; wrapper arguments are read-only, own tools cannot be
  wrapped and the last loaded wrapper is outermost; the `{{slot:x}}` marker has no escape;
  migrations are a flat ordered directory, not checksummed, a failing file rolls back alone;
  one synchronous `post_init`; no uninstall; install and removal need a restart.

## D83 - The HTTP tool bridge: a library addon over a Core adapter package (Phase 3, G1)

- **Shape.** Addon `http_tool_bridge` (settings schema only, no `provides`) plus the library
  `agent_core.adapters.driven.tools.http_bridge` (`manifest`, `client`, `builder`, `settings`).
  Consumers (`glazed` tools, later others) ship a `tools.yaml` and call
  `build_bridge_toolset(manifest_path, settings=None, *, transport=None) -> FunctionToolset`
  from their toolset module. Why the library is not inside the addon directory: toolset modules are
  loaded by path, so another addon cannot import them, and `importlib`/`sys.modules` are forbidden by
  the boundary scan (D82). `agent_core.adapters.*` is the allowed surface; nothing here touches
  `domain/`, `application/`, `ports/` beyond `domain.tool_replay`. Rejected: a bridge helper module
  copied into every consumer; widening the allowed surface to other addons.
- **Settings reach the builder from the environment.** `build_toolset()` takes no arguments and the
  loader hands settings only to `post_init`, so `settings_from_environment()` re-reads the same
  sources (addon defaults, `AGENT_CORE_ADDONS_CONFIG`, `AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__*`) and
  fails at build time, naming the addon, when it is not enabled. A test keeps `BridgeSettings` and
  `addon.yaml` in step. SUPERSEDED by D84: the loader passes the consumer's settings.
- **Manifest** (see ADDONS.md): per tool name, description, method, route template, `kind`
  read/write, typed arguments with `in` path/query/body, `enum`, clamped `minimum`/`maximum`,
  `context` headers (session, agent, tenant, subject; header NAMES are settings, an empty name turns
  one off), write `idempotency` parts, `response` (`pick`, `rows_key`, `row_limit`, `wrap_as`),
  `warning`, `replay`. Unknown keys are errors.
- **Behavior carried over from the first vertical's client:** errors as values (timeout, network, any
  exception, HTTP >= 400 with a 300-char body excerpt and the status, non-JSON), `None` query values
  dropped, the idempotency key `sha256(session \x1f parts...)[:32]`, peer session ids resolved to the
  asking session (now the `peer_session_prefix` / `_separator` settings, off by default), warning
  header surfaced. Messages say "The <service_name> ..." instead of a product name.
- **Deliberate differences (reversible):** reads are replay-`safe` and writes `interrupted` by
  default (the old tools were all `interrupted`); a junk integer falls back to its default, an
  out-of-range one is clamped, an enum miss or a missing required argument is an error value and
  makes no call; the tool schema is built from the manifest, so the docstring prose of the old
  functions moves into `description` (G2 must keep the text identical).
- **One manifest model, many sources.** `BridgeManifest` is a plain typed (pydantic) model;
  `parse_bridge_manifest(mapping, source=...)` validates and resolves defaults for ANY producer and
  `load_bridge_manifest(path)` is just YAML parsing in front of it, so a later OpenAPI reader (G1b)
  feeds the same model and builder. Default header names are neutral (`X-Agent-Session`,
  `X-Agent-Id`, `X-Agent-Tenant`; the first vertical sets its own via settings). An
  `application/problem+json` error body is kept as a structured `problem` in the error value.

## D84 - Slice 26 advisories closed; the loader hands settings to `build_toolset(settings)` (Phase 3, G1a)

- **Loader.** A toolset module may declare `build_toolset()` (as before) or `build_toolset(settings)`;
  registration detects it by signature and binds the OWNING addon's loader-validated settings
  (`ResolvedAddon.settings`, a read-only mapping) so `ToolsetBuilder` stays zero-argument for every
  caller. More than one required parameter is refused naming the addon. Rejected: always passing
  settings (breaks every shipped zero-argument builder).
- **Whose settings.** The consumer's own, not the bridge addon's. A consumer declares in its own
  `addon.yaml` the subset of bridge keys it wants operators to edit (everything else keeps the
  `BridgeSettings` default) and `BridgeSettings.from_consumer` takes the keys it knows and ignores
  the consumer's other keys. Why: each vertical is configured under its own name, two verticals can
  have two backends (a shared bridge setting could not), and the operator edits one addon per
  vertical. Consequence: `http_tool_bridge` loses its `settings:` and becomes a marker (version
  1.1.0); `AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__*` is refused by the existing loader rule (not a
  setting of the addon) instead of being silently ignored; `settings_from_environment` is gone and the
  builder reads no environment. Cost: a consumer repeats the key names it exposes. Open (reversible):
  drop the marker addon and let `depends` name nothing.
- **Builder signature.** `build_bridge_toolset(settings, *, manifest, transport=None)`, with `settings`
  a `BridgeSettings` or the consumer's mapping (D85 adds `openapi`/`overlay`).
- **Advisories.** (1) A path argument is one percent-encoded segment (`/`, `%2e`, `\`, `?`, `#` are
  encoded, so `%2e%2e` reaches the backend as the literal `%252e%252e`); an empty value, `.` or `..`
  survive encoding and are refused as an error value with no call. (2) `pick` fails closed: only the
  picked top-level keys of an object or row reach the model, non-object rows are dropped, and a reply
  that is not the shape the manifest declares (`rows_key` missing or not a list, a scalar with `pick`)
  is `{"error": "... expected shape ..."}` instead of the raw body; a `rows_key` envelope keeps its
  other keys (`total`, cursors) by design. A tool with neither `pick` nor `rows_key` is unchanged.
  (3) `peer_session_prefix` with an empty `peer_session_separator` is refused when settings load;
  `case_id_for` also tolerates it. (4) A fractional bound of an integer argument rounds inward
  (`minimum` up, `maximum` down) both when clamping and in the schema; an integer range with no
  integer in it is a manifest error. A `number` keeps fractional bounds. (5) Tests cover number,
  boolean, array and enum arguments.

## D85 - The bridge reads the backend's OpenAPI document (Phase 3, G1b)

- **Why.** The backend already describes its internal API; a hand-kept `tools.yaml` drifts from it.
  Contract A (agent -> vertical internal API) is therefore OpenAPI-driven with the manual manifest as
  fallback and overlay. MCP for third parties and gRPC stay out (D83 analysis).
- **Shape.** `http_bridge/openapi.py`: `load_openapi_manifest(source, *, overlay, expect, timeout,
  transport) -> BridgeManifest`. It builds the same mapping the YAML loader builds and enters
  `parse_bridge_manifest`, so defaults, validation, builder, client and replay are untouched. Only
  `x-agent-tool: true` operations are tools. Source: a dict, a `.json`/`.yaml` path, or an http(s) URL
  fetched once at build time with `timeout_seconds` (no redirects followed, other schemes refused).
- **Consumer API.** `build_bridge_toolset(settings, *, manifest=None, openapi=None, overlay=None,
  expect=(), transport=None)`. Exactly one of `manifest`/`openapi`; `overlay` needs `openapi`;
  `expect` also works for a manifest. Rejected: a positional `manifest_path` first (it cannot be
  optional before required `settings`), and a separate `build_openapi_toolset` (two entry points to
  keep in step).
- **Mapping.** Marks listed in ADDONS.md. `x-agent-warning` and `x-agent-idempotency` are additions
  to the requested set (the manifest needs both; a write defaults to NO idempotency key rather than a
  key over all arguments, which would dedupe two legitimate identical writes in one session).
  Header parameters are skipped, never required of the model; everything the mapping cannot express
  faithfully is an error rather than a wider tool (remote `$ref`, cookies, several-alternative
  `oneOf`/`anyOf`, exclusive bounds, non-flat or non-JSON bodies, no type).
- **Contract check.** Startup failures (`OpenApiContractError`, a `BridgeManifestError`) name the
  operation as `METHOD path (operationId)`: unmappable operation, duplicate tool names (both
  operations named), no marked operation, an `expect=` tool the document does not mark, an overlay
  patch matching no marked operation (the marked tools are listed), an overlay `operation:` pin that
  no longer matches. Detecting an *incompatible change* of an operation relies on the pin and on
  `expect`; the bridge keeps no baseline of earlier documents.
- **Overlay.** Same-name entry patches (arguments/response merged by key, other fields replaced);
  no-twin entry must be complete and takes the overlay `api_prefix`; the generated routes are used as
  written (the document's paths already carry `/internal/v1`).
- **Default warning header** is now `X-Agent-Warning` (was `X-Warning`) to match the documented
  convention; a vertical with another name sets `warning_header`.


## D86 - Slice 27 advisories closed: one OpenAPI fetch, honest errors, a migration hint (Phase 3, G1c)

- **Fetch once.** `load_openapi_manifest` keeps a per-process cache keyed by URL (`_FETCHED`,
  `clear_openapi_cache()` for tests): a consumer with eight toolsets asks the backend once, not eight
  times. Only a good document is stored; a failure is raised every time and now says the backend must
  answer at startup. Each build deep-copies the cached document, so an overlay never leaks between
  builds. Rejected: a per-instance cache (the eight toolset modules are separate objects) and a TTL
  (the contract is checked at startup, a long-lived process keeps the contract it started with).
- **Async transports.** The builder used to drop a transport that was not a sync `httpx.BaseTransport`
  when it fetched the document. It now passes any transport; an async-only one runs the fetch on its
  own loop (on a helper thread when the build happens inside a running loop). `httpx.MockTransport`
  is both and keeps the sync path.
- **Malformed input is a contract error.** `parameters` that are not a list of objects with a text
  `name` and `in`, a request body whose `content` is not an object, and an overlay of the wrong shape
  (`tools` not a list, an entry without a text `name`, `arguments`/`response`/`defaults` not objects,
  `operation`/`api_prefix` not text) raise `OpenApiContractError` naming the operation or overlay
  tool instead of an `AttributeError`/`TypeError`.
- **Settings migration.** A key set on a library addon with no settings of its own
  (`AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__*`, or its section of the addons file) is still refused, but
  the message names the addons that depend on it and the variable to use. Rejected: honouring the old
  names as an alias (two sources for one value; D84 chose one).

## D87 - Glazed tools through the bridge: overlay hide/fix/pin, shared helpers, one repeated tool set (Phase 3, G2)

- **Overlay can hide, fix and pin (bridge library).** Glazed's internal API has query parameters the
  model must never choose (`store_id`, `as_of`, `past_days`), constants the old tools sent
  (`limit=10`) and one operation behind two tools (`/issues` for `get_issues` and
  `get_integrity_issues`). Added: `arguments: {x: null}` in a patch removes an argument (an unknown
  name is an error); `fixed: value` on an argument sends it on every call, hides it from the schema and
  ignores whatever the model sends; a manual tool may be `operation:`-pinned to a document operation.
  Rejected: marks in the backend (`x-agent-hide`), because what the model may see is the addon's
  decision, not the backend's, and the backend would need a deploy to change it.
- **Addon files may import their siblings; one addon may repeat its own tool (loader).** The eight
  glazed role toolsets share most tools and one body of shaping code. The registration refused a tool
  name already provided by anything, including the same addon, and a toolset file could import nothing
  of its own addon (files are loaded by path). Now the loader creates the synthetic packages
  `agent_core_addons[.<addon>[.toolsets]]`, so `from ._common import x` works, and drops everything
  imported under an addon package when the same addon name is loaded from another directory (tests,
  never production). A tool name repeated inside ONE addon is allowed (audit and policy key on the
  name, so it is one tool); a name another addon or Core owns is still refused. Rejected: one
  toolset module serving eight names (the manifest ties one name to one file), and putting the glazed
  code into Core (the point of the addon is that it leaves Core).
- **The addon `glazed_tools` (consumer of the bridge).** Thirty tools in eight per-role toolsets
  (`glazed_orchestrator` ... `glazed_liaison`), the tool sets of the hand-written ones. The backend marks
  its `/internal/v1` operations (glazed-copilot `adapters/driving/agent_tools.py`: name, effect, the
  description the model reads, idempotency, warning, row limit); `overlay.yaml` pins each operation
  and patches what OpenAPI cannot say; `toolsets/_glazed.py` holds only the outcomes that are logic, not
  data: the two forecasts (future dates only, "unavailable" never invented) and the seven manager views
  (`{available, reason}` and a key allow-list), plus two guards (an enum refused before the call,
  `discount_pct` in 0-100). The source is the backend URL (`base_url` + `/openapi.json`, or
  `openapi_url`), fetched once; tests use the committed snapshot of the backend's internal contract
  (`tests/fixtures/glazed_tools/internal_openapi.json`, exported by glazed-copilot's
  `export_internal_openapi.py`) and the captured old tool contract (`old_tool_contract.json`). The
  startup passes `expect=` for all thirty tools and every overlay entry carries an `operation:` pin,
  so a backend that renames, moves or unmarks one stops Core before a conversation does.
  Rejected: all tools generated with no code (the forecast and view outcomes are domain rules the
  bridge must not learn), and marks that carry the allow-list (`x-agent-fields` cannot express the
  nested picks or the `available` envelope, and a second place to edit the same keys would drift).
- **Replay.** The old tools were all `interrupted`. Now: every plain GET read is `safe` (the backend
  answers the same, the read adds only a trace row), every POST keeps `interrupted`, including the two
  that read (`audit_promo`, `classify_text`: `x-agent-effect: read` for what they do, `x-agent-replay:
  interrupted` as before, since `classify_text` costs a model call). Open: if a replayed read's extra
  trace row is a problem, mark those operations `x-agent-replay: interrupted` in the backend.
- **Assertions that changed in the port (all in `test_glazed_tools_addon.py`, each with a `CHANGED:`
  note).** `default_backend_url` (now the manifest setting), `a_body_that_cannot_be_serialised` (a
  `string` argument is stringified by the bridge, so an `object()` is sent as its text), the
  composition-root test (addon contributions, not `TOOL_PACKAGES`), `case_id_for` (the bridge's generic
  function with the glazed prefix), the docstring tests (the description the model reads), the
  signature test (the JSON schema), placeholder tools (absent from every toolset) and the forecast
  dates (an ISO string). Not ported: `test_second_ask_peer...`, a test of the vertical branch's
  `peers._guarded_ask_peer` patch that this Core replaced with fan-out. Behavior differences that no
  old test pinned: a junk integer falls back to the argument's default (the old `_bounded` fell back to
  its maximum, 20); an empty-string `kind`/`decision_id` is sent, not dropped; a path argument of `""`
  / `.` / `..` is an error value (the old tools would have built the URL); a missing required argument
  is an error value (the old tools relied on the framework's validation); the schema now carries
  `enum`, `minimum`, `maximum` (the old one had types only).

## D88 - Slice 28 advisories closed; glazed audit and policy live in `glazed_tools` (Phase 3, G2b + G3)

- **Advisories (G2b).** The OpenAPI fetch cache is keyed by `(url, transport)`: a document fetched
  through one transport (a test double, a second backend's own route) is never served to another; a
  production build passes no transport, so one process still asks the backend once. A tool repeated
  across toolsets of ONE addon must be the same definition (description, argument schema, replay
  class); a divergent twin stops startup naming the addon, because policy and audit key on the name
  alone. A `fixed` value on a `path` argument is refused at manifest load when it is empty, `.` or
  `..` (it would leave the route; the model never sends it, so only the manifest could). In
  `_glazed.py`: a forecast called without its `sku` and a view called without a required argument now
  answer `{"error": "<name> is required"}` before any request (the bridge's wording; its argument
  refusal carries no HTTP status, which the forecast and view outcomes read as "the backend is down"
  and answered "unavailable"); a delivery calendar whose `days` is not a list is "unavailable: unexpected
  reply" instead of a `TypeError` into the turn. Open, left as the old tool had it: a 404/503 from the
  backend is still "unavailable" even when its problem body names a reason (the old tool did the same;
  surfacing the detail would change what the manager is told). The auditor tool is one more than the
  backend marks (29 marked operations, 30 tools): `get_integrity_issues` is the overlay's second tool
  over `GET /internal/v1/issues`, pinned to it, and a test says so. The generated argument schemas are
  compared with the old-tool oracle field by field, with the differences (bounds, enums, no
  `default: null`, no `format: date`) listed in the test. In glazed-copilot the snapshot is now the
  OpenAPI document of a fresh app mounting only the internal router (no public route, schema, title or
  version can move it; it also gained two body schemas the old walk dropped), `--check` fails clearly
  on a missing snapshot, and the description assertion checks a trimmed sentence of real length with no
  route text and no duplicate across tools.
- **Audit and policy go with the tools: `glazed_tools`, not a second addon.** An addon audits only its
  own tools and `allow`s only its own, so the audit entries and the allow rows must sit in the addon
  that owns the thirty tools. A separate `glazed_vertical` addon could hold the profiles and the channel
  (G5) but not these. The rules name identities (`glazed-manager`, `glazed-service`, `peer`) and the
  channel `glazed`, which G5's addon provides; the coupling is by name, as it was in `rules.yaml`.
  Rejected: splitting the deny rows into a second addon (they share one file, one header and the same
  reading of "absent constraint widens").
- **Audit.** `addon.yaml` carries the thirty entries of the vertical branch's allowlist and presence map
  unchanged (`propose_action`: ids by value, `rationale` by presence; `classify_text`: `kind` by value,
  `text` by presence; tools with no model argument decided with an empty list). A test compares the map
  entry by entry with a verbatim copy and another derives the decided arguments from each generated
  tool's schema.
- **Policy.** `policy/rules.yaml` carries 33 of the 34 glazed rules, same ids, patterns, identities and
  reasons (the audit trail joins on `rule_id`). `glazed-orchestrator-asks-specialists` allows
  `ask_peer`, which is Core's tool, so an addon may not grant it (D74); it stays in the deployment's own
  policy directory (text in docs/ADDONS.md "The glazed policy"; the glazed infra switch, G7, must carry
  it). Without it the orchestrator's `ask_peer` is refused. The brief counted 44 rules; the vertical
  branch's file has 42, of which 34 are glazed's and 8 are the delivery/peer rules Core already has.
  Proof: the oracle file (`old_glazed_policy_rules.yaml`, verbatim) and the addon's rules plus the
  deployment rule are reduced by the real `PgToolPolicy.decide` for every glazed tool, six foreign tools
  and nine identities (effect, rule id and reason equal), plus a hand-written table of rule id -> outcome.
  An addon with policy still needs a policy directory (D74); a test pins the refusal where there is none.

## D89 - An insert may aim at every profile with a slot; fragments read their addon's settings (Phase 3, G4)

- **Why.** `plain_language` is generic (no vertical named in it) yet must put its text into the
  orchestrator of whatever vertical enables it, with the audience, language and spoken style that vertical
  chose. Two things were missing from the Phase 2 machinery: an insert had to name one profile id, and a
  fragment was a static file.
- **`provides.inserts[].profile: "*"`.** Every profile that DECLARES the slot receives the fragment
  (a child that merely inherits the slot gets it through its parent once; a child that re-declares the
  slot does not get a second copy). Opt-in is the slot: a profile that does not declare it is untouched.
  A slot no profile declares is an error naming the addon (fail loud, no silent no-op). Rejected: a
  `profiles:` setting listing the targets (every vertical would repeat its profile ids in settings) and
  inserting into every profile (text would reach agents that never asked).
- **Fragment templates.** `{{setting:name}}` and a `{{if:name}}` ... `{{endif}}` block, rendered when the
  addon registers (so a template the settings cannot fill stops startup) with the addon's validated
  settings. `AddonFragment` is a `Path`-shaped object (`is_file`, `read_text`, `name`, `stem`), so
  `include:` and the slot inserts read it unchanged; Core's own fragments are plain files, never
  rendered. Rejected: Jinja or any expression language (a persona is reviewed text; two forms are what a
  reviewer can hold), and rendering at read time with the environment (the settings are already
  validated at startup). Consequence: a literal `{{setting:...}}` or `{{if:...}}` in an addon fragment
  cannot be written; no escape is added (reversible: add one if an author needs it).
- **The addon `plain_language`.** One fragment, no code, inserted at the slot `plain_language` of every
  profile that declares it. It is the "How to talk to the manager" section of the glazed orchestrator,
  generalized: plain vocabulary in every channel, depth kept in text, adaptive depth when the reader asks,
  and (setting `voice_style`) a short spoken style keyed on a tag that starts the message
  (`voice_marker`, default `[voice]`; the glazed addon will set `[canal: voz]`). Settings: `audience`,
  `language` (empty = no language rule), `avoided_terms` (the jargon list; glazed's Spanish list is G5's),
  `voice_style`, `voice_marker`. Not carried over because they are glazed's: the Spanish jargon list, the
  "rounded dollars" example, the Spanish examples and the "manager" wording. The voice cue is a tag in the
  message, as glazed's was, not the runner's `modality` hint: a modality hint is selected by a profile's
  own `modality_instructions`, which an addon cannot add to another's profile. Open: a profile that
  already has `modality_instructions.voice` can use those instead and leave `voice_style` off.
- **Ported tests.** `test_glazed_prompts.py` has 33 tests; one concerns this text
  (`test_orchestrator_has_the_manager_communication_section`) and is ported in generic form to
  `test_plain_language_addon.py`. The other 32 are about the eight glazed personas (and the tool
  docstrings) and stay for G5 (docstrings are already covered by the contract tests of G2).

## D90 - The glazed vertical as addons: inheriting profiles, a template base, a pull channel (Phase 3, G5)

- **Why.** The eight `glazed_*.yaml` profiles and `_glazed_channel` were patched into Core on
  `vertical/glazed`. Phase 3 moves them into `addons/glazed_vertical` with nothing but Phase 2 machinery.
- **A template base.** `glazed_base` carries what the eight files repeated (model, mcp/skill lists,
  approval rules, compaction, media) and the specialists' peer block switched off. Inheritance needs a
  parent that loads, and Core has no abstract profile, so the base is a real profile made harmless: no
  tools, no peers, `max_iterations: 1`, `max_cost_usd: "0.01"` (zero would mean no cap). Rejected: an
  `abstract:` flag (a Core change for one convenience; reversible if a second vertical wants it) and
  copying the shared block into every file (the old drift: one model line per file).
- **Personas stay whole.** A persona is a scalar, so each role carries its own text verbatim. The seven
  specialists are byte for byte the vertical branch's (the parity test compares the served profiles with
  the old files, kept in `tests/fixtures/glazed_vertical/`).
- **The orchestrator differs in one section, on purpose.** Its "How to talk to the manager" section is
  now the `plain_language` fragment rendered with the glazed settings, followed by the glazed-only words
  (Rule 3 in plain Spanish, rounding in voice, the examples). Byte identity there would mean not using
  `plain_language` (or templating every word of it), which defeats G4. The parity test pins everything
  else of the orchestrator (head, tail, every non-persona field) to the old file. Open for G6: the golden
  turn compares behaviour, not bytes, for this section.
- **Settings of another addon.** An addon cannot set `plain_language`'s settings; the vertical ships
  `addons_config.yaml` for `AGENT_CORE_ADDONS_CONFIG`.
- **The channel.** `provides.channels: [glazed]`, the pull-mode registration `_glazed_channel` did.
- **Not carried.** `Core/scripts/set_glazed_model.py` (rewrote eight model lines; now one line of
  `glazed_base.yaml`).
- **Ported tests.** `test_glazed_vertical.py` (all of it, assertions unchanged, plus parity tests) and
  the 32 deferred tests of `test_glazed_prompts.py` (docstring tests read the generated description).

## D91 - A refusal carries the backend's reason, and an unavailable view says it (Phase 3, G5)

- **Why.** User decision 2026-10-14, "Que el modelo vea el motivo": a 404 or 503 of a glazed view or
  forecast reached the model as a bare "not available right now", so it could not tell the manager why
  (no closing data yet, the model is retraining) and the reason was lost.
- **Generic, in the bridge.** Every refusal (`status >= 400`) whose body says why gets `reason` in its
  error value: the problem `detail` (else `title`), the `detail`/`message`/`error`/`title` text of a
  JSON body, or a `text/plain` body; whitespace collapsed to one line, at most 300 characters (the
  same bound as the message excerpt). A body with no readable text (a list of validation errors, a
  blank, HTML) gives no `reason`, never a guess. Additive: `error`, `status` and `problem` are unchanged.
- **Glazed.** The view and forecast outcomes that turn a 404/503 into `available: false` /
  `forecast_available: false` append "The backend said: <reason>." before the "never estimate"
  instruction. Without a reason the text is exactly the old one. Other refusals stay error values and
  now carry `reason` too.
- **Trust.** The text comes from the vertical's own backend, bounded, and reaches the model inside the
  tool result like every other backend field; no new channel is opened.


## D92 - Slice 29 advisories closed: checked fragment text, one fetch under a race, wildcard chains

- **registration.py (fragments).** Registration rendered each addon fragment to check it and threw the
  text away; every later read rendered the file again, so an edit or removal after startup reached a
  profile unchecked or failed at profile load naming no addon. Registration now serves the text it
  checked (`_CheckedFragment`). Consequence: editing an addon fragment needs a restart, like every
  other addon change.
- **openapi.py (fetch cache).** The cache was checked under the lock but the fetch ran outside it, so
  builds racing at startup each asked the backend. A per-key lock makes the second build wait and read
  the cached document; different URLs or transports are not serialized; a failed fetch is still not
  stored (the waiter tries again).
- **inheritance.py (`_declaring`).** No defect: a test now pins that a wildcard insert lands once down a
  chain that re-declares the slot, including when only the grandparent declares it, in either load
  order (it fails if only the direct parent were consulted).

## D93 - Golden-turn parity with vertical/glazed; slice 30 advisories closed (Phase 3, G6)

- **Golden turns (G6).** `Core/tests/support/glazed_parity.py` scripts ten glazed turns (all eight
  roles, reads, writes, 404/503 refusals, a peer turn denied, a null optional, two bad enums) with a
  `FunctionModel`, one fake backend over `httpx.MockTransport`, and a gate that does what
  `before_tool_execute` does (decide on the validated arguments, write the audit row, then refuse or
  run). `Core/tests/fixtures/glazed_parity/capture_golden.py` ran them on vertical/glazed (cfecd44, in a
  throwaway worktree under the system temp dir) and wrote `golden.json`; `test_glazed_golden_turns.py`
  runs them on the addon build. Compared: tools, tools left by the policy filter, the definitions the
  model sees, persona, validated arguments, policy outcome, every request (method, path, query,
  headers incl. `X-Glazed-*` and `Idempotency-Key`, JSON body), the result the model reads, audit rows.
  Rejected: re-implementing the old tools as fixtures (not the old code), and running the full runner
  (needs Postgres and DBOS; the gate reproduces its order with both Cores' real `PgToolPolicy` and
  `PgAuditSink`).
- **Differences allowed, each asserted on its own.** Accepted: the orchestrator's manager section is
  `plain_language`'s; a 404/503 refusal carries the backend's reason (D91). Documented in D87: the
  schema (no `null`/`None` default on optionals, clamps and enums in the schema, no `format: date`).
  Found and fixed: `classify_text` answered an unknown `kind` with the bridge's message, without the
  old final period. Found and pinned for a decision (not fixed): F1, the policy and audit arguments no
  longer contain the defaults the old Python signatures filled in when the model did not send them
  (`limit`, `category`, `kind`, `date_to`, `impact`); the request still carries them. F2, JSON body
  keys and query parameters come in another order (same content).
- **No approval in a glazed turn.** No glazed rule is `needs_approval`, in either build: a write is
  allowed and creates a pending proposal the manager decides in the backend.
- **Slice 30 advisories.** `_CheckedFragment` declares `FragmentFile` (mypy checks it against the
  protocol) and refuses a read in another encoding than the UTF-8 it was checked in; a test runs both
  consumers (include, slot insert). `clear_openapi_cache` no longer drops a fetch in flight: racing
  builds wait on a `_Flight`, and the race tests force the overlap with events, not a sleep.

## D94 - The bridge fills unsent defaults into the validated arguments; slice 31 advisories (Phase 3, G7)

- **F1 resolved (approved as default).** A Python tool's signature filled every default into the
  arguments `before_tool_execute` hands the policy and the audit row; the bridge's tools did not, so
  a tool moved onto it lost `limit`, `category: "eventos"` and the null optionals from its audit
  trail. Each bridge tool now gets an arguments validator that adds every argument the model did not
  send: its `default`, or null when it has none. A `fixed` argument is the bridge's and is never
  added; what the model sent is kept as sent. The request does not change (`_prepare` treats null as
  unsent and applies the same default), pinned by a test. It applies to EVERY bridge consumer, not
  only glazed (a test uses the vertical-neutral library manifest). The golden-turn test now compares
  validated arguments and audit rows exactly; F2 (body and query order) stays pinned.
  Rejected: filling defaults only in the glazed overlay (every consumer has the same gap), and
  filling them in Core's policy hook (the hook does not know a tool's schema).
- **Slice 31 advisories.** Every alias of UTF-8 (`utf8`, `U8`, `cp65001`) is served by a checked
  fragment, another codec is refused naming it, an unknown one raises `LookupError` as a file read
  does (tests; the code already compared the resolved codec). The race tests fail when a build thread
  is still running after its join. The parity harness records httpx's user agent without its version
  and the comparison ignores it, so an httpx upgrade does not break the golden; `golden.json` is
  pinned by its SHA-256 in the test; `capture_golden.py` checks its command line (one existing Core
  directory, else usage and exit 2). The positional pairing of audit rows with scripted calls went
  away with F1 (rows are compared as a list).

## D95 - A turn router before the model: `provides.routers`, a recorded step, off by default (Phase 5, P1)

- **What.** A profile may declare `routing: {router, targets}`. When `AGENT_CORE_TURN_ROUTING` is on
  and a person or a channel starts its turn, the named router (an addon's `provides.routers`,
  `routers/<name>.py` with `route(request[, settings])`) answers `pass`, `route(profile_id)` or
  `intent(name, arguments)` before the model runs. A route makes the turn the target profile's from
  its first step: its `started` event, its persisted request, its policy, approvals and budgets.
- **Layering.** `domain/routing.py` holds the decision, the record and `resolve_route` (pure);
  `ports/turn_router.py` asks a router; `ports/route_audit.py` records the consultation;
  `application/route_turn.py` (`RouteTurn`) owns the timeout, the fallbacks and the audit;
  `adapters/driven/routing/addon_router.py` turns an addon's plain-dict answer into a decision.
  Addons stay plain Python (no domain imports), as wrappers do.
- **A port of its own for the audit.** `AuditSink` is frozen with a regression lock and a route is
  none of its five facts, so `RouteAudit.record_route` writes `audit_turn_routes` (migration 0045)
  on the audit pool, append-only (CLAUDE.md #6). A route is applied only after its row is written;
  an audit failure runs the requested profile (`audit_unavailable`).
- **Guards.** A route lands only on a target the declaring profile lists, that is loaded and a
  channel may start (`resolve_route`); startup refuses a routing profile whose router or targets
  cannot work, when routing is on. A router slower than `AGENT_CORE_TURN_ROUTER_TIMEOUT_MS`
  (default 1500 ms), one that raises (recorded as `error:<Class>`, never the message) and a
  malformed answer fall back to the requested profile. Peer and subagent turns are never routed. A
  router is not a safety barrier: it chooses a profile, it grants nothing.
- **Intent.** Accepted by the type and recorded; until intent executors exist (P3) it runs the
  normal path with `intent_not_supported`.
- **Replay safety.** The consultation is `_step_route`, a DBOS step whose result is the routed
  request and its record, so a replayed or recovered turn reads the route back and never asks the
  router again (an integration test kills the process mid-turn and changes the router's answer).
  The step runs for EVERY person-started turn, routing on or off, so the replay shape does not
  depend on configuration; that is a new step, so `PINNED_DBOS_APPLICATION_VERSION` moves to
  `agent-core-turns.12` (drain first, OPERATIONS.md). Rejected: running the step only when routing
  is on (flipping the flag would strand every turn in flight with `DBOSUnexpectedStepError`), DBOS
  patching (needs `enable_patching`, a second versioning mechanism), and routing inside
  `StartTurn` (a crash mid-model would re-classify on the step retry).
- **Observability.** A `routed` progress event before `started` (`agent` the profile that runs,
  `detail` `routed` / `passed` / `fallback:<reason>`, `elapsed_ms` the router's time; no label on the
  feed) and the audit row with label, confidence, calibration, model, elapsed time, decision and
  fallback reason. The feed fold ignores `routed` (it changes no turn's state).
- **Reversible defaults.** Off by default; one router per profile; the timeout is per deployment,
  not per profile; the confidence threshold lives in the router (P2), not in Core.

## D96 - A router's reason and usage are audited, not only logged (Phase 5, L12)

- **What.** `RouteDecision` carries the router's `reason` (a fixed code: why it passed) and its
  `usage` (token counts by name, whole numbers of at least 0); `RouteRecord` keeps the reason
  (bounded to `MAX_AUDIT_TEXT`) and `input_tokens` / `output_tokens` (from `input_tokens` or
  `prompt_tokens`, `output_tokens` or `completion_tokens`). Migration 0046 adds the three nullable
  columns to `audit_turn_routes`. Before, P2's router reported both and Core only logged them.
- **Why the columns and not a JSON blob.** The two numbers are what an operator sums (the cost of
  routing) and the reason is what they group by (abstentions); a blob would need a query per key.

## D97 - Intent plans: a typed intent runs a declared read-only plan, the model only writes (Phase 5, P3)

- **What.** S3/S6 layer B. An addon declares plans (`provides.intents: <file>`): intent -> owning
  profile, 1-8 tool calls with arguments (constants, the router's arguments with optional
  defaults, `today` / `channel` / `tenant_id`), and a reply (`compose`: one tool-less model call
  of the owner, told to use only the results; `template`: deterministic text). A profile opts in
  with `routing.intents: true`; an intent then lands on the plan's owner by the rules of a route
  (`resolve_route`), and `RoutedTurn.intent` carries it to the workflow.
- **The extension point is its own file, not part of a router's routes.** A plan is Core's
  contract (it runs Core's tools under Core's policy); a routes file is one router's input. Any
  router (keyword, System One, a model) can answer an intent a plan covers.
- **Same pipeline, no second enforcement point.** A planned call is one `agent.run` of the owner's
  cached agent whose model is a script (`FunctionModel`, `intent_plan.py`) that emits the call and
  reads back how it ended. So the toolset (grant, wrappers), `PolicyEnforcement` (verdict, audit
  row, `tool_called` event, journal, approval deferral), `UntrustedResultWrapping` and the usage
  limits are the production ones. Rejected: calling the tool functions directly (a parallel
  policy/audit path that would drift).
- **Read-only, checked three times.** Registration refuses a tool no toolset exports, one not
  declared `replay: safe`, and Core's delegating tools (`INTERRUPTING_TOOLS`); startup refuses an
  owner that is missing, unstartable, typed, or not granted the tool; each call re-checks the
  tool's definition. A NEEDS_APPROVAL verdict or a deferring tool aborts the plan.
- **Fallback, never a failed turn.** Any problem audits the reason (`audit_turn_intents`,
  migration 0047, via `RouteAudit.record_intent`) and runs `_step_start` with the same request. The
  turn is persisted only once the reply exists, so the normal path writes the request once.
- **Replay safety, no version bump.** prepare, each call, the reply and the fallback are DBOS
  steps (`_step_intent_*`): a resumed turn reads back finished calls and the compose. A turn
  WITHOUT an intent takes exactly the steps it took (`_step_start` first), so its operation log is
  unchanged; an intent turn's shape is decided by the recorded `_step_route` result. A `.12` route
  record has no `intent` slot and is read with `getattr(..., None)`: it runs the normal path. So
  `PINNED_DBOS_APPLICATION_VERSION` stays `agent-core-turns.12`; no drain is needed for this.
- **Compose without history.** The intent was read from this message alone, and a provider that
  refuses tool parts without tools would reject an earlier turn's tool calls; the reply message is
  stored, the prompt with the results is not (`_messages_the_turn_added`).
- **Observability.** `planned` progress event (`plan` / `fallback:<reason>`), folded onto the turn
  in the feed snapshot (`plan`), so a client (the routing benchmark) can count plan vs fallback.

