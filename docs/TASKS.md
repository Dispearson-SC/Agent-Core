# Tasks

The index every stub points at. Each task has an anchor, a file, a phase, and a done
criterion. Any agent — Claude or otherwise — can pick up work by reading this file and
opening the file the task names.

**How this connects to the code.** Every stub in `Core/src/` carries a header:

```
Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-02
Status:  TYPES DEFINED / BEHAVIOUR PENDING
```

Follow the anchor here to get the done criterion; go back to the file for the
pseudo-code. Update `Status` when a task closes.

**How this connects to SDD.** A phase is one SDD change. Run
`/gentle-sdd-new` naming the phase, and these tasks become its `tasks.md` work units.
Do not invent tasks outside this file — add them here first so the spine stays single.

**Status legend.** `TODO` · `WIP` · `DONE` · `BLOCKED`

---

## F0 · Executable skeleton

Done when a POST returns a model response that used the tool, with the `tool_call` in the log.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f0-00"></a>t-f0-00 | **GATE — resolve D23: own channel adapters or Chatwoot.** Blocks F0; the first driving adapter is written under one of the two assumptions. | `docs/DECISIONS.md` | **OPEN** |
| <a id="t-f0-01"></a>t-f0-01 | Fake ports for `AgentRunner` and `ToolProvider` — build the fakes before the adapters | `Core/tests/fakes/ports.py` | TODO |
| <a id="t-f0-02"></a>t-f0-02 | Composition root; one place that imports concrete adapters | `Core/src/agent_core/composition.py` | TODO |
| <a id="t-f0-03"></a>t-f0-03 | `POST /turns` returning 202 immediately, never blocking | `adapters/driving/http/routes.py` | TODO |
| <a id="t-f0-04"></a>t-f0-04 | `ModelGateway` over LiteLLM in library mode (`base_url()` returns None) | `adapters/driven/llm_litellm/gateway.py` | TODO |

> Build the fake model provider first. Hermes shipped ~3,821 test files and not one
> reusable fake — every test hand-rolled a mock. Skipping this is why the extracted core
> could not be tested until one was written.

---

## F1 · Real hexagonal core

Done when a tool denied by policy does not execute, the model receives the refusal as the
tool result, and the denial is in the audit table.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f1-01"></a>t-f1-01 | Turn types; add `__post_init__` asserting `pending`/`result` exclusivity | `domain/turn.py` | TODO |
| <a id="t-f1-02"></a>t-f1-02 | `PolicyRule.matches()` — pin semantics with tests **before** any rule is stored | `domain/policy.py` | TODO |
| <a id="t-f1-03"></a>t-f1-03 | `BudgetState.wrapup_notice_due()` — fire once per turn, never per iteration | `domain/budget.py` | TODO |
| <a id="t-f1-04"></a>t-f1-04 | `AgentProfile` loading from YAML | `domain/profile.py` | TODO |
| <a id="t-f1-05"></a>t-f1-05 | `AgentRunner` protocol frozen | `ports/agent_runner.py` | TODO |
| <a id="t-f1-06"></a>t-f1-06 | `ToolPolicy` protocol frozen | `ports/tool_policy.py` | TODO |
| <a id="t-f1-07"></a>t-f1-07 | `ToolProvider` protocol frozen; **no auto-discovery** | `ports/tool_provider.py` | TODO |
| <a id="t-f1-08"></a>t-f1-08 | `ConversationStore` protocol frozen | `ports/conversation_store.py` | TODO |
| <a id="t-f1-09"></a>t-f1-09 | `AuditSink` protocol frozen | `ports/audit_sink.py` | TODO |
| <a id="t-f1-10"></a>t-f1-10 | `ModelGateway` protocol frozen | `ports/model_gateway.py` | TODO |
| <a id="t-f1-11"></a>t-f1-11 | `StartTurn` use case | `application/start_turn.py` | TODO |
| <a id="t-f1-12"></a>t-f1-12 | Pydantic AI adapter + the seven hooks; policy and audit in `before_tool_execute` | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f1-13"></a>t-f1-13 | Postgres `ConversationStore`; index on `(session_id, seq)` | `adapters/driven/persistence_pg/conversation_repository.py` | TODO |
| <a id="t-f1-14"></a>t-f1-14 | Postgres `AuditSink` on a **separate pool** | `adapters/driven/persistence_pg/audit_repository.py` | TODO |
| <a id="t-f1-15"></a>t-f1-15 | Postgres `ToolPolicy`; default DENY, fail closed | `adapters/driven/persistence_pg/policy_repository.py` | TODO |
| <a id="t-f1-16"></a>t-f1-16 | Migrations; two logical databases on one instance | `adapters/driven/persistence_pg/migrations.py` | TODO |

---

## F2 · Durability

