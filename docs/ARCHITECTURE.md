# Architecture

The core owns governance, not intelligence. Intelligence is rented; governance is
written.

---

## 1. What is bought and what is written

The boundary below is what keeps this core from growing into a second Hermes. Every
row marked *dependency* is code that does not get written.

| Responsibility | Solved by | Cost |
|---|---|---|
| Reasoning loop and tool dispatch | Pydantic AI | dependency |
| MCP client: stdio, HTTP, SSE, resources | Pydantic AI `MCPToolset` | dependency |
| Multimodal input: image, audio, video, document | Pydantic AI `BinaryContent` | dependency |
| Compaction **mechanics** (history rewriting) | Pydantic AI `ProcessHistory` | dependency |
| Durability, retries, long pauses, crash recovery | DBOS Transact | dependency |
| Multi-provider, failover, request normalization | LiteLLM | dependency |
| Tool policy by caller identity | — | **written** |
| Approval and evidence-request lifecycle | — | **written** |
| Append-only audit of decisions and cost | — | **written** |
| Compaction **strategy** and its trigger | — | **written** |
| Skill registry and lazy loading | — | **written** |
| Agent profiles and tool packages | — | **written** |
| Error classification to strategy | — | **written** (late phase) |

Note the compaction split: the mechanics come solved, the strategy does not. What
gets trimmed, when, and in how many steps is where money is won or lost.

---

## 2. Layers

Dependencies always point inward.

```
domain/          zero external imports
  ↑
application/     use cases; imports domain/ and ports/ only
ports/           eleven protocols; only signatures and domain types
  ↑
adapters/driving/    http/  workflow/  scheduler/
adapters/driven/     agent, model, persistence, context, skills, mcp, human, media, tools
```

### Sync or async

Async end to end, with two deliberate sync islands — full reasoning in
[DECISIONS.md](DECISIONS.md) D13.

| Layer | Nature | Why |
|---|---|---|
| `domain/` | **sync** | No I/O to await. Frozen dataclasses and pure predicates. |
| `application/`, `ports/`, `adapters/` | async | Both ends are already async: FastAPI is ASGI, Pydantic AI is async-native, DBOS supports coroutine workflows and steps. A sync middle would bridge twice instead of zero times. |
| Postgres transactions | **sync** | DBOS does not support coroutine transactions; wrap with `asyncio.to_thread`. |

Asynchrony here is forced by the human-in-the-loop requirement, not chosen for throughput:
a turn can suspend for three days, so `POST /turns` returns 202 and the turn runs behind
it. The long waits themselves cost nothing either way — DBOS makes them durable, so a
suspended turn holds no thread, no connection and no memory.

### Directory layout

All development lives under `Core/`. The repo root holds only `docs/`, `README.md`,
`CLAUDE.md` and config.

```
Core/
  pyproject.toml
  profiles/            <vertical>.yaml
  skills/              <skill-name>/SKILL.md
  tests/               conftest, fakes/, unit/, integration/
  src/agent_core/
    domain/
      turn.py            profile.py       policy.py
      budget.py          media.py         compaction.py
    application/
      start_turn.py      resume_turn.py   decide_approval.py
      compact_context.py ingest_media.py
    ports/
      agent_runner.py    tool_provider.py    tool_policy.py
      human_gateway.py   conversation_store.py
      audit_sink.py      model_gateway.py
      context_engine.py  skill_registry.py
      media_store.py     speech_port.py
    adapters/
      driving/
        http/  workflow/  scheduler/
      driven/
        agent_pydantic/    # AgentRunner + hooks
        llm_litellm/       # ModelGateway
        persistence_pg/    # ConversationStore, AuditSink, MediaStore
        context/           # ContextEngine: the compaction ladder
        skills_fs/         # SkillRegistry over SKILL.md files
        mcp/               # MCPToolset composed into ToolProvider
        human/             # HumanGateway: channel + evidence
        media_fs/          # MediaStore
        tools/
          fraud/  delivery/
    composition.py         # the ONE place adapters are chosen and wired
```

