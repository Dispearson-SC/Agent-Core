# Where this build stands

A running handover. `docs/TASKS.md` says what is left; this file says what is *true right
now* — what works, what cannot be run, what infrastructure exists, and what a fresh session
needs to know before touching anything.

Last updated: 2026-09-10, after wave 10.

## Verified state

```
370 passed, 14 skipped        ruff: clean
mypy strict: 156 source files  imports: 84 modules, 0 failed
44 anchors still TODO (of 91)
```

Nothing has been committed. Every change lives in the working tree.

## What actually works

A turn runs end to end below the HTTP route, against real infrastructure — real MiniMax M3
through LiteLLM, real Postgres. The model emits a structured tool call, the tool executes,
the audit row lands with its `rule_id`, and the model's second turn reflects the tool's
result with the provider's `tool_call_id` round-tripped. That is `test_f0_end_to_end.py`,
and it drives the production path: no injected model factory, no test double for the model.

Also landed and tested: the DBOS turn workflow with a per-session partitioned queue; the
knowledge repository with its tenant predicate in the SQL; the durable peer mailbox; the
transcript repository; the content-addressed media store; the filesystem skill registry with
path containment; the compaction ladder; MCP toolsets driven by a real stdio fixture server;
`ResumeTurn`; `DecideApproval`; proxy mode with per-tenant spend separation.

## What cannot be run

**There is no entry point.** No `main`, no ASGI app, no process to start. This is a library
with tests.

**`build_container()` cannot produce a working `start_turn`.** Two gaps, and neither has an
anchor — which is why they are written down here:

- **`ToolProvider` has no adapter.** `adapters/driven/tools/delivery/tools.py` exists, but
  nothing implements the port and hands a toolset to the runner. `test_f0_end_to_end.py`
  uses `_DeliveryToolProvider`, which its own docstring calls "a TEST double, not an adapter".
- **`PgConversationStore.append_outcome` still raises.** `StartTurn` step 7 awaits it on
  every turn, finished or suspended. `t-f1-13` is marked DONE and is not: that anchor's
  assertion covered `load_history` only, and the anchor is wider than the assertion it was
  given. Third instance of that class — see the `t-f1-04` and `t-f1-12` notes in `TASKS.md`.

`composition.py` refuses to wire a half-built use case, which is correct: a container that
builds and dies on the first turn is the bug that file exists to prevent. Note its "missing
seats" comment is now **stale** — it lists `ContextEngine` and `SkillRegistry` as
docstring-only, and both are implemented (704 and 299 lines). `ToolProvider` is the only
genuinely empty seat.

**Nothing reaches the user.** `POST /turns` answers 202, there is no GET route, and outbound
delivery (`_step_deliver`, `t-f3-07`) is the first item of wave 11. A turn executes and the
answer goes nowhere.

**Durability is unproven.** `t-f2-02` — kill the process mid-turn, restart, and the turn
completes without re-running the tool that already ran — has not been written. The workflow
exists; surviving a crash is exactly what is not demonstrated.

## The shortest path to something operable

Five items, roughly one focused wave. None are hard; none were on the critical path of
earlier waves:

1. A `ToolProvider` adapter (needs an anchor).
2. `PgConversationStore.append_outcome` (re-open `t-f1-13`).
3. `t-f3-07` `_step_deliver` — the return trip.
4. A process entry point (needs an anchor).
5. Verify Telegram live; `t-f3-09` is currently marked payload-mapping-only because it was
   written before the token existed.

After that, this answers a Telegram message end to end.

## Infrastructure — all verified, credentials in the gitignored `.env`

| What | Status | Env |
|---|---|---|
| MiniMax chat | `minimax/MiniMax-M3`, tool calling | `MINIMAX_API` (LiteLLM wants `MINIMAX_API_KEY` — map it) |
| MiniMax speech | `minimax/speech-2.6-turbo`, real audio | same key |
| Gemini | `gemini/gemini-3-flash-preview`, tool calling | `GEMINI_API_KEY` |
| Telegram | `@agentcoresc_bot` | `TELEGRAM_BOT` |
| LiteLLM proxy | configured; `MiniMax-M3` + `MiniMax-Speech` granted to two teams; per-team spend separation verified | `LITELLM_PROXY_URL`, `LITELLM_KEY_TENANT_A`, `LITELLM_KEY_TENANT_B` |
| Postgres | 17.11, superuser, **pgvector 0.8.6 available** | `AGENT_CORE_DATABASE_URL`, `AGENT_CORE_TEST_ADMIN_DATABASE_URL` |