Done when the process is killed mid-turn and, on restart, the turn completes **without
re-running** the tool that already ran.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f2-01"></a>t-f2-01 | DBOS workflow as a **driving adapter**; R1–R5 in the file docstring | `adapters/driving/workflow/turn_workflow.py` | TODO |
| <a id="t-f2-02"></a>t-f2-02 | Crash-injection test | `Core/tests/integration/test_durability.py` | TODO |

> Verify `DBOS.recv()` / `DBOS.send()` signatures against the installed version before
> coding. Confirmed the durable-notification primitives exist; the exact signatures were
> not verified.

---

## F3 · Deferred human interaction

Done when a turn waits for approval, the service is redeployed, and approving 24h later
resumes it.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f3-01"></a>t-f3-01 | `HumanGateway` protocol; publish + correlate, **no waiting** | `ports/human_gateway.py` | TODO |
| <a id="t-f3-02"></a>t-f3-02 | `ResumeTurn`; idempotent per `(turn_id, tool_call_id)` | `application/resume_turn.py` | TODO |
| <a id="t-f3-03"></a>t-f3-03 | `DecideApproval`; record before signalling | `application/decide_approval.py` | TODO |
| <a id="t-f3-04"></a>t-f3-04 | Channel adapter + correlation table with unguessable ids | `adapters/driven/human/gateway.py` | TODO |
| <a id="t-f3-05"></a>t-f3-05 | Decide four-eyes: must the approver differ from the requester? Write the answer down | `application/decide_approval.py` | TODO |

---

## F4 · First vertical — the contract test

Done when the vertical works end to end and the diff touches no file under `domain/`,
`application/` or `ports/`.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f4-01"></a>t-f4-01 | Delivery tools package | `adapters/driven/tools/delivery/tools.py` | TODO |
| <a id="t-f4-02"></a>t-f4-02 | `AgentProfile.requires_approval_for()`; unparseable condition must mean **approve** | `domain/profile.py` | TODO |
| <a id="t-f4-03"></a>t-f4-03 | Enable the contract test | `Core/tests/unit/test_contract.py` | TODO |

---

## F5 · Context compaction

Done when a 200-turn conversation still answers well, cost per turn stays flat, and every
tool call/return pair survives each compaction.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f5-01"></a>t-f5-01 | Compaction domain types; ladder L1–L4 | `domain/compaction.py` | TODO |
| <a id="t-f5-02"></a>t-f5-02 | `ContextEngine` protocol; `None`-window fallback is mandatory | `ports/context_engine.py` | TODO |
| <a id="t-f5-03"></a>t-f5-03 | `CompactContext` use case; never retry on no progress | `application/compact_context.py` | TODO |
| <a id="t-f5-04"></a>t-f5-04 | Ladder engine — **budget ~750 lines, that number is the discipline** | `adapters/driven/context/engine.py` | TODO |
| <a id="t-f5-05"></a>t-f5-05 | Pairing test per rung | `Core/tests/unit/test_compaction.py` | TODO |
| <a id="t-f5-06"></a>t-f5-06 | Cost-flatness test over 200 simulated turns | `Core/tests/unit/test_compaction.py` | TODO |

> The trap: compacting often costs **more** than it saves, because every pass rewrites the
> prompt prefix and breaks the provider cache. Hermes ships per-exchange compaction off by
> default for this reason. High trigger, aggressive target, never per turn.

---

## F6 · Skills and MCP

Done when the agent solves a task by reading a skill that was not in its prompt, and an
MCP tool denied by policy is recorded exactly like a local one.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f6-01"></a>t-f6-01 | `SkillRegistry` protocol; index is metadata only | `ports/skill_registry.py` | TODO |
| <a id="t-f6-02"></a>t-f6-02 | Filesystem registry; cache by `(path, mtime)`; no path traversal | `adapters/driven/skills_fs/registry.py` | TODO |
| <a id="t-f6-03"></a>t-f6-03 | `MCPToolset` composed into `ToolProvider`; name prefixing | `adapters/driven/mcp/toolsets.py` | TODO |
| <a id="t-f6-04"></a>t-f6-04 | Untrusted-content wrapper + reduced budget for `mcp_*` | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f6-05"></a>t-f6-05 | MCP schema cache so policy filtering never spawns a server | `adapters/driven/mcp/toolsets.py` | TODO |
| <a id="t-f6-06"></a>t-f6-06 | Hostile-server shadowing test | `Core/tests/unit/test_policy.py` | TODO |

---

## F7 · Multimodal input and evidence