`profiles/` and `skills/` sit beside `src/`, not inside the package: they are data read at
runtime, not Python modules, and packaging them into the wheel would be wrong.

---

## 3. The ports — the contract

Each port answers exactly one question. A port that answers two is cut wrong.

### Skeleton

| Port | Question it answers | Day-1 adapter | Changes per vertical? |
|---|---|---|---|
| `AgentRunner` | How do I run a turn and how do I resume it? | Pydantic AI | no |
| `ToolProvider` | Which tools exist for this profile? | local tools + `MCPToolset` | **yes** |
| `ToolPolicy` | May this caller use this tool, and does it need approval? | rules in Postgres | **yes** (data) |
| `HumanGateway` | Who do I ask, and how do I wait for the answer? | DBOS + HTTP endpoint | no |
| `ConversationStore` | Where does the history and its checkpoints live? | Postgres | no |
| `AuditSink` | What happened, who asked, what did it cost? | Postgres append-only | no |
| `ModelGateway` | Which model and provider am I talking to? | LiteLLM (library) | no |

### Capabilities enabled per profile

| Port | Question it answers | Day-1 adapter | Changes per vertical? |
|---|---|---|---|
| `ContextEngine` | When and how do I compress the conversation? | own compressor over `ProcessHistory` | no |
| `SkillRegistry` | Which skills exist, and what is this one's body? | `SKILL.md` files on disk | no (content changes) |
| `MediaStore` | Where does this binary live and how do I retrieve it? | Postgres + disk / S3 | no |
| `SpeechPort` | How do I convert between audio and text? | phase 2 | no |
| `KnowledgeBase` | What does the business offer today? (**read only**) | Postgres, `FULL_TEXT` | no (collections are data) |
| `KnowledgeAdmin` | Change what the business offers (**admins only**) | Postgres | no |
| `AgentMailbox` | How does one agent ask another? | durable queue, A2A-shaped | no |
| `TranscriptReader` | What happened in this conversation? | Postgres projection | no |

**The contract:** fifteen ports, and still only two change when a vertical is added —
`ToolProvider` and the rows behind `ToolPolicy`. Every capability added since the first
draft (compaction, skills, MCP, media, knowledge, peers, transcripts) entered as **data and
composition**, never as a per-vertical code change. That invariant, not the port count, is
what the architecture is defending.
Skills and MCP added no per-vertical ports because they enter as *data and
composition*, not as new code.

### On abstracting Pydantic AI

`AgentRunner` exists so use cases and tests do not import Pydantic AI. It does
**not** exist so the framework can be swapped tomorrow — that portability is not
real, and chasing it produces a bloated abstraction that reimplements half a
framework.

The port therefore mirrors Pydantic AI's own contract shape (`run` returns a result
or deferred requests; `resume` takes decisions) rather than inventing a parallel
vocabulary.

---

## 4. Where DBOS lives

DBOS works through decorators. The obvious move is to decorate the use case — and
that is wrong. It puts infrastructure inside the application layer: tests then need
Postgres, and the use case is married to an orchestration engine.

**A DBOS workflow is a driving adapter**, like a FastAPI router. It orchestrates
steps; each step resolves a use case and calls it.

```python
# adapters/driving/workflow/turn_workflow.py
@DBOS.workflow()
def run_turn_workflow(request: TurnRequest) -> TurnResult:
    outcome = _step_start(request)

    while outcome.pending:                    # approval OR evidence
        _step_publish(outcome.pending)
        answer = DBOS.recv(timeout_seconds=THREE_DAYS)
        outcome = _step_resume(outcome.turn_id, answer)

    return outcome.result


@DBOS.step()
def _step_start(request):
    return container.start_turn(request)      # pure use case
```

Three concrete benefits: the use case is testable without a database; the
`while` + `recv` is a durable wait that survives restarts and deploys; and if the
process dies after `_step_start`, restarting does **not** re-run the agent turn that
already completed.

> **Hard rule.** Never put the model call inside a database transaction. A step that
> takes forty seconds with an open transaction holds connections and ends in pool
> exhaustion under load.