Two database URLs exist because they are used differently. `CREATE DATABASE` cannot run
inside a transaction, so migrations connect to the admin URL (the `postgres` database) to
create `agent_core_app` and `agent_core_app_dbos`. The app URL points at `agent_core_app`,
which `ensure_databases()` creates on first run.

A portable PostgreSQL 17.2 cluster also runs from the session scratchpad for offline work —
see `docs/FIELD-NOTES.md`, and start it with `pg_ctl -W`, never `-w`.

**Still blocked, and not by code:** WhatsApp (`t-f3-08`) needs a WhatsApp Business token and
a public HTTPS webhook; the user has deferred it. `t-f2-06` has no reachable subject in phase
one at all — it needs inbound typing state, which D22 established only Chatwoot's web widget
supplies, and D23 put Chatwoot in phase two.

## What the guard tests caught

Five real defects, all the same class: a boundary that looks closed and is not. This is the
argument for writing the guards before the phase that needs them, not after.

| Defect | How it was found |
|---|---|
| `domain/profile.py` imported `yaml` | the layer contract test, on its first real run — and the task itself had prescribed it |
| `KnowledgeBase` mandated a tenant filter it had no way to receive | the read-only port lock, while asserting something else |
| `AdminIdentity` carried no tenant, so an admin granted `pricing` held every tenant's `pricing` — on a **write** path | checking whether the read-side hole had a mirror |
| The transcript visibility guard could never fail (`ADMIN = frozenset(EntryKind)`), and had wrongly exposed `PENDING_PLACEHOLDER` | an agent noticing its own test was unfalsifiable |
| `api_key=None` on an OpenAI-compatible client sends `OPENAI_API_KEY` to whatever provider you pointed it at | fixing the mirror-image `base_url=None` bug |

## Open, unowned, needs anchors

1. `dbos.Queue` defaults to `polling_interval_sec=1.0` — up to a second of latency on every
   chat reply, left at the default, and it interacts with `t-f2-05`'s coalescing window.
2. `domain/compaction.py::climb_ladder` takes a sync callable, but rungs L3/L4 must await a
   summariser, so the adapter grew a `climb_ladder_async` twin. Two ladders in a silent-bug
   area can diverge and only the bill would say so.
3. No anchor for the Postgres `KnowledgeAdmin` adapter, though `ARCHITECTURE.md` says
   `knowledge_pg/` implements it and `t-f8-06` will need it.
4. Reasoning has no port-level reader *or* producer.
5. `test_coalescing.py::test_one_turn_at_a_time_per_session_while_sessions_run_in_parallel`
   failed twice inside full-suite runs under sixteen-way agent load and passes 5/5 alone.
   Not fixed, not dismissed. A concurrency guarantee proved by a test that only fails under
   load is not proved, and it trains people to re-run until green.
6. F0's done criterion is stale wording: `POST /turns` answers 202 and can never "return a
   model response". Suggested: *a turn started by POST produces a model response that used
   the tool, and the `tool_call` — with its `rule_id` — is in the audit log*.

## Next wave

Wave 11, writes verified disjoint: `t-f2-05` routes.py, `t-f3-07` turn_workflow.py,
`t-f3-05` decide_approval.py, `t-f5-05` test_compaction.py, `t-f6-04` runner.py, `t-f6-05`
mcp/toolsets.py, `t-f8-06` admin_routes.py, `t-f9-04` tools/peers.py, `t-f9-05`
peers/hop_limit.py, `t-f10-06` transcript_routes.py, `t-f7-05` tools/evidence.py, `t-d2-06`
domain/policy.py, `t-later-01` scheduler/cron.py, `t-f4-03` test_contract.py.

`t-d2-03` waits for `t-d2-06`: `RuleSet` must carry the tenant before the SQL predicate means
anything.

Measured pace: waves of 16–23 agents take 40–65 minutes, and each wave has produced a
follow-up round of 15–20 minutes fixing what it found. Roughly four to five hours of wall
clock remain for everything reachable.
