# Roadmap

Each phase closes with a **verifiable test**, not an estimate. Every phase leaves the
system working and adds exactly one capability.

The first vertical lands at **F4, before** the advanced capabilities. That is
deliberate: it is the proof that the port contract holds. Building compaction, skills
and multimodality on top of a contract that already broke is building on sand.

---

## Day 1 — one process, one Postgres instance, two logical databases

### F0 · Executable skeleton

FastAPI + Pydantic AI + LiteLLM as a library. One toy tool. No DBOS, no policy, no
persistence.

**Done when:** a POST returns a model response that used the tool, with the
`tool_call` visible in the log.

### F1 · Real hexagonal core

`domain/`, `application/` and the seven skeleton ports appear. `ToolPolicy` and
`AuditSink` hooked into `before_tool_execute`. Postgres persistence.

**Done when:** a tool denied by policy does not execute, the model receives the
refusal as the tool result, and the denial is in the audit table.

### F2 · Durability

DBOS enters as a driving adapter. The workflow wraps use cases in steps. Second
database in the same Postgres instance.

**Done when:** the process is killed mid-turn during a multi-tool run; on restart the
turn completes **without re-running** the tool that already ran.

### F3 · Deferred human interaction

`requires_approval`, `DeferredToolRequests` to `HumanGateway`, durable wait with
`DBOS.recv()`, decision endpoint. Build it generic from the start — F7's evidence flow
reuses it.

**Done when:** a turn is left waiting for approval, the service is redeployed, and
approving twenty-four hours later resumes the turn correctly.

### F4 · First vertical — the contract test

One real profile and its tools package. **Start with delivery, not fraud** — short
turns, bounded domain and simple approvals exercise the whole core without the
regulatory weight.

**Done when:** the vertical works end to end and the diff touches no file under
`domain/`, `application/` or `ports/`.

### F5 · Context compaction

`ContextEngine` over `ProcessHistory`. Full L1–L4 ladder. Trigger on
`context_window_used` with a local-estimate fallback. Compaction as a `@DBOS.step()`.
Persisted checkpoints.

**Done when:** a two-hundred-turn conversation still answers well, cost per turn stays
flat instead of growing, and every tool call/return pair remains matched after each
compaction.

### F6 · Skills and MCP

`SkillRegistry` over `SKILL.md` files with a lazy index and a `skill_view` tool.
`MCPToolset` composed inside `ToolProvider`, with the same policy and a reduced budget
for `mcp_*`.

**Done when:** the agent solves a task by reading a skill that was not in its prompt,
and an MCP tool denied by policy is recorded exactly like a local one.

### F7 · Multimodal input and evidence

`MediaStore`, the `IngestMedia` use case, and `request_evidence` as an
externally-executed deferred tool. Written decision on bytes versus signed URL.

**Done when:** the agent asks for a photo, the turn waits, the user uploads it from
another device, and the turn resumes with the image as the tool result.

---

## Day 1.5 — cold start and the operator surface

### F11 · A clone, an empty Postgres, and one command

**Done when** a fresh clone with an empty PostgreSQL instance and a credentials file
starts with one command, and an operator can create an agent, give it tools, connect it to
an MCP server, point it at another agent, watch a tool refused, approve that refusal, have
one agent orchestrate the other, and read the whole exchange in the audit trail — **without
writing a line of SQL, and without a code change for any of it except a new tool.**

The exception is the contract, not a caveat: writing a new TOOL is code, because a tool is
behaviour. Everything else — which tools an agent may use, which MCP servers it connects
to, which agents it may ask, what needs approval — is configuration, or the port cut is
wrong.

That criterion is written against what actually happened the first time anyone tried to
use this system. Everything below is a step that had to be performed by hand, and every
one of them was invisible until a human sat at a terminal:

| What a human had to do by hand | Why nothing complained |
|---|---|
| `CREATE DATABASE agent_core_app` and `..._dbos` | `start_container` migrates and deliberately does not create; every test created its own |
| export `AGENT_CORE_DATABASE_URL` and `MINIMAX_API_KEY` | the process never reads `.env`; only tests do |
| repoint both profiles off `claude-sonnet-5` | no test ever served a shipped profile against a real provider |
| `INSERT INTO policy_rules` | **nothing in production writes that table** — only tests |

The pattern is the one this build hit seven times: **the fixture that supplies a missing
collaborator is exactly what stops anyone noticing it is missing.** F11 is that pattern
applied to the last mile — the mile a test never walks, because a test is already inside.

#### Why the CLI is the phase, and not an accessory

Agents are *made, used and judged* at a terminal. The HTTP surface answers 202 and polls;
the channels deliver text. Neither shows the thing an operator has to see to trust the
system: which tools a profile actually resolved to, which rule admitted or refused each
one, what the model was handed, and what a user would have seen instead. The console is
where the silent-bug table in `CLAUDE.md` becomes visible to a person — four of those five
areas have no failing test by definition, and a human reading a real exchange is the only
check they will ever get.

#### The two design decisions this phase makes, and what they cost

**1. Bootstrap is opt-in, not automatic.** `Settings` carries no admin URL today on
purpose: *a deployment that hands its application superuser credentials has a bigger
problem than a missing table.* That reasoning survives. So `AGENT_CORE_ADMIN_DATABASE_URL`
is **optional**: present, startup creates the two logical databases and migrates,
idempotently; absent, startup migrates only and a missing database fails loudly naming the
exact command. Development leaves it in `.env` and gets one command. Production sets it for
one bootstrap run and removes it. The app never *requires* superuser to run.

**2. Policy is reviewed configuration, not a console command.** A `:allow` command would be
the identity widening non-negotiable #9 forbids — the console runs as a `CallerIdentity`
and policy is an administrative write. Rules therefore live in `Core/policy/*.yaml` and are
applied at startup the way migrations are: declarative, diffable, idempotent, and reviewed
before they are in force. The cost is that changing a rule needs a restart, and that cost
is deliberate — a permission that can be granted at a prompt is a permission granted
without a record.

## Day 1.6 — one agent asking another

### F12 · Agent-to-agent orchestration

**Done when** an operator gives one agent a question outside its competence, that agent
delegates to a peer its own profile names, the peer answers under **its own** identity and
permissions, and the first agent finishes the turn with that answer — surviving a redeploy
in the middle, and with the answer treated as untrusted text the whole way.

Anchored as `t-f11-42` … `t-f11-46`, which were written before this section existed: the
loop arrived as a finding, not as a design, and this is that design written down after the
fact. That order is itself the lesson — see *How this was found*, below.

#### The shape, and why each piece is separate

Delegation is **four moves**, and the reason they are four rather than one function call is
that a peer may take hours to answer:

| # | Move | Where it lives |
|---|---|---|
| 1 | A's model calls `ask_peer`; the turn **suspends** rather than blocking | `agent_pydantic/runner.py` (`t-f11-41`, done) |
| 2 | The suspension becomes a real `AgentMailbox.ask()`; the correlation id is minted **and persisted together** | `workflow/turn_workflow.py` (`t-f11-42`) |
| 3 | A worker claims the ask and runs a turn **as B** | `driving/peers/worker.py` (`t-f11-43`) |
| 4 | The answer resumes A's turn under the provider's **original** `tool_call_id` | `workflow/turn_workflow.py` (`t-f11-44`) |

Move 3 is a **driving** adapter, the sibling of `scheduler/cron.py`: it claims work from
outside and calls a use case. It is not a service A calls.

#### The five properties that must hold, and what breaks without each