> **Hard rule.** Anything non-deterministic — clock, UUID, randomness — is generated
> *inside* a step, never in the workflow body. A non-deterministic workflow body
> produces a different result on replay, and the bug only surfaces during crash
> recovery.

---

## 5. Context compaction

The subsystem with the most nuance, and the one where Hermes recorded a lesson that
appears in no documentation.

Responsibility split:

- **Pydantic AI supplies the mechanics.** A history processor wrapped as a
  `ProcessHistory` capability rewrites the history, and works with any provider.
  Provider-native compactors and a tiered orchestrator also exist.
- **`ContextEngine` supplies the strategy.** What is trimmed, in what order, with
  what trigger, in how many steps.

### The ladder — cheapest first

Climb a rung only if the previous one was not enough.

| Rung | What it does | Model cost |
|---|---|---|
| **L1** | Prune old tool outputs into a stub referencing the original on disk | none |
| **L2** | Sliding window with a protected head (system prompt + first exchanges) | none |
| **L3** | Summarize the middle with a cheap model; head and tail stay intact | one cheap call |
| **L4** | **Iterative re-summary**: the aging summary folds into a new one along with newly-aged exchanges | one cheap call |

L4 is what keeps the summary from freezing — the previous summary is an input to the
next one, so information is refined rather than duplicated.

### The trap nobody documents

Hermes ships a per-exchange micro-compaction mode and it is **off by default**. The
reason, verbatim from its source:

> every pass rewrites the prompt prefix and breaks the provider prompt cache

That is the counterintuitive result: **compacting often costs more than it saves.**
Rewriting old messages invalidates the cached prefix, and the next request re-bills
the entire prompt at full price.

**Derived design rule: compact rarely and in large steps, never per turn.** Use a
high trigger (on the order of 70–80% of the window) and an aggressive target, so each
compaction buys many turns of headroom.

### The trigger

Pydantic AI exposes `ctx.context_window_used` — the fraction of the window occupied
as of the last response, or `None` when unknown. That is the natural input to
`should_compress()`.

Handling `None` is not a detail. With providers that do not report usage, fall back
to a local token estimate. Without that fallback the agent never compacts and dies of
context overflow in production.

### The port contract

Lifecycle taken from Hermes' context-engine base class, which is production-proven.

```python
# ports/context_engine.py
class ContextEngine(Protocol):
    def on_session_start(self, session: SessionRef) -> None: ...
    def update_from_response(self, usage: Usage) -> None: ...
    def should_compress(self, state: ContextState) -> bool: ...
    def compress(self, history: Messages) -> CompactionResult: ...
    def on_session_end(self, session: SessionRef) -> None: ...
```

Two contract details that prevent expensive mistakes. `on_session_end` fires **only
at real session boundaries** — explicit close, reset, expiry — never per turn. And
`compress` takes history and returns a result: it does **not** mutate the live
conversation.

### Concurrency and durability come free

Hermes runs compression over a deep copy and publishes only on an admitted commit
fence, one pass per session under a durable lock, with sessions concurrent.

Here that is free: **compaction is a `@DBOS.step()`**. The durable lock, isolation
and crash recovery are DBOS's job. This is one of the places where the stack choice
pays the most.

> **Invariant that breaks the conversation.** If a cut leaves a tool call without its
> return — or the reverse — the provider errors and the conversation is unusable.
> **Always cut at complete-exchange boundaries.** Write a test that generates
> tool-bearing histories and verifies pairing after every rung.

The summary is **persisted** in `ConversationStore` as a compaction checkpoint.
Recomputing it on every load pays twice for the same work.

---

## 6. Skills and MCP

### Skills: metadata in the prompt, body on demand

A skill is a `SKILL.md` file with frontmatter. The rule that makes it cheap: **only
the index goes into the system prompt** — name, one-line description, path — and the
model reads the full body with a tool when it decides it needs it.

OpenClaw injects roughly one hundred characters per skill. With fifty skills that is
about five thousand characters instead of two hundred thousand. Best
effort-to-benefit ratio of any token optimization in this plan.