Done when the agent asks for a photo, the turn waits, the user uploads from another
device, and the turn resumes with the image as the tool result.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f7-01"></a>t-f7-01 | Media domain types; `MediaPolicy.accepts()` — empty means **nothing** | `domain/media.py` | TODO |
| <a id="t-f7-02"></a>t-f7-02 | `MediaStore` protocol | `ports/media_store.py` | TODO |
| <a id="t-f7-03"></a>t-f7-03 | `IngestMedia`; size first, then sniff, then accept | `application/ingest_media.py` | TODO |
| <a id="t-f7-04"></a>t-f7-04 | Content-addressed filesystem store | `adapters/driven/media_fs/store.py` | TODO |
| <a id="t-f7-05"></a>t-f7-05 | `request_evidence` as an externally-executed deferred tool | `adapters/driven/tools/delivery/tools.py` | TODO |
| <a id="t-f7-06"></a>t-f7-06 | **Write down the bytes-vs-signed-URL decision** in `docs/DECISIONS.md` | `docs/DECISIONS.md` | TODO |

> Verified: given `ImageUrl`/`AudioUrl`, Pydantic AI sends the URL to the provider, which
> downloads the file. For evidence with personal data that is a disclosure. Default is
> bytes.

---

## D2 · Multi-tenant and voice out

Done when two tenants with different budgets run in parallel, one is cut off by the proxy,
and the only code change was the adapter's `base_url`.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-d2-01"></a>t-d2-01 | LiteLLM proxy mode; third database, same instance | `adapters/driven/llm_litellm/gateway.py` | TODO |
| <a id="t-d2-02"></a>t-d2-02 | `SpeechPort` synthesis, gated by profile flag | `ports/speech_port.py` | TODO |
| <a id="t-d2-03"></a>t-d2-03 | Tenant dimension in policy rules | `adapters/driven/persistence_pg/policy_repository.py` | TODO |

---

## Later — not scheduled

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-later-01"></a>t-later-01 | Scheduled turns; service identity, fresh session per run | `adapters/driving/scheduler/cron.py` | TODO |
| <a id="t-later-02"></a>t-later-02 | Fraud vertical — the real proof the core did not change | `adapters/driven/tools/fraud/tools.py` | TODO |
| <a id="t-later-03"></a>t-later-03 | `classify_error` — five strategies, only after a second provider is live | `ports/model_gateway.py` | TODO |

---

## Rules for whoever picks this up

1. **Read `docs/DECISIONS.md` before changing a design choice.** Each entry carries the
   evidence that produced it. Overturn one deliberately, with a new entry — never by
   accident.
2. **Do not weaken `test_contract.py` to make a change pass.** If it fails, the port cut
   is wrong. Fix the design.
3. **The silent-bug table in `CLAUDE.md` is not advisory.** None of those five areas fails
   a test. Verify them by hand.
4. **Budgets in this file are discipline, not estimates.** The compaction adapter is
   budgeted at ~750 lines against Hermes' 11,003. Exceeding it means scope crept.
5. **Update `Status` here when you close a task**, and the `Status:` header in the file
   itself. This index is the only shared state between agents.


---

## F8 · Knowledge: Skills and `FULL_TEXT`

Done when an administrator changes a price through the admin endpoint and the very next
turn quotes the new price, with the change auditable — and when a second agent whose
profile does not list that collection cannot retrieve from it.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f8-01"></a>t-f8-01 | Knowledge domain types; `KnowledgePolicy.can_read()` — empty means **nothing** | `domain/knowledge.py` | TODO |
| <a id="t-f8-02"></a>t-f8-02 | `KnowledgeBase` protocol — **read only, no write method exists** | `ports/knowledge_base.py` | TODO |
| <a id="t-f8-03"></a>t-f8-03 | `KnowledgeAdmin` protocol; `AdminIdentity` is a distinct type | `ports/knowledge_admin.py` | TODO |
| <a id="t-f8-04"></a>t-f8-04 | Postgres adapter, `FULL_TEXT` only; versioned docs with `effective_from` | `adapters/driven/knowledge_pg/` | TODO |
| <a id="t-f8-05"></a>t-f8-05 | `knowledge_search` tool + untrusted-content wrapping of excerpts | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f8-06"></a>t-f8-06 | Admin HTTP routes, separate from the chat surface | `adapters/driving/http/admin_routes.py` | TODO |
| <a id="t-f8-07"></a>t-f8-07 | Test: a chat caller cannot reach any write path | `Core/tests/unit/test_knowledge.py` | TODO |
| <a id="t-f8-08"></a>t-f8-08 | Test: cross-collection and cross-tenant retrieval both return empty | `Core/tests/unit/test_knowledge.py` | TODO |

> `SEMANTIC` and `HYBRID` are **not** implemented in F8. Enabling them later is one config
> line per collection plus pgvector in the existing instance.

---

## F9 · Agent-to-agent foundations

