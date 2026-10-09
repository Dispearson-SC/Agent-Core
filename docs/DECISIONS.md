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