```python
# ports/skill_registry.py
class SkillRegistry(Protocol):
    def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        """Name, description and path only. Goes into the system prompt."""
    def read(self, name: str) -> str:
        """Full SKILL.md body. Invoked by the skill_view tool."""
```

Filtering happens twice: at load time by frontmatter (required binaries, environment
variables, platform), and per profile — each `AgentProfile` declares which skill
namespaces it sees, so the fraud agent never receives the delivery agent's index.

### MCP is not a port, it is a composition

MCP enters **inside `ToolProvider`**. Pydantic AI ships `MCPToolset`, which wraps the
FastMCP client and speaks stdio, Streamable HTTP and SSE. The `ToolProvider` adapter
composes the vertical's local tools with whatever `MCPToolset`s the profile declares
and returns a single set.

`MCPToolset` also exposes resources — `list_resources()`, `read_resource(uri)` — and
converts types: server image content arrives as binary image, audio as binary
content. That connects MCP to the multimodal subsystem with no glue code.

> **Non-negotiable.** MCP tools pass through **exactly the same `ToolPolicy`** as
> local ones, and their results are wrapped in untrusted-content delimiters. An MCP
> server is third-party code returning text the model will read: it is the natural
> vector for indirect prompt injection.
>
> Also give `mcp_*` results a smaller result budget than local tools. Hermes uses 50K
> against 100K characters, with this stated reason: MCP servers routinely return
> un-paginated 20–50K payloads.

---

## 7. Multimodality

Pydantic AI covers input with `ImageUrl`, `AudioUrl`, `VideoUrl`, `DocumentUrl` and
`BinaryContent`. What gets built is the governance around it: where binaries live,
who may request them, and how the turn waits for them to arrive.

> **Privacy decision, not a performance one.** With `ImageUrl` or `AudioUrl`,
> Pydantic AI **sends the URL to the model provider, which downloads the file from
> its side**. For an incident system whose evidence may contain personal data, that
> exposes a URL reachable from the provider's infrastructure.
>
> The alternative is sending bytes as `BinaryContent`, or very short-lived signed
> URLs. Decide this in phase F7 and write it down — do not discover it during an
> audit.

### The unification: evidence uses the same mechanism as approval

Pydantic AI's deferred tools cover **two cases, not one**: tools requiring approval,
and tools executed externally. In both, the run ends and returns
`DeferredToolRequests`.

So "ask the user for a photo of the package" needs no new machinery. It is an
externally-executed tool, walking the same path as an approval:

1. **Agent** calls `request_evidence(kind="photo", reason="photo of the damage")`.
   Declared as externally executed, so the run ends and returns the deferred request.
2. **HumanGateway** publishes the request to the channel. Same port that publishes
   approvals; the request type changes, the mechanism does not.
3. **DBOS workflow** waits durably with `DBOS.recv()`. No hung process; the service
   may restart or redeploy.
4. **IngestMedia** use case validates type and size, stores via `MediaStore`, records
   in `AuditSink`, returns a `MediaRef`.
5. **DBOS workflow** resumes. The binary enters as `BinaryContent` in the deferred
   tool's result. To the agent it was simply a tool that returned an image.

One mechanism covers approvals, evidence requests, and any future mid-turn human
interaction. That is why the port is `HumanGateway` and not `ApprovalGateway`: the
right name describes the question it answers, not the first use case that motivated
it.

### Audio in, two paths

If the profile's model accepts native audio, the file is passed directly as
`BinaryContent`. If not, `SpeechPort` transcribes first and the agent receives text.
The port exists precisely so that decision is profile configuration and not a branch
in core code.

### Voice out: phase 2, and on demand

Synthesis is modeled as `SpeechPort.synthesize(text) -> bytes`; the result is stored
in `MediaStore` and the channel sends it. Enabled by a profile flag, never by
default: generating audio on every response multiplies cost and latency with nobody
having asked.

---

## 8. How an agent is characterized

An agent is data, not a subclass.

