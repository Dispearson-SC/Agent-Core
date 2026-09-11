# Where this build stands

A running handover. `docs/TASKS.md` says what is left; this file says what is *true right
now* — what works, what cannot be run, what infrastructure exists, and what a fresh session
needs to know before touching anything.

Last updated: 2026-09-10, at the wave-11 barrier. **Wave 12 is in flight as this is
written**, so treat the anchor counts below as a floor, not a total.

## Verified state

```
438 passed, 14 skipped, 2 xfailed   ruff: clean
mypy strict: 176 source files        imports: 91 modules, 0 failed
```

Committed through wave 10 on branch `feat/f0-f10-core-implementation`, in six work-unit
commits sliced by architectural layer. Wave 11 and the barrier reconciliation are in the
working tree, uncommitted.

## What actually works

A turn runs end to end below the HTTP route, against real infrastructure — real MiniMax M3
through LiteLLM, real Postgres. The model emits a structured tool call, the tool executes,
the audit row lands with its `rule_id`, and the model's second turn reflects the tool's
result with the provider's `tool_call_id` round-tripped. That is `test_f0_end_to_end.py`,
and it drives the production path: no injected model factory, no test double for the model.

**`build_container()` now produces a working `start_turn`.** Wave 11 closed the two seats
that were blocking it: the `ToolProvider` adapter (`t-f1-21`) and
`PgConversationStore.append_outcome` (`t-f1-22`). The barrier verified that claim by
AST-walking every method a first turn reaches, rather than trusting the report.

**A finished turn now leaves the building.** `_step_deliver` (`t-f3-07`) dispatches through
the channel registry, the `http` channel is registered as a deliberate pull-mode no-op
(D23/GAPS A4: the answer is stored for a later `GET`), and `build_container` binds the DBOS
workflow to its collaborators — which nothing had ever done.

**Telegram is verified live**, not assumed: `getMe` against the real Bot API plus the
mapping asserted against a real `getUpdates` payload. The webhook path stays unverified —
no public HTTPS here — and the docstring says so rather than implying otherwise.

Also landed and tested: the DBOS turn workflow with a per-session partitioned queue; the
knowledge repository with its tenant predicate in the SQL, plus the admin adapter nobody
had owned; the durable peer mailbox and the hop limit; the transcript repository and its
four read endpoints; the admin HTTP surface; the content-addressed media store; the
filesystem skill registry with path containment; the compaction ladder; MCP toolsets with a
schema cache; `request_evidence` and `ask_peer` as shared deferred tools; `ResumeTurn`;
`DecideApproval` with the four-eyes rule (D25); proxy mode with per-tenant spend separation.

## What cannot be run

**There is still no entry point.** No `main`, no ASGI app, no process to start. This is a
library with tests. `t-f0-05` now exists as an anchor — that it did not is why the gap
survived ten waves — and it is in flight in wave 12.

**Durability is unproven.** `t-f2-02` — kill the process mid-turn, restart, and the turn
completes without re-running the tool that already ran — has not been written. The workflow
exists; surviving a crash is exactly what is not demonstrated.

**Coalescing is blocked, and not by us.** See below.

## The two findings that outrank everything else here

**1. D19 prescribes a shape dbos 2.31.1 does not have.** A queue cannot be partitioned AND
deduplicated: `Queue._validate_enqueue` refuses the pair, and `Debouncer` refuses it saying
so. `docs/FIELD-NOTES.md` carries the full evidence in a **RETRACTED** section, because that
file previously claimed the opposite and a wave was scheduled on the claim. The rule it
earned: *a signature check proves a parameter exists; only running it proves two of them
compose.* `t-f2-05` is BLOCKED and split; `t-f2-11` is the half that can actually be built.

**2. Half the compaction ladder has been inert the whole time.** `LadderContextEngine`
takes a `Summariser` **optionally**, no implementation exists anywhere in the tree, and
production is wired with `None` — so rungs L3 and L4 free nothing on every turn. Every test
passes. Nothing warns. This is `CLAUDE.md`'s silent-bug table happening in front of us:
*"compaction strategy — only on the bill"*. An optional seat with no adapter is not a seat,
it is a gap with a default. It is `t-f5-10`, and `t-f5-09`'s measurement is meaningless
until it lands.

## Infrastructure — all verified, credentials in the gitignored `.env`

| What | Status | Env |
|---|---|---|
| MiniMax chat | `minimax/MiniMax-M3`, tool calling | `MINIMAX_API` (LiteLLM wants `MINIMAX_API_KEY` — map it) |
| MiniMax speech | `minimax/speech-2.6-turbo`, real audio | same key |
| Gemini | `gemini/gemini-3-flash-preview`, tool calling | `GEMINI_API_KEY` |
| Telegram | `@agentcoresc_bot`, **verified live** | `TELEGRAM_BOT` |
| LiteLLM proxy | configured; `MiniMax-M3` + `MiniMax-Speech` granted to two teams; per-team spend separation verified | `LITELLM_PROXY_URL`, `LITELLM_KEY_TENANT_A`, `LITELLM_KEY_TENANT_B` |
| Postgres (local, **what the suite runs against**) | portable EDB 17 in the scratchpad, superuser, **NO pgvector** | `postgresql://postgres:postgres@localhost:5432` |
| Postgres (remote, Coolify) | 17.11 Debian, **pgvector 0.8.6 available, not yet installed** | `AGENT_CORE_DATABASE_URL`, `AGENT_CORE_TEST_ADMIN_DATABASE_URL` |