**1. The answering turn runs as B — B's profile, B's toolset, B's policy, B's budget.**
Running it under A's identity makes delegation a privilege escalation dressed as a
question: A asks B, B has tools A does not, and if the turn runs as A then A has just used
them. It would be invisible, because the audit row would name A and everything would look
correct. The two-sided allowlist exists to stop exactly this, and it stops nothing if the
identity does not travel.

**2. The wait is durable, and it is not sixty seconds.** `DBOS.recv()` defaults to a
60-second timeout (`docs/FIELD-NOTES.md`), which is the trap F3 was warned about. B may
itself suspend to ask a human. A durable wait that quietly becomes a one-minute wait looks
fine on a fast machine and fails the first time a person is slow.

**3. The `tool_call_id` is the provider's, byte for byte.** Non-negotiable #5. A regenerated
or normalised id surfaces as a provider 400 much later, never as a failing test here.

**4. A peer's answer is untrusted content.** Non-negotiable #10. *"It is our own agent"* is
not a trust argument: an agent can be misled, and then it is a confused deputy holding our
credentials. If B was the victim of an injection, what it sends A is hostile text with
friendly provenance. `read_answer` already returns it fenced — the risk here is
**double-wrapping**, not forgetting to wrap.

**5. The hop count travels with the ask.** A cycle A→B→A that resets the counter at every
hop is a limit that never fires.

#### What suspends, and why they are one mechanism

Three things park a turn and wait for something outside it: a peer ask, a human approval,
and an evidence upload. They are deliberately the same mechanism — a deferred tool call
whose result arrives later — and `PendingKind` tells them apart so the workflow can route
each to something that can actually answer it. Publishing a peer ask to a person asks a
question nobody can answer; `t-f9-08` exists for that.

`t-f11-45` finishes the set: an approval currently **blocks** inside the run rather than
suspending, because until `t-f11-41` there was no way to end a run with an unanswered call.
There is now, and F3's criterion — approve 24 hours later, after a redeploy — is only
reachable through it.

#### What this phase deliberately does NOT do

- **No agent discovers another.** A profile NAMES its peers, and an unnamed peer is refused.
  `PeerPolicy.may_ask` is an allowlist whose empty value means *nobody*, and that default is
  the whole design: a permission model whose default is "anyone" is not a permission model.