```python
# domain/profile.py
@dataclass(frozen=True)
class AgentProfile:
    id: str
    persona: str                        # system prompt
    toolsets: tuple[str, ...]
    mcp_servers: tuple[MCPServerRef, ...]
    skill_namespaces: tuple[str, ...]
    model: str
    max_iterations: int
    max_cost_usd: Decimal
    approval_rules: tuple[ApprovalRule, ...]
    compaction: CompactionPolicy        # threshold, target, rungs
    media: MediaPolicy                  # input, evidence, voice
```

| | Fraud analyst | Delivery optimizer |
|---|---|---|
| tools | `sql_readonly`, `case_notes` | `routing`, `pricing`, `orders` |
| mcp | data warehouse server | maps and traffic server |
| skills | fraud typologies, escalation criteria | zone rules, refund policy |
| approval | `freeze_account` always | price change above 15% |
| compaction | high threshold, investigations run long | short window, decides in seconds |
| media | document input, no voice | delivery photo as evidence |

**Adding a vertical is one profile file plus one tools package.** Zero changes to
`domain/`, `application/` or `ports/`. Defend this with a test that fails if someone
breaks it.

---

## 9. Known risks

| Risk | Early signal | Mitigation |
|---|---|---|
| Compaction breaks the prompt cache | Cost per turn rises after enabling compaction | Compact rarely, in large steps; high threshold, aggressive target; never per turn |
| Cut splits a tool call from its return | Provider returns 400 after a compaction | Cut only at exchange boundaries; pairing test per ladder rung |
| `context_window_used` returns `None` | Agent never compacts and dies of overflow | Local token estimator as a mandatory trigger fallback |
| Indirect injection via MCP or web | Agent follows instructions that arrived in a tool result | Untrusted-content delimiters on every `mcp_*`, web and media result |
| Evidence URL exposed to the provider | Files with personal data downloaded externally | Send `BinaryContent`, or short-lived signed URLs; decision written in F7 |
| `AgentRunner` over-abstracted | Adapter starts reimplementing message handling or retries | Port mirrors Pydantic AI's contract; if it grows, trim it |
| Non-deterministic DBOS steps | On resume, a step produces a different result | Clock, UUID and randomness generated inside a step, never in the workflow body |
| Audit inside the rollback | A failed turn leaves no trace | `AuditSink` writes outside the domain transaction; append-only, never update |
| Provider errors treated as one | Credential rotated on a 429 that came from the upstream model | Own error classifier in a late phase: five strategies, not twenty-five reasons |


---

## 13. Knowledge: Skills or KnowledgeBase

Two ways to give an agent business information, and the deciding question is **not**
retrieval quality — it is **who edits it**.

> **Skills** = *how* to do things. Static, in git, written by engineers. Procedures,
> escalation rules, policies.
>
> **KnowledgeBase** = *what* the business offers today. Dynamic, in the database, edited by
> administrators. Prices, hours, catalogue, availability.

A price lives in `KnowledgeBase` even when it is two lines long, because a `SKILL.md` is a
file in git and an administrator is not going to open a pull request to change a price.

### When each mechanism actually wins

| | Skills | KnowledgeBase `FULL_TEXT` | KnowledgeBase `SEMANTIC` |
|---|---|---|---|
| Corpus size | tens of documents | under ~20–30K chars | above that, to thousands |
| Precision | model reads the whole doc — **zero retrieval error** | same | retrieval can miss or fetch the wrong chunk |
| Prompt cost | ~100 chars per skill, body on demand | injected, capped at 6K | embedding call per query + top-k |
| Update path | edit a file, commit, deploy | admin UI, immediate | admin UI + re-embed changed chunks |
| Auditability | file mtime | versioned rows with `effective_from` | same |

**Rule of thumb:** under ~20–30K characters total, `FULL_TEXT` wins outright. Adding
embeddings there buys nothing and adds a chance of fetching the wrong chunk.

### Day 1 is Skills plus `FULL_TEXT`

No embeddings, no vector index, no pgvector. `SEMANTIC` and `HYBRID` exist in the port
signature so enabling them is one config line per collection rather than a redesign.