Done when a customer-service agent asks a personal-assistant agent a question, that agent
suspends to ask its human, and the answer returns to the first agent — with a hop limit
enforced and the answer wrapped as untrusted content.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f9-01"></a>t-f9-01 | Peer domain types; `PeerPolicy.may_ask()` — empty means nobody | `domain/peers.py` | TODO |
| <a id="t-f9-02"></a>t-f9-02 | `AgentMailbox` protocol, A2A-shaped | `ports/agent_mailbox.py` | TODO |
| <a id="t-f9-03"></a>t-f9-03 | Durable queue adapter | `adapters/driven/peers/` | TODO |
| <a id="t-f9-04"></a>t-f9-04 | `ask_peer` as an externally-executed deferred tool | `adapters/driven/tools/` | TODO |
| <a id="t-f9-05"></a>t-f9-05 | Hop limit + two-sided allowlist check | `adapters/driven/peers/` | TODO |
| <a id="t-f9-06"></a>t-f9-06 | **Communicable suspension**: tell the user before suspending on a peer | `application/start_turn.py` | TODO |
| <a id="t-f9-07"></a>t-f9-07 | Test: A→B→A is refused at the hop limit | `Core/tests/unit/test_peers.py` | TODO |

---

## F10 · Transcript and audit API

Done when an operator can page a full conversation with tools, reasoning and pending state,
the same conversation rendered for the user shows only messages plus a pending placeholder,
and neither query can cross a tenant boundary.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f10-01"></a>t-f10-01 | Transcript domain types + the `VISIBLE_TO` table | `domain/transcript.py` | TODO |
| <a id="t-f10-02"></a>t-f10-02 | `TranscriptReader` protocol | `ports/transcript_reader.py` | TODO |
| <a id="t-f10-03"></a>t-f10-03 | Postgres projection; cursor pagination, tenant predicate in SQL | `adapters/driven/persistence_pg/transcript_repository.py` | TODO |
| <a id="t-f10-04"></a>t-f10-04 | Persist reasoning blocks (admin-only) in `ConversationStore` | `adapters/driven/persistence_pg/conversation_repository.py` | TODO |
| <a id="t-f10-05"></a>t-f10-05 | **Decide the reasoning retention window** and record it in `docs/DECISIONS.md` | `docs/DECISIONS.md` | TODO |
| <a id="t-f10-06"></a>t-f10-06 | The four read endpoints | `adapters/driving/http/transcript_routes.py` | TODO |
| <a id="t-f10-07"></a>t-f10-07 | Test: `VISIBLE_TO` covers every `EntryKind` | `Core/tests/unit/test_transcript.py` | TODO |
| <a id="t-f10-08"></a>t-f10-08 | Test: the user projection never leaks a tool name | `Core/tests/unit/test_transcript.py` | TODO |
| <a id="t-f10-09"></a>t-f10-09 | Test: a transcript query cannot cross a tenant | `Core/tests/unit/test_transcript.py` | TODO |

---

## D2 additions

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-d2-04"></a>t-d2-04 | Full A2A wire protocol as an adapter swap | `adapters/driven/peers/` | TODO |
| <a id="t-d2-05"></a>t-d2-05 | `SEMANTIC` retrieval via pgvector, per collection | `adapters/driven/knowledge_pg/` | TODO |


---

## F1 additions — profile versioning (D20)

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f1-17"></a>t-f1-17 | `AgentProfile.version`; monotonic per id, bumped on YAML change | `domain/profile.py` | TODO |
| <a id="t-f1-18"></a>t-f1-18 | Persist `profile_version` + resolved snapshot on every turn | `adapters/driven/persistence_pg/conversation_repository.py` | TODO |
| <a id="t-f1-19"></a>t-f1-19 | Test: a turn's audit record reproduces the persona and rules in force | `Core/tests/unit/test_profile_versioning.py` | TODO |

## F2 additions — concurrency and coalescing (D19)

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f2-03"></a>t-f2-03 | Partitioned queue: `partition_concurrency=1`, key = session id | `adapters/driving/workflow/turn_workflow.py` | TODO |
| <a id="t-f2-04"></a>t-f2-04 | Pending-input buffer table + drain as the workflow's first step | `adapters/driven/persistence_pg/` | TODO |
| <a id="t-f2-05"></a>t-f2-05 | Coalescing enqueue: `delay_seconds` + `deduplication_id` / `return-existing` | `adapters/driving/http/routes.py` | TODO |
| <a id="t-f2-06"></a>t-f2-06 | Typing-indicator extension of the window, where the channel supplies it | `adapters/driven/human/gateway.py` | TODO |
| <a id="t-f2-07"></a>t-f2-07 | Test: three messages inside the window produce **one** turn and one model call | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f2-08"></a>t-f2-08 | Test: two simultaneous sessions run in parallel; two messages on one session do not | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f2-09"></a>t-f2-09 | Test: a message arriving mid-turn becomes a follow-up turn, not a lost message | `Core/tests/integration/test_durability.py` | TODO |