Two database URLs exist because they are used differently. `CREATE DATABASE` cannot run
inside a transaction, so migrations connect to the admin URL (the `postgres` database) to
create `agent_core_app` and `agent_core_app_dbos`. The app URL points at `agent_core_app`,
which `ensure_databases()` creates on first run.

A portable PostgreSQL 17 cluster runs from the session scratchpad for offline work — see
`docs/FIELD-NOTES.md`, and start it with `pg_ctl -W`, never `-w`.

**THERE ARE TWO DATABASES AND THEY DO NOT HAVE THE SAME CAPABILITIES.** This file previously
said "Postgres 17, pgvector 0.8.6 available" as if that were one row. It is not: pgvector
lives on the REMOTE Coolify instance, and the integration suite runs against the LOCAL
portable cluster, which has no `vector.control` at all. That conflation was copied into a
wave prompt and `t-d2-05` built a `CREATE EXTENSION vector` migration against the cluster
that cannot have it — which then broke STARTUP for every other anchor, because migration
discovery applies every sibling module it finds.

Two things follow. A capability belongs to a *named* database, never to "Postgres". And a
migration that can fail on a valid deployment target must not be able to take the whole
startup path down with it — `SEMANTIC` is opt-in per collection by F8's own words, so its
schema has to be opt-in too.

**Still blocked, and not by code:** WhatsApp (`t-f3-08`) needs a WhatsApp Business token and
a public HTTPS webhook; the user has deferred it. `t-f2-06` has no reachable subject in phase
one at all — it needs inbound typing state, which D22 established only Chatwoot's web widget
supplies, and D23 put Chatwoot in phase two.

## What the guard tests caught

Six real defects, all the same class: a boundary that looks closed and is not. This is the
argument for writing the guards before the phase that needs them, not after.

| Defect | How it was found |
|---|---|
| `domain/profile.py` imported `yaml` | the layer contract test, on its first real run — and the task itself had prescribed it |
| `KnowledgeBase` mandated a tenant filter it had no way to receive | the read-only port lock, while asserting something else |
| `AdminIdentity` carried no tenant, so an admin granted `pricing` held every tenant's `pricing` — on a **write** path | checking whether the read-side hole had a mirror |
| The transcript visibility guard could never fail (`ADMIN = frozenset(EntryKind)`), and had wrongly exposed `PENDING_PLACEHOLDER` | an agent noticing its own test was unfalsifiable |
| `api_key=None` on an OpenAI-compatible client sends `OPENAI_API_KEY` to whatever provider you pointed it at | fixing the mirror-image `base_url=None` bug |
| `RuleSet` recorded roles and channel but not the tenant it was loaded for | asking whether the SQL predicate alone could be safe — it could not (`t-d2-06`) |

## The recurring process defect, now four times

**The anchor is wider than the assertion the agent was given.** `t-f1-04`, `t-f1-12`,
`t-f1-13` and — differently — `t-f5-10`. Each time an agent satisfied exactly what it was
asked and the anchor claimed more. The fix is never to re-open the anchor and hope; it is to
**split it so a criterion and an assertion are the same size**. `t-f1-20`, `t-f1-22`,
`t-f2-10` and `t-f2-11` all exist for that reason.

The mirror image also happened: a **frozen port with no adapter is not a finished port**.
`t-f1-07` froze `ToolProvider` and no anchor ever claimed the adapter, so a test double
stood in for it for ten waves — the same way an injected model factory hid `t-f0-04`'s
missing endpoint.

## Open, unowned, needs anchors

1. `dbos.Queue` defaults to `polling_interval_sec=1.0` — up to a second of latency on every
   chat reply, left at the default. **This is also why the old coalescing test was flaky**:
   it was measuring scheduler jitter and calling it parallelism. The test is fixed; the
   latency decision is still nobody's.
2. `domain/compaction.py::climb_ladder` takes a sync callable, but rungs L3/L4 must await a
   summariser, so the adapter grew a `climb_ladder_async` twin. Two ladders in a silent-bug
   area can diverge and only the bill would say so.
3. Reasoning has no port-level reader *or* producer.
4. F0's done criterion is stale wording: `POST /turns` answers 202 and can never "return a
   model response". Suggested: *a turn started by POST produces a model response that used
   the tool, and the `tool_call` — with its `rule_id` — is in the audit log*.
5. `test_channel_wiring.py::test_an_unknown_channel_id_raises_...` has a stale docstring
   ("nothing is registered") now that `http` is always registered. Every assertion in it
   still holds; only the prose lies.

## Measured pace

Waves of 14–16 agents take 25–45 minutes, and each has produced a follow-up barrier round of
10–20 minutes fixing collateral between agents. The collateral is never a logic bug — it is
always a contract that moved under a test asserting the old state, which is exactly what the
barrier exists to catch.