When the corpus does outgrow `FULL_TEXT`, **pgvector in the same Postgres instance is the
first move**, not a dedicated vector database — Postgres gives semantic *and* keyword
retrieval in one query (`pgvector` + `tsvector`), which is what hybrid scoring needs.

### The write boundary is structural, not a permission check

```
KnowledgeBase   read only.  Injected into StartTurn.
KnowledgeAdmin  write.      NEVER injected into anything the agent touches.
```

Two ports, not one with a role check. The difference: **a prompt injection cannot call a
method that is not on the object the agent holds.** A single port with an `if is_admin`
guard is one refactor away from being wrong, and when it is wrong nothing fails.

`AdminIdentity` is a distinct type that cannot be produced from a `CallerIdentity`. The
type checker enforces the boundary before a test would.

Updates create versions; nothing is overwritten. `effective_from` in the future is a
scheduled change — new hours starting Monday — and retrieval already filters on it, so no
separate scheduler is needed.

---

## 14. Prompt injection: five layers

Ordered from structural to defensive. The first layer is the only one that cannot be
evaded, so it carries the most weight.

### Layer 1 — Absence of surface

There is **no knowledge-write tool**. None, ever. Persistent corpus poisoning is
impossible when no reachable method can write to the corpus.

This matters more here than anywhere else in the system: an MCP injection is ephemeral,
but a poisoned knowledge base affects **every future conversation** until someone notices.

### Layer 2 — Untrusted-content delimiters

Every one of these is wrapped, case-insensitively so a differently-cased tag cannot forge
or prematurely close the boundary:

| Source | Why it is untrusted |
|---|---|
| `mcp_*` | third-party server code returning text the model reads |
| `web_*`, `browser_*` | the open internet |
| knowledge excerpts | a human wrote the corpus |
| media / transcriptions | uploaded by whoever is in the conversation |
| **peer agent answers** | another agent is a third party; it may have read something hostile |
| `sql_readonly` rows | a customer typed that text into your own database |

The last two are the ones teams skip. *"Our own agent"* and *"our own database"* are not
trust arguments — an agent can be misled, and a database row is user-supplied text that
merely took a longer route.

### Layer 3 — Identity separation by type

`CallerIdentity` cannot become `AdminIdentity`. Different types, different routes,
different policies. There is no code path that widens a chat client into an administrator.

### Layer 4 — Collection isolation in the query

Tenant and collection filtering happen **inside** the SQL, never as a post-retrieval
filter. Cross-tenant leakage through a shared index is the classic multi-tenant retrieval
breach, and post-filtering is how it happens.

An agent requesting a collection it may not read gets an **empty result, not an error** —
an error message confirms the collection exists, which is itself a leak.

### Layer 5 — Relevance floor

Without `min_score`, retrieval always returns *k* results even when nothing is relevant,
and the agent receives confidently irrelevant context. **An empty result is a valid
answer.**

---

## 15. Agent to agent

The scenario: a customer-service agent cannot answer from its Skills or `KnowledgeBase`. It
asks the person's personal-assistant agent, which suspends its own turn to ask the human,
and the answer flows back.

### It is the same mechanism, for the third time

```
tool the agent cannot resolve locally
  → turn SUSPENDS  (DeferredToolRequests)
  → something external answers
  → DBOS.recv() wakes it, turn RESUMES
```

`ask_peer` is an externally-executed deferred tool — identical in shape to
`request_evidence` and to an approval. The answering agent then suspends on
`HumanGateway`. **Two nested suspensions, zero new suspension machinery.**

`AgentMailbox` exists only for addressing and routing, not for waiting.

### A2A: shape now, wire protocol in D2

Agent2Agent reached **v1.0 under the Linux Foundation**, backed by Google, Microsoft and
Salesforce, though adoption is measured rather than universal. So the domain types are
modelled on A2A's concepts — capability discovery, tasks, modality negotiation — without
implementing the wire format on day 1. D2 completes it as an adapter swap.