- **No agent is an administrator.** The worker runs under a `CallerIdentity`, never an
  `AdminIdentity` (non-negotiable #9). An answering agent is a caller with no human behind
  it.
- **No shared conversation.** A fresh session per answered ask, for `cron.py`'s reason:
  inheriting a conversation would let one customer's history answer another's question.

#### How this was found, which is the part worth keeping

Every piece of the ASK side and the TRANSPORT was built and tested across F9: the durable
mailbox with an exactly-once `claim_next` and a concurrent-claim test behind it, the hop
limit, the two-sided allowlist, the A2A adapter, `ask_peer` as a shared deferred tool, and
`PendingKind.DELEGATION` so a peer ask is never published to a human. `t-f11-14` then made
a profile able to name a peer.

**None of it had ever run.** `ask_peer` resolved into the toolset and the policy engine
refused it — `deny [no matching rule]`, correctly, because no rule granted it — so the model
was never offered the tool and the deferred path was never walked. Granting it in YAML,
which is exactly what this architecture claims should be sufficient, is what finally reached
the code nobody had executed: first a crash in the runner, then an empty middle where the
worker should be.

That is the ninth instance in this build of one shape — **a collaborator every test supplied
and production never exercised** — and the queue with no consumer is its purest form: a
durable, correct, concurrency-tested mechanism built for a caller nobody wrote. The table in
`docs/STATE.md` lists the other eight.

## Day 2 — multi-tenant

### D2 · LiteLLM becomes a proxy, plus voice out

LiteLLM moves from library to a separate process with its own database (third
database, same instance). Virtual keys per tenant, per-team budgets, centralized spend
tracking. `SpeechPort` gains synthesis, enabled by a profile flag.

**Done when:** two tenants with different budgets run in parallel, one exhausts its
own and is cut off by the proxy — and the only code change was the adapter's
`base_url`.

### What changes and what does not

| Changes | Does not change |
|---|---|
| LiteLLM: in-process library → separate proxy | `ModelGateway`'s interface — only the `base_url` |
| Third database, same Postgres instance | `domain/`, `application/`, `ports/` |
| `ToolPolicy` gains a tenant dimension | The DBOS workflows |
| Budget enforced at the proxy via virtual keys | The compaction ladder |
| Human requests routed per tenant | Skills, MCP servers, tool packages |
| `SpeechPort` gains synthesis | Agent profiles |

Day 2 **does not require a second Postgres instance**. Three logical databases in one
instance are enough. Splitting instances is an operations decision — blast radius,
backups, independent scaling — and is worth taking when an incident justifies it, not
before.

---

## Day 2.5 — a provider you did not configure by hand

### F13 · Provider and tenant provisioning

**Not built. Specified here because the gap was found by asking a question the system could
not answer**, and writing it down now is cheaper than rediscovering it on the first machine
that is not this one.

**Done when** bringing a new machine up is: point it at a database, start it, name a
provider once — and every tenant that appears afterwards gets its own virtual key, its own
budget and its own spend line without anyone opening the proxy's UI.

#### What exists today, exactly

Day 1 resolves a credential by litellm's own convention: `MINIMAX_API_KEY` and friends,
read from the environment, with `.env` layered under it and the mapped names exported
(`t-f11-23`). `preflight` reports whether each one resolves, by NAME and never by value.
That half works and needs nothing.

Day 2 sets `AGENT_CORE_LITELLM_BASE_URL` and the same code talks to a proxy instead — which
is the whole point of `t-d2-01`, and the part of it that is real: the adapter change is one
URL.

**What nobody built is everything around it.** Registering a model on the proxy, creating a
team, creating a virtual key, granting the team its models — all of that was performed by
hand through the proxy's API while this system was being built, and nothing in the tree
does any of it.

#### The defect that question surfaced

`models.py::proxy_model` builds `LiteLLMProvider(api_base=base_url)` **with no `api_key`**.
So in proxy mode this code sends no virtual key at all, and **per-tenant separation is not
reachable through the adapter.**

`test_proxy_mode.py` knows: its own docstring records that `proxy_model` takes no key and
that its signature was frozen at `t-f0-04`, and it proved per-team spend separation by
using the two keys **directly** rather than through our code. That is an honest test of the
proxy and not a test of this system's use of it.

So `t-d2-01` is real for what it claims about the ADAPTER — the code change is one URL — and
D2's done-when, *"two tenants with different budgets run in parallel and one is cut off by
the proxy"*, is **not** met on the path production takes. The anchor is qualified rather
than re-opened, because what it built is right; what is missing was never anyone's.

#### The decision, made 2026-09-11: one key per TENANT

One team per deployment, one virtual key per tenant, created the first time that tenant is
seen. `CallerIdentity` already carries `tenant_id`, so the key travels with the turn and no
port changes shape.

**Not per agent**, and the reason is worth keeping. A per-agent budget ALREADY EXISTS:
`max_cost_usd` in the profile, enforced per turn. Adding a second one in the proxy would be
two enforcement points for one number, and two enforcement points for one number drift.

They are also not the same control, which is why both are wanted:

| | Stops | Lives |
|---|---|---|
| `max_cost_usd` in the profile | a runaway TURN | in our process, and only works while it behaves |
| the virtual key's budget | real SPEND | on the other side of the network, and does not care whether we behave |

The **team** is the unit that owns model grants. Granting models per agent would mean
re-granting every time an agent is added, which is configuration churn for no boundary: an
agent is already restricted by its profile's `model:` line.

#### The CLI shape, and why it is not a console command

```
python -m agent_core provider add <name>      # register a model and its credential, once
python -m agent_core provider status          # what the proxy has, and what it costs
python -m agent_core tenant add <tenant-id>   # a virtual key, a budget, a spend line
```

**A subcommand, not a `:command` in the console** — for the reason `Core/policy` is not a
console command either: creating a key is an administrative write, the console runs as a
`CallerIdentity`, and non-negotiable #9 says a caller is never widened into an
administrator. A permission granted at a prompt is a permission granted with no record.

Auto-creating a tenant's key on first sight is the convenience; it belongs at
tenant-provisioning time, behind an administrative identity, and `preflight` should report a
tenant that has no key rather than the first turn discovering it.

#### What this phase must not do

- **Never print or log a key**, including the one it just created. `preflight` already sets
  the vocabulary: *set* / *missing*, never a value.
- **Never fall back to an unkeyed request** when a tenant's key is missing. That is the
  shape `docs/FIELD-NOTES.md` records for `api_key=None`: a client built without a
  credential reads `OPENAI_API_KEY` and sends it to whatever provider it was pointed at.
  Refuse loudly instead — a missing key must not become someone else's spend.
- **Never store a key in a profile.** A profile is reviewable data an operator reads top to
  bottom; a credential in one is a credential in a code review, a backup and a diff.

## Sizing

> **Superseded** — see *Revised scope* at the end of this document. Kept for the method.

Estimated against measured equivalents in `../Hermes-Core/Hermes`.

### Per subsystem

| Subsystem | Hermes (measured) | Here (estimated) | Why the difference |
|---|---:|---:|---|
| Turn loop | 11,570 | **0** | Pydantic AI |
| Compaction / context | 11,003 | ~750 | `ProcessHistory` gives mechanics; we write the ladder |
| Skills | 10,217 | ~200 | No self-improvement, no marketplace, no sandbox |
| MCP client | 7,662 | ~150 | `MCPToolset` + composition |
| Media / multimodal | 5,715 | ~275 | `BinaryContent` native; we write the governance |
| Approval | 3,624 | ~325 | Deferred tools + `DBOS.recv()` |
| Registry / toolsets | 2,434 | ~300 | Pydantic AI toolsets |
| Guardrails / budget | 1,312 | ~250 | Start with a budget, not five mechanisms |
| Error classification | 1,158 | ~150 | Five strategies, not twenty-five reasons |
| | **54,695** | **~2,400** | |

Plus the layers Hermes does not separate: `domain/` ~350, `application/` ~450,
`ports/` ~300, Postgres persistence ~600, HTTP ~375, DBOS workflows ~250, LiteLLM
adapter ~115, policy engine ~250, wiring ~250.

### Totals

| | Lines |
|---|---:|
| **Core, production code** | **~5,000** (range 4,200–5,800) |
| Tests | ~8,000 |
| Test doubles (11 fakes + fake model) | ~900 |
| **Day-1 repo, no verticals** | **~14,000** |
| Each vertical (tools + profile + tests) | +1,000 to 1,800 |
| Day 2 (multi-tenant + speech synthesis) | +400 to 700 |

**Full repo with one vertical, Day 2 included: ~16,500 lines, of which ~5,700 are
production code.**

Independent cross-check: the sibling `Hermes-Core/Core` project measured **4,510 lines
of hand-written seams** replacing roughly 30,000 lines of Hermes CLI, auth and config,
with 8,848 test lines — nearly 2:1. This bottom-up estimate landed at ~5,000
production and ~8,000 test lines for comparable scope. Two independent methods, same
order of magnitude.

### Where the estimate blows up

One place, and it deserves a name: **the compaction adapter.**

Budgeted at 750 lines. Hermes spent 11,003. That gap is not bad engineering on their
part — it is the cost of chasing quality: semantic deduplication, provider-native
compaction, image handling inside history, timeouts with a commit fence, repair of
broken histories.

**Discipline: treat the L1–L4 ladder as the complete Day-1 scope.** Do not add another
rung until the bill or the answer quality demands it with data.

Two smaller risks: adding an aggregator such as OpenRouter inflates the error
classifier, and adding a streaming UI introduces a surface this estimate does not
cover.

---

## Effort

Not measurable — no benchmark exists. This is judgment from the shape of the work.

**Code generation is not the bottleneck.** The lines get written in a fraction of the
total time. Calendar time is dominated by verification and by human decisions.

| Phase | Sessions | What makes it slow |
|---|---:|---|
| F0 | 1 | Nothing. The only genuinely fast phase. |
| F1 | 2–3 | Postgres schema, migrations, first real integration |
| F2 | 2–3 | Step semantics, determinism, and the kill-the-process test |
| F3 | 2 | Discovering the real deferred-tools API; faking the clock to test 24h |
| F4 | 2–4 | **Domain decisions**, not code |
| F5 | 3–5 | **Longest.** Needs long real conversations and bill measurement |
| F6 | 2–3 | MCP always surprises: servers that hang, return garbage |
| F7 | 2–3 | Real provider limits, real files |
| D2 | 2–3 | New process, third database, real budgets |
| | **18–27** | |

A session is 2–4 hours of focused paired work: **40 to 100 hours**. At three sessions
a week, **6 to 9 weeks**; full time, 2 to 3 weeks.

F5 takes the crown with only ~750 estimated lines, which confirms the point: time does
not correlate with lines. It correlates with how long reality takes to answer.

### The real limit is comprehension, not agent speed

The stated goal is a core **we understand**. That sets a ceiling that is not
technical: comprehension rate.

An agent can deliver 16,500 lines far faster than they can be understood. Accepting
code faster than it is understood produces a codebase nobody understands — which is
exactly the problem being escaped, but worse: Hermes at least is production-tested.

### Loud bugs versus silent bugs

Let the agent run long where a bug is loud. Drive line by line where it is silent.

| Loud — long leash | Silent — close supervision |
|---|---|
| `domain/`, `ports/`, test doubles | DBOS step boundaries |
| HTTP adapters, wiring, migrations | Compaction strategy |
| Per-vertical tool packages | Policy engine |
| Use-case tests | Untrusted-content wrapping |

The right column has one thing in common: **none of those bugs fail a test.** A
non-deterministic step only shows on crash recovery. Compaction breaking the cache
only shows on the bill. A policy hole only shows the day it matters.

The sibling project's `EXTRACTION.md` records three bugs its own tooling had, and
notes verbatim: *"Worth recording, because all three were silent."* Three out of
three.

**Practical adjustment:** to compress the calendar without losing comprehension, the
lever is not more sessions per week. It is doing F0–F4 with the agent on a short
leash, and loosening it only from F5 onward — by then the port contract is understood
and review by diff against a known contract is possible.


---

## F8 · Knowledge: Skills and `FULL_TEXT`

Skills plus a `KnowledgeBase` port running `FULL_TEXT` — no embeddings, no pgvector. Writes
go through `KnowledgeAdmin`, a separate port the agent is never injected with. Documents are
versioned with `effective_from`, so a future date is a scheduled change.

**Done when** an administrator changes a price through the admin endpoint and the next turn
quotes the new price, the change is auditable with its before-value, and an agent whose
profile omits that collection retrieves nothing from it.

Tasks: `docs/TASKS.md` F8 (`t-f8-01` … `t-f8-08`).

## F9 · Agent-to-agent foundations

`ask_peer` as an externally-executed deferred tool — the third user of the suspension
mechanism, after approvals and evidence. A2A-shaped types without the wire protocol. Safety
controls ship now: hop limits, two-sided allowlist, visibility defaulting to `NONE`,
untrusted wrapping of peer answers.

**Done when** a customer-service agent asks a personal-assistant agent, that agent suspends
to ask its human, the answer returns, and an A→B→A attempt is refused at the hop limit.

Tasks: `docs/TASKS.md` F9 (`t-f9-01` … `t-f9-07`).

## F10 · Transcript and audit API

A read-only projection over rows `ConversationStore` and `AuditSink` already write. Two
audiences from one write path. Four endpoints; the frontend is built separately.

**Done when** an operator can page a full conversation with tools, reasoning and pending
state, the same conversation rendered for the user shows only messages plus a pending
placeholder, and neither query can cross a tenant boundary.

Tasks: `docs/TASKS.md` F10 (`t-f10-01` … `t-f10-09`).

## D2 additions

Full A2A wire protocol as an adapter swap (`t-d2-04`), and `SEMANTIC` retrieval via pgvector
per collection (`t-d2-05`).


---

## Revised scope — supersedes the sizing and effort figures above

The tables above were computed for the original F0–F7 scope. F8 (knowledge), F9 (peers) and
F10 (transcripts) were added afterwards, along with profile versioning and coalescing.
Updated figures:

### Added since the first estimate

| Component | Lines |
|---|---:|
| `KnowledgeBase` + `KnowledgeAdmin` adapter, versioned docs | ~450 |
| `AgentMailbox` + peer routing | ~350 |
| `TranscriptReader` + the two-audience projection | ~500 |
| Queue partitioning, coalescing buffer and drain | ~200 |
| Profile versioning and snapshot persistence | ~150 |
| New domain modules (knowledge, peers, transcript) | ~400 |
| Their tests | ~1,500 |

### Revised totals

| | Lines |
|---|---:|
| **Core, production code** | **~7,000** (was ~5,000) |
| Tests | ~11,000 |
| Test doubles (15 fakes + fake model) | ~1,100 |
| **Day-1 repo, no verticals** | **~19,000** |
| Each vertical (tools + profile + tests) | +1,000 to 1,800 |
| Day 2 (proxy, speech, full A2A, handoff) | +1,200 to 1,800 |

**Full repo with one vertical, Day 2 included: ~22,000 lines, of which ~8,000 are production
code.**

The ratio held: roughly 1.6 test lines per production line, which is what a hexagonal design
with a fake per port produces. That is the property being paid for.

### Revised effort

| Phase | Sessions |
|---|---:|
| F0 – F7 (as estimated above) | 18–27 |
| F8 knowledge | 2–3 |
| F9 peer foundations | 2–3 |
| F10 transcript API | 2–3 |
| | **24–36** |

A session is 2–4 hours of focused paired work: **50 to 140 hours**. At three sessions a week,
**8 to 12 weeks**; full time, 3 to 5 weeks.

F8 is cheaper than it looks because day 1 is `FULL_TEXT` — no embeddings, no vector index.
F10 is cheaper than it looks because it stores nothing new. F9 is cheaper than it looks
because it reuses the suspension mechanism. **None of the three needed new machinery, which is
the return on the port design.**

### What F1 and F2 gained

**F1** also delivers profile versioning (D20): `AgentProfile.version`, `profile_version`
persisted on every turn plus the resolved snapshot, and a test proving a turn's audit record
reproduces the persona and rules in force. Tasks `t-f1-17` … `t-f1-19`.

Pulled into F1 rather than left for later because it cannot be filled retroactively — by then
the turns are already written.

**F2** also delivers per-session serialization and message coalescing (D19, D22): a
partitioned DBOS queue with `partition_concurrency=1` keyed on session, a pending-input buffer
drained as the workflow's first step, and a resetting time-based debounce with a maximum wait.
Tasks `t-f2-03` … `t-f2-09`.

All of it is DBOS configuration rather than new code. The tests are the substance: three
messages inside the window must produce **one** turn and one model call; two sessions must run
in parallel while two messages on one session must not; a message arriving mid-turn must
become a follow-up turn rather than a lost message.