For reference, Hermes' own A2A plugin is 2,345 lines. Aligning the shape costs nothing now;
adopting the protocol later then costs an adapter instead of a redesign.

### The five risks

| Risk | Control |
|---|---|
| A peer's answer is untrusted | Same delimiters as `mcp_*`. Being "our" agent is not a trust argument. |
| Loops between agents | `PeerPolicy.max_hops`. Every hop is a full turn with model calls on both sides — far costlier than an ordinary loop. |
| Unauthorised paging | `PeerPolicy.peers` allowlist, checked on **both** sides. A one-sided check is bypassable by whoever controls the other side. |
| Data fishing | `PeerPolicy.visibility` defaults to `NONE` — the peer receives the question and nothing else. A CS agent must not be able to ask an assistant what the person has scheduled next month. |
| **Compounding latency** | See below. This one is a product problem, not a technical one. |

### The latency problem is a product problem

CS suspends → the assistant's turn runs → the human answers when they can → CS resumes.
That can take hours. **Meanwhile a customer is waiting.**

So the suspension must be **communicable**: before suspending, the CS agent tells its own
user *"I am checking on that, I will get back to you."* A turn that suspends silently looks
like a system that hung, and the customer leaves.

---

## 16. Transcript and audit API

The read model behind a conversation viewer. The frontend is built separately; the
endpoints are the contract.

### It stores nothing new

`ConversationStore` already holds the messages. `AuditSink` already holds tool calls,
policy decisions and human decisions. This is a **projection** over rows that already
exist — which is why it is read-only and why it is a separate port.

### Store everything, filter on read

`Audience` decides what comes back; storage keeps everything.

An audit that stored a redacted copy cannot answer a question nobody anticipated. And two
write paths drift — with the drifting one always being the one nobody reads until an
incident.

| Entry kind | User | Admin |
|---|---|---|
| user message, agent message | ✅ | ✅ |
| pending placeholder | ✅ | — |
| tool call, arguments, result | — | ✅ |
| reasoning / chain of thought | — | ✅ |
| pending request (which tool, since when) | — | ✅ |
| policy denial | — | ✅ |
| knowledge hit (which document answered) | — | ✅ |
| peer exchange | — | ✅ |
| compaction event | — | ✅ |
| human decision | — | ✅ |
| cost per turn | — | ✅ |

`VISIBLE_TO` in `domain/transcript.py` is the single source of truth for that table, and a
test asserts it covers every `EntryKind` so it cannot rot. A kind added without a
visibility entry is invisible to everyone — the safe failure.

### The one thing a user must still see

A user must not learn **which** tool is pending, but must see **that** something is.
`PENDING_PLACEHOLDER` exists for that. A conversation that silently stops while the agent
waits three days for an approval looks broken, and the user leaves.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /conversations` | Inbox. `suspended_only=true` is the operational queue: everything stuck on a human, oldest first. |
| `GET /conversations/{session_id}` | The timeline. `audience` selects user or admin projection. |
| `GET /conversations/{session_id}/pending` | What this conversation is waiting on, and since when. |
| `GET /turns/{turn_id}` | One turn in full: tools, reasoning, cost, decisions. |

Two rules for all of them. **Cursor pagination, never offset** — a live conversation grows
while someone reads it, and offsets then skip or repeat entries. And **tenant scoping goes
in the query**: these endpoints return whole conversations, so a missing tenant predicate
here is a cross-tenant breach with a friendly UI on top.

### Reasoning deserves its own retention decision

Reasoning traces are bulky and contain intermediate speculation the model discarded —
including guesses about a user that were never said out loud. Storing them forever is a
liability, not an asset. The retention window is an open decision, tracked as `t-f10-05`.


---

## 18. Queueing, coalescing and what channels actually tell us

### The queue is DBOS, and it is all configuration

| Need | DBOS mechanism |
|---|---|
| One turn at a time per session | `partition_concurrency=1`, `queue_partition_key=<session_id>` |
| Coalescing window | `delay_seconds` on enqueue |
| Second message joins the pending turn | `deduplication_id` + `duplication_policy="return-existing"` |
| Global cap on concurrent turns | `global_concurrency` |
| Per-tenant rate limit | `limiter={"limit": N, "period": S}` |
| Durable timeout | `SetWorkflowTimeout`, persisted across restarts |
| Priority | `SetEnqueueOptions(priority=N)` — lower is higher |

No Redis, no broker. The reason is atomicity, not throughput — see D21.

### Channel capabilities, verified

This matrix decides what the coalescing window can rely on, and the answer is **less than
you would hope**.

| Channel | Transport in | Can we SEND "typing"? | Can we RECEIVE the user's typing? |
|---|---|---|---|
| **WhatsApp Cloud API** | webhook (Meta POSTs to us) | ✅ up to 25s, needs the inbound `message_id` | ❌ **no inbound typing event** |
| **Telegram Bot API** | webhook, or long-poll `getUpdates` | ✅ `sendChatAction`, ~5s, must be re-sent | ❌ **not exposed to bots** |
| **Chatwoot web widget** | ActionCable WebSocket | ✅ `toggle_typing_status` | ✅ contact typing is available |
| **Chatwoot relaying WhatsApp/Telegram** | webhook to the bot URL | ✅ propagates outbound | ❌ it can only relay what the provider gives, which is nothing |
| **Own app** | your WebSocket | ✅ trivial | ✅ trivial |

Neither WhatsApp nor Telegram is a WebSocket. Both are **webhook push over HTTPS**: the
platform POSTs to an endpoint. Telegram additionally allows the bot to long-poll
`getUpdates`, which is the bot pulling, not the platform streaming.

### So the debounce is time-based, not typing-based

See D22. The window resets on each arriving message and is capped, which needs no typing
signal and therefore works on every channel:

```
first message  -> start window (~1.5s), buffer it
next message   -> buffer it, RESET the window
                  unless total wait >= max_wait (~6s), then fire now
window expires -> drain buffer, run ONE turn
```

The cap matters: without it, somebody typing five short messages in a row keeps postponing
their own answer indefinitely.

Where inbound typing *is* available — an own app, or Chatwoot's web widget — it can extend
the window as an optimisation. It is never a requirement.

### Outbound typing is still worth wiring, for a different reason

It does nothing for coalescing, but it is the cheapest answer to the communicability problem
in D17: while a turn is running or suspended, a typing indicator tells the user something is
happening. WhatsApp gives 25 seconds per send; Telegram about 5 and needs re-sending.

A turn that suspends silently looks like a hung system. This is how it stops looking that
way for the first 25 seconds.

### Chatwoot as the channel layer — what it replaces and what it does not

A reasonable option: the core owns all agent logic, Chatwoot owns the interface and the
inbox. Its AgentBot integration POSTs conversation events to a bot URL and accepts replies
through its API, which fits cleanly as a driving adapter.

**What it gives you.** Multi-channel routing, contact management, an agent inbox — and
**human handoff**, which is exactly the A5 gap deferred to D2. Handing a conversation to a
person is Chatwoot's core product.

**What it does NOT replace.** Chatwoot shows *messages*. It does not show tool calls,
reasoning, policy denials, knowledge hits or per-turn cost. So it replaces the **user-facing
half** of the F10 transcript viewer and none of the **admin half** — that projection stays
ours, and `TranscriptReader` is still needed.

**What it costs.** An extra hop of latency; a data model to map `SessionRef` onto
(conversation, contact, inbox); and self-hosting means another Rails app with its own
Postgres **and its own Redis**. That Redis is Chatwoot's, not ours — D21 is about our work
queue and is unaffected.

### Building typing state yourself is easy; the client around it is not

In an own app, typing state is a WebSocket message: the client emits `typing: true` on
keystroke (locally debounced), the server broadcasts it, a timeout clears it. Roughly twenty
lines.

The hard part was never typing. It is reconnection, delivery receipts, offline queueing, push
notifications, media upload and read state. Typing is the easy two percent of a chat client,
so it should not be the reason to build or avoid one.
