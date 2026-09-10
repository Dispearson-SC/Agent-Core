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

Two qualified variants appear in F0 and F1, and the qualifier is the point — an
unqualified `DONE` claims the task was proved, so a task that was not gets to say so:

- **DONE — proof pending.** The code and its test are written, typed and linted, but the
  test cannot execute here for want of infrastructure, so the behaviour has not been
  observed. The qualifier stays until it runs. No F0 or F1 anchor carries this today:
  the four Postgres-backed anchors were run against a real PostgreSQL 17 and passed.
- **FROZEN — contract already correct, test is a regression lock.** The anchor described
  a change that turned out to be already in place, so there was no red step to take. The
  test was written anyway and proved to have teeth against a deliberately wrong stub; it
  now exists to catch a future regression rather than to drive an implementation.
- **MEASURED — closed by an observation, not by a green test.** The criterion is a
  quantity in the real world, so no test can close it: a fake model has no bill and a
  simulated conversation has no provider cache. The anchor closes when the number is
  observed against the real provider and written down with its date, its traffic and its
  model. A test may stand next to it as a proxy, but the proxy never carries this status —
  it is `DONE` like any other test, and the measurement is the closing evidence.

---

## F0 · Executable skeleton

Done when a POST returns a model response that used the tool, with the `tool_call` in the log.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f0-00"></a>t-f0-00 | **GATE — D23 resolved: Agent-Core owns channel adapters in phase one, Chatwoot deferred.** No longer blocks F0. | `docs/DECISIONS.md#d23` | **DONE** |
| <a id="t-f0-01"></a>t-f0-01 | Fake ports for `AgentRunner` and `ToolProvider` — build the fakes before the adapters | `Core/tests/fakes/ports.py` | **DONE** |
| <a id="t-f0-02"></a>t-f0-02 | Composition root; one place that imports concrete adapters | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f0-03"></a>t-f0-03 | `POST /turns` returning 202 immediately, never blocking | `adapters/driving/http/routes.py` | **DONE** |
| <a id="t-f0-04"></a>t-f0-04 | `ModelGateway` over LiteLLM in library mode (`base_url()` returns None), **plus the day-1 `Model` that endpoint-less mode needs** | `adapters/driven/llm_litellm/` | **DONE** |

> **`t-f1-12`'s F1 share is done; the anchor is wider than one phase and stays open.**
> `PolicyEnforcement` is implemented and pinned (on DENY the audit row is written before
> `SkipToolExecution` is raised, the tool body never runs, and the refusal reaches the
> model), and `PydanticAgentRunner` now builds a cached agent per profile, attaches that
> hook per turn, runs the tool loop to completion and returns a FINISHED `TurnOutcome`.
> `tests/unit/test_runner.py` pins the wiring; `tests/integration/test_f0_end_to_end.py`
> drives it against the real provider and the real audit table, and the borrowed tool loop
> that used to live in that test body is gone.
>
> What is left in the file belongs to later phases and says so in code: suspension and
> `resume` (F3), compaction wiring (F5), the untrusted-result hook and MCP (F6), media
> (F7), peers (F9).
>
> Two gaps surfaced while implementing it. **Both are now closed** — see the two
> corrections below. They were recorded here first, unfixed, which is what made them
> fixable deliberately rather than by whoever tripped over them next:
>
> - **`AgentRunner.run` had no `turn_id` seat.** `resume` took one, `run` did not — yet
>   `TurnOutcome` requires one and every audit row is filed under one.
> - **Day 1 had no model endpoint.** Pydantic AI's `LiteLLMProvider` is an
>   OpenAI-compatible HTTP client and needs a base URL; library mode has none, and a client
>   built with `base_url=None` silently targets api.openai.com — a MiniMax profile would
>   reach OpenAI. The default factory refused instead, so only a test injecting its own
>   factory could reach a provider.
>
> `composition.py` therefore still reports `start_turn` PENDING, now for three named seats
> rather than four: `ToolProvider` (`t-f1-07` / F4), `ContextEngine` (F5), `SkillRegistry`
> (F6) — plus `PgConversationStore.append_outcome`, still pending under `t-f1-13`.
>
> This was mis-reported as DONE once: the implementing agent satisfied the assertion it
> was given (the DENY ordering) and the anchor covers more than that assertion. When an
> anchor spans phases, say so in the anchor.
>
> **Who writes `runner.py`.** The docstring names four phases; the anchors named three,
> and the three parts nobody owned were exactly the ones a wave would have had to invent.
> The full set, each in its owning phase:
>
> | Anchor | Phase | Its share of the file |
> |---|---|---|
> | `t-f1-12` | F1 | `PolicyEnforcement` and the runner body |
> | `t-f3-10` | F3 | the `resume()` path — a pending tool call answered from outside |
> | `t-f5-07` | F5 | `ProcessHistory` wiring, so compaction reaches the model request |
> | `t-f6-04` | F6 | untrusted-content wrapper + reduced budget for `mcp_*` |
> | `t-f7-07` | F7 | the BYTES vs SIGNED_URL branch, per `t-f7-06`'s decision |
> | `t-f8-05` | F8 | `knowledge_search` and the wrapping of its excerpts |
>
> Six anchors, one file, no two of them ever in the same wave. That is a serial spine, not
> a scheduling accident — `docs/WAVES.md` records it as one.

> **F0's done criterion is not yet proved.** The header above — a POST returning a model
> response that used the tool, with the `tool_call` in the log — needs a real model
> provider. `tests/integration/test_http_turns.py` covers the HTTP boundary only (the
> 202, the identity built before the use case, the route never awaiting the turn) and
> says so in its own docstring; it drives a fake starter. Every other F0 anchor is done
> and its tests run. Close this by supplying a provider credential and asserting the
> `tool_call` row lands in the audit table — mocking the model would only restate the
> fake test under a new name.

> Build the fake model provider first. Hermes shipped ~3,821 test files and not one
> reusable fake — every test hand-rolled a mock. Skipping this is why the extracted core
> could not be tested until one was written.

---

## F1 · Real hexagonal core

Done when a tool denied by policy does not execute, the model receives the refusal as the
tool result, and the denial is in the audit table.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f1-01"></a>t-f1-01 | Turn types; add `__post_init__` asserting `pending`/`result` exclusivity | `domain/turn.py` | **DONE** |
| <a id="t-f1-02"></a>t-f1-02 | `PolicyRule.matches()` — pin semantics with tests **before** any rule is stored | `domain/policy.py` | **DONE** |
| <a id="t-f1-03"></a>t-f1-03 | `BudgetState.wrapup_notice_due()` — fire once per turn, never per iteration | `domain/budget.py` | **DONE** |
| <a id="t-f1-04"></a>t-f1-04 | `AgentProfile.from_mapping()` — validation only, no parsing and no file read | `domain/profile.py` | **DONE** |
| <a id="t-f1-20"></a>t-f1-20 | Profile loader — reads the file and parses the YAML, hands the domain a plain mapping | `adapters/driven/profiles_fs/loader.py` | **DONE** |
| <a id="t-f1-05"></a>t-f1-05 | `AgentRunner` protocol frozen | `ports/agent_runner.py` | **FROZEN — widened once, for the `turn_id` seat `run` was missing; the test is the regression lock and moved with it** |
| <a id="t-f1-06"></a>t-f1-06 | `ToolPolicy` protocol frozen | `ports/tool_policy.py` | **DONE** |
| <a id="t-f1-07"></a>t-f1-07 | `ToolProvider` protocol frozen; **no auto-discovery** | `ports/tool_provider.py` | **DONE** |
| <a id="t-f1-08"></a>t-f1-08 | `ConversationStore` protocol frozen | `ports/conversation_store.py` | **FROZEN — contract already correct, test is a regression lock** |
| <a id="t-f1-09"></a>t-f1-09 | `AuditSink` protocol frozen | `ports/audit_sink.py` | **DONE** |
| <a id="t-f1-10"></a>t-f1-10 | `ModelGateway` protocol frozen | `ports/model_gateway.py` | **FROZEN — contract already correct, test is a regression lock** |
| <a id="t-f1-11"></a>t-f1-11 | `StartTurn` use case | `application/start_turn.py` | **DONE** |
| <a id="t-f1-12"></a>t-f1-12 | Pydantic AI adapter + the seven hooks; policy and audit in `before_tool_execute`. **F1's share only** — the file closes across F1, F3, F5, F6 and F7; the other five anchors are listed below | `adapters/driven/agent_pydantic/runner.py` | **F1 SHARE DONE — hook and runner body; F3/F5/F6/F7/F9 seams stubbed with anchors** |
| <a id="t-f1-13"></a>t-f1-13 | Postgres `ConversationStore`; index on `(session_id, seq)` | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE** |
| <a id="t-f1-14"></a>t-f1-14 | Postgres `AuditSink` on a **separate pool** | `adapters/driven/persistence_pg/audit_repository.py` | **DONE** |
| <a id="t-f1-15"></a>t-f1-15 | Postgres `ToolPolicy`; default DENY, fail closed | `adapters/driven/persistence_pg/policy_repository.py` | **DONE** |
| <a id="t-f1-16"></a>t-f1-16 | Migrations; two logical databases on one instance | `adapters/driven/persistence_pg/migrations.py` | **DONE** |

---

> **t-f1-04 was rewritten after it caused a layer violation.** As originally worded —
> "`AgentProfile` loading from YAML | `domain/profile.py`" — it put a YAML parser and a
> file read inside the one layer that may import nothing external. The F1 wave followed
> the task and introduced `import yaml` into `domain/`. `test_contract.py::test_domain_and_application_import_nothing_external`
> caught it on its first real run. Parsing now lives in `t-f1-20`, ruff bans `yaml`
> outside `adapters/`, and the domain takes an already-parsed mapping.
>
> The lesson is about this file, not about that code: a task that names a file and an
> activity the layer rules forbid together is itself the defect. Fix the task.

> **`t-f1-05` was widened after the adapter had to work around it.** The port froze `run`
> at `(request, profile, history)` while `resume` took a `turn_id`. Every `TurnOutcome`
> needs one and every audit row is filed under one, so `PydanticAgentRunner` closed the
> gap from the wrong side: a `for_turn(turn_id)` pre-binding, a `TurnNotBoundError` for
> the unbound case, and a second way to call the port that the port never described.
> `StartTurn` held the id the whole time and had nowhere to put it.
>
> `run` now takes `turn_id` first, matching `resume`. `StartTurn` passes the id it already
> has; `for_turn`, `TurnNotBoundError` and the runner's optional bound id are gone rather
> than kept as dead code — nothing needed a bound instance once the seat existed, and the
> shared agent cache they justified is simply the runner's own cache again.
>
> The regression lock moved WITH the port, which is the part worth stating. Its negative
> case in `tests/unit/test_ports_agent_runner.py` is now the *pre-widening* signature: a
> `run` that cannot be handed the identifier its own return type requires is what the lock
> rejects, so the widening cannot be quietly undone. A lock that only checks the new shape
> would have accepted the old one too.
>
> Same class as [`t-f1-04`](#t-f1-04), one layer up: a port that cannot be handed what its
> own return type requires is itself the defect. Fix the port, not the adapter.

> **`t-f0-04` grew the day-1 model endpoint it was missing.** `base_url()` returning
> `None` in library mode is correct and stays (its test pins it), but nothing could BUILD
> a model from it: `litellm_model_factory` refused, correctly, rather than let a client
> default to api.openai.com — and the consequence was that only
> `tests/integration/test_f0_end_to_end.py`, injecting its own factory, could call a
> model. Production could not make a model call at all, and the test injecting the missing
> component is exactly what hid that.
>
> `adapters/driven/llm_litellm/models.py` now owns both modes. Day 1 asks litellm to
> RESOLVE — provider, wire model name, endpoint, credential — and talks to the provider's
> own OpenAI-compatible surface; day 2 keeps the `provider/` prefix and points at the
> proxy, which is still [`t-d2-01`](#t-d2-01)'s. The loud refusal stayed for what genuinely
> cannot be served, and gained a second case: a provider with no resolvable credential,
> because an OpenAI-compatible client built with `api_key=None` reads `OPENAI_API_KEY` and
> would send it to that provider. That is the first bug's mirror image and nothing fails a
> test when it happens.
>
> The e2e no longer injects a factory — that was the acceptance signal, and if this file
> ever needs one again the gap is back. What went with the factory is named as a gap
> rather than faked: `ModelFactory` is `(model_id, base_url) -> Model` and `AgentProfile`
> has no model-settings field, so there is **no seat for a per-run model setting** such as
> `max_tokens` or `reasoning_effort` between a profile and the runner. The e2e's two-mode
> parametrization went with it; what MiniMax M3 actually does in both modes is recorded in
> `docs/FIELD-NOTES.md`, and giving profiles a model-settings seat is recorded as
> [`docs/GAPS.md` B9](GAPS.md#b9--a-profile-has-no-seat-for-a-model-setting) — named before
> anyone needs it rather than after.

> **Work carrying a phase marker but no anchor is nobody's.** A dependency sweep over the
> anchors still open found nine such items. Each was written down somewhere — an `R5` in a
> workflow docstring, two routes in a module header, five `@pytest.mark.skip` reasons — and
> none of them was in this file, so no wave could ever be scheduled to do them:
>
> | Where it was written | Now owned by |
> |---|---|
> | `turn_workflow.py` R5, `_step_compact` | [`t-f5-08`](#t-f5-08) |
> | `routes.py` header, `POST /decisions/{corr_id}` | [`t-f3-11`](#t-f3-11) |
> | `routes.py` header, `POST /evidence/{corr_id}` | [`t-f7-08`](#t-f7-08) |
> | `test_durability.py`, three tests marked F3 | [`t-f3-12`](#t-f3-12) |
> | `test_durability.py`, one test marked F7 | [`t-f7-09`](#t-f7-09) |
>
> Five tests in `test_durability.py` ship skipped. One, the F2 crash test, is owned by
> [`t-f2-02`](#t-f2-02). The other four had a phase marker and no anchor — and two of them
> are the F3 and F7 done-when criteria of this file, verbatim. A phase whose own acceptance
> criterion is an unowned skipped test cannot be finished; it can only be declared finished.
>
> This is exactly the blind spot `docs/WAVES.md` §"Wave 7 — the guard tests" was written
> about, and it recurred because that section's fix — *sweep for phase-marked skips before
> declaring a phase done* — is a sweep at the end. It found three F1 skips after the phase
> was written. The anchors above are the same sweep run at the front, before F2 through F7
> are scheduled instead of after. **A phase marker in the code is a claim on this file.
> Either it has an anchor here or it is not work, and `pytest -rs` is not the only place to
> look — a docstring `R`-rule and a route listed in a module header are the same claim.**

---

## F2 · Durability

Done when the process is killed mid-turn and, on restart, the turn completes **without
re-running** the tool that already ran.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f2-01"></a>t-f2-01 | DBOS workflow as a **driving adapter**; R1–R5 in the file docstring | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f2-02"></a>t-f2-02 | Crash-injection test | `Core/tests/integration/test_durability.py` | TODO |

> **Signatures verified — see `docs/FIELD-NOTES.md`.** Against dbos 2.31.1: `recv` and
> `send` exist with async counterparts (`recv_async`, `send_async`, `start_workflow_async`,
> `run_step_async`), which are the ones this async system must call. Three findings that
> change the work below: `recv` defaults to a 60-second timeout, which is a trap for F3's
> durable human wait; `send` already takes an `idempotency_key`; and `DBOS.step` has
> retries OFF by default.
>
> **`t-f2-03` and `t-f2-05` are largely configuration, not construction.** `dbos.Queue`
> takes `partition_concurrency` and a per-enqueue `queue_partition_key`, and
> `EnqueueOptions` already carries `delay_seconds`, `deduplication_id` and
> `duplication_policy="return-existing"` by those exact names. `Debouncer.debounce_async`
> takes a `debounce_key` and a `debounce_period_sec` window — which is `t-f2-07`'s
> assertion directly. Read the field notes before hand-rolling either.

---

## F3 · Deferred human interaction

Done when a turn waits for approval, the service is redeployed, and approving 24h later
resumes it.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f3-01"></a>t-f3-01 | `HumanGateway` protocol; publish + correlate, **no waiting** | `ports/human_gateway.py` | **DONE** |
| <a id="t-f3-02"></a>t-f3-02 | `ResumeTurn`; idempotent per `(turn_id, tool_call_id)` | `application/resume_turn.py` | **DONE** |
| <a id="t-f3-03"></a>t-f3-03 | `DecideApproval`; record before signalling | `application/decide_approval.py` | **DONE** |
| <a id="t-f3-04"></a>t-f3-04 | Channel adapter + correlation table with unguessable ids; migration `0010` lives here | `adapters/driven/human/gateway.py` | **DONE** |
| <a id="t-f3-05"></a>t-f3-05 | Decide four-eyes: must the approver differ from the requester? Write the answer down | `application/decide_approval.py` | TODO |
| <a id="t-f3-06"></a>t-f3-06 | **Wiring only**: populate `t-f3-13`'s registry with the concrete channels, shared by `ChannelHumanGateway` and turn delivery (D23). Lands after `t-f3-08` and `t-f3-09` | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f3-07"></a>t-f3-07 | `_step_deliver` — generic outbound-delivery step for a finished `TurnResult`, dispatched through the channel registry; no new port (D23) | `adapters/driving/workflow/turn_workflow.py` | TODO |
| <a id="t-f3-08"></a>t-f3-08 | WhatsApp Cloud API webhook adapter — inbound POST and outbound send, registered in the channel registry | `adapters/driving/channels/whatsapp.py` | **DONE — payload mapping only, not verified live (no WABA token and no public webhook)** |
| <a id="t-f3-09"></a>t-f3-09 | Telegram Bot API webhook adapter — inbound webhook (or `getUpdates`) and outbound send, registered in the channel registry | `adapters/driving/channels/telegram.py` | **DONE — payload mapping only, not verified live (written before the bot token arrived)** |
| <a id="t-f3-10"></a>t-f3-10 | `PydanticAgentRunner.resume()` — feed an outside answer back as the pending tool's result; `tool_call_id` verbatim | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f3-11"></a>t-f3-11 | `POST /decisions/{corr_id}` — the human's approve/refuse route; resolves the correlation, never blocks | `adapters/driving/http/routes.py` | TODO |
| <a id="t-f3-12"></a>t-f3-12 | Enable the three skipped F3 tests: approval survives a redeploy, `tool_call_id` round-trips verbatim, resolving twice is a no-op | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f3-13"></a>t-f3-13 | `Channel` structural Protocol (`send`) + the registry type it keys. **Lands before `t-f3-08`, `t-f3-09` and `t-f3-06`** | `adapters/driving/channels/registry.py` | **DONE** |

> **`t-f3-06` was circular, and `t-f3-13` is the cut.** The registry was to be "wired in
> `composition.py`" by `t-f3-06`, but `t-f3-08` and `t-f3-09` register *against a shape*,
> and no anchor owned that shape. Each of the three waited on the other two: the adapters
> needed the Protocol, the wiring needed the adapters, and the Protocol was inside the
> wiring.
>
> The shape now has a file of its own and lands first. It is a **structural Protocol in the
> adapter layer, not a port**: D23 settled that Agent-Core owns its channel adapters and
> adds no port for them, `t-f3-07` says so, and `application/` never names a channel. If a
> use case ever needs to name one, it has become a port and D23 must be reopened
> deliberately — not by moving this file.

---

## F4 · First vertical — the contract test

Done when the vertical works end to end and the diff touches no file under `domain/`,
`application/` or `ports/`.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f4-01"></a>t-f4-01 | Delivery tools package | `adapters/driven/tools/delivery/tools.py` | **DONE** |
| <a id="t-f4-02"></a>t-f4-02 | `AgentProfile.requires_approval_for()`; unparseable condition must mean **approve** | `domain/profile.py` | **DONE** |
| <a id="t-f4-03"></a>t-f4-03 | Enable the contract test | `Core/tests/unit/test_contract.py` | TODO |

---

## F5 · Context compaction

Done when a 200-turn conversation still answers well, cost per turn stays flat, and every
tool call/return pair survives each compaction.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f5-01"></a>t-f5-01 | Compaction domain types; ladder L1–L4 | `domain/compaction.py` | **DONE** |
| <a id="t-f5-02"></a>t-f5-02 | `ContextEngine` protocol; `None`-window fallback is mandatory | `ports/context_engine.py` | **DONE** |
| <a id="t-f5-03"></a>t-f5-03 | `CompactContext` use case; never retry on no progress | `application/compact_context.py` | **DONE** |
| <a id="t-f5-04"></a>t-f5-04 | Ladder engine — **budget ~750 lines, that number is the discipline** | `adapters/driven/context/engine.py` | **DONE** |
| <a id="t-f5-05"></a>t-f5-05 | Pairing test per rung | `Core/tests/unit/test_compaction.py` | TODO |
| <a id="t-f5-06"></a>t-f5-06 | **Proxy test**, not the criterion: over 200 simulated turns, tokens resent per turn stay bounded and the prompt prefix is rewritten no more than the ladder's trigger allows | `Core/tests/unit/test_compaction.py` | TODO |
| <a id="t-f5-07"></a>t-f5-07 | `ProcessHistory` wiring — compaction reaches the model request through the runner, not around it | `adapters/driven/agent_pydantic/runner.py` | **DONE** |
| <a id="t-f5-08"></a>t-f5-08 | `_step_compact` — R5's step wrapping `CompactContext`; DBOS's durable lock gives one pass per session | `adapters/driving/workflow/turn_workflow.py` | TODO |
| <a id="t-f5-09"></a>t-f5-09 | **Measure** cost per turn against the provider bill over real F4 traffic; record the number, its date, its model and its traffic in `docs/FIELD-NOTES.md` | `docs/FIELD-NOTES.md` | TODO |

> The trap: compacting often costs **more** than it saves, because every pass rewrites the
> prompt prefix and breaks the provider cache. Hermes ships per-exchange compaction off by
> default for this reason. High trigger, aggressive target, never per turn.

> **`t-f5-06` was filed as a unit test and its criterion is a quantity.** "Cost per turn
> stays flat" cannot be proved by a fake model: a fake has no bill, and a simulated
> conversation has no provider cache to break — which is the exact mechanism the phase is
> worried about. A green test here would have certified nothing while looking like the
> phase's done-when.
>
> So it is two anchors. `t-f5-06` keeps the part a fake *can* prove — tokens resent per
> turn, and how often the prefix is rewritten — and closes `DONE` like any other test.
> `t-f5-09` is the closing evidence and closes **MEASURED**; see the status legend. F5's
> done-when is not satisfied until `t-f5-09` carries a number.
>
> `t-f5-09` is also the only F5 anchor that depends on F4: it needs real traffic.
> `t-f5-01`..`t-f5-06` need none, which is what `docs/WAVES.md` had wrong.

---

## F6 · Skills and MCP

Done when the agent solves a task by reading a skill that was not in its prompt, and an
MCP tool denied by policy is recorded exactly like a local one.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f6-01"></a>t-f6-01 | `SkillRegistry` protocol; index is metadata only | `ports/skill_registry.py` | **DONE** |
| <a id="t-f6-02"></a>t-f6-02 | Filesystem registry; cache by `(path, mtime)`; no path traversal | `adapters/driven/skills_fs/registry.py` | **DONE** |
| <a id="t-f6-03"></a>t-f6-03 | `MCPToolset` composed into `ToolProvider`; name prefixing | `adapters/driven/mcp/toolsets.py` | **DONE** |
| <a id="t-f6-04"></a>t-f6-04 | Untrusted-content wrapper + reduced budget for `mcp_*` | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f6-05"></a>t-f6-05 | MCP schema cache so policy filtering never spawns a server | `adapters/driven/mcp/toolsets.py` | TODO |
| <a id="t-f6-06"></a>t-f6-06 | Hostile-server shadowing test | `Core/tests/unit/test_policy.py` | TODO |

---

## F7 · Multimodal input and evidence

Done when the agent asks for a photo, the turn waits, the user uploads from another
device, and the turn resumes with the image as the tool result.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f7-01"></a>t-f7-01 | Media domain types; `MediaPolicy.accepts()` — empty means **nothing** | `domain/media.py` | **DONE** |
| <a id="t-f7-02"></a>t-f7-02 | `MediaStore` protocol | `ports/media_store.py` | **DONE** |
| <a id="t-f7-03"></a>t-f7-03 | `IngestMedia`; size first, then sniff, then accept | `application/ingest_media.py` | **DONE** |
| <a id="t-f7-04"></a>t-f7-04 | Content-addressed filesystem store | `adapters/driven/media_fs/store.py` | **DONE** |
| <a id="t-f7-05"></a>t-f7-05 | `request_evidence` as an externally-executed deferred tool — **shared, not per-vertical** | `adapters/driven/tools/evidence.py` | TODO |
| <a id="t-f7-06"></a>t-f7-06 | **Write down the bytes-vs-signed-URL decision** in `docs/DECISIONS.md` | `docs/DECISIONS.md` | TODO |
| <a id="t-f7-07"></a>t-f7-07 | The BYTES vs SIGNED_URL branch in the runner, implementing `t-f7-06`'s decision | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f7-08"></a>t-f7-08 | `POST /evidence/{corr_id}` — the upload route; size first, then sniff, then accept (`t-f7-03`) | `adapters/driving/http/routes.py` | TODO |
| <a id="t-f7-09"></a>t-f7-09 | Enable the skipped F7 test: an evidence request resumes with the uploaded image | `Core/tests/integration/test_durability.py` | TODO |

> Verified: given `ImageUrl`/`AudioUrl`, Pydantic AI sends the URL to the provider, which
> downloads the file. For evidence with personal data that is a disclosure. Default is
> bytes.

> **`request_evidence` was filed inside the delivery vertical, and it is not delivery's.**
> `adapters/driven/tools/delivery/tools.py` is a vertical's tools package — the thing the
> contract in `CLAUDE.md` says a new vertical brings *one of*. A core mechanism parked
> there is copied by every vertical that follows, and the second copy is the one that
> drifts. `request_evidence` is the deferred-tool mechanism itself, owned by D9, so it
> lives in a shared module and a vertical imports it.
>
> `t-f9-04`'s `ask_peer` had the same defect and took the same fix. Two anchors, one
> mistake: **a mechanism every vertical needs is not a vertical's file.** If a tool would
> have to be copied into the next `tools/` package to work, it is in the wrong package.

---

## D2 · Multi-tenant and voice out

Done when two tenants with different budgets run in parallel, one is cut off by the proxy,
and the only code change was the adapter's `base_url`.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-d2-01"></a>t-d2-01 | LiteLLM proxy mode; third database, same instance | `adapters/driven/llm_litellm/gateway.py` | **DONE** |
| <a id="t-d2-02"></a>t-d2-02 | `SpeechPort` synthesis, gated by profile flag | `ports/speech_port.py` | **DONE** |
| <a id="t-d2-03"></a>t-d2-03 | Tenant dimension in policy rules — **SQL predicate and migration `0012` only**; if the condition below fails, the domain half is `t-d2-06` | `adapters/driven/persistence_pg/policy_repository.py` | TODO |
| <a id="t-d2-06"></a>t-d2-06 | **TAKEN — condition checked 2026-09-10 and it fails.** Carry the tenant through `RuleSet.applicable` / `PolicyRule.matches` — `RuleSet` records no tenant, so the SQL alone cannot make it safe | `domain/policy.py` | TODO |

> **`t-d2-03` needs a file it did not name, and the file is a silent-bug area.** Adding a
> tenant dimension to policy rules touches `domain/policy.py` unless one thing is true, so
> here is the condition, written down instead of discovered mid-wave:
>
> > `t-d2-03` stays a pure SQL predicate **if and only if** tenant is already a field of the
> > identity that `load_rules(caller)` narrows on, so every rule in the returned `RuleSet`
> > is by construction this tenant's. Then the tenant never reaches `RuleSet.applicable`,
> > `PolicyRule.matches` keeps its signature, and `domain/policy.py` is untouched.
>
> If instead a `RuleSet` can ever hold rules from more than one tenant, the reducer must
> decide on it and `t-d2-06` is taken.
>
> **Condition checked 2026-09-10: it FAILS. `t-d2-06` is taken.** `CallerIdentity` does
> carry `tenant_id` (`domain/turn.py`), but `RuleSet` narrows on `subject_roles` and
> `channel` only — it does not record the tenant it was built for. So a snapshot loaded for
> tenant A is structurally capable of answering a question about tenant B, and only the
> discipline of the SQL would stop it. That is precisely the hole `t-f1-02` closed for roles
> and channel, and closing it there while leaving it open for tenant would be worse than
> never closing it, because the shape would then look safe. `domain/policy.py` already
> anticipates this: `PolicyRule` carries a `TODO(D2)` for `tenant_id`, where `None` means
> all tenants. The two anchors are separate because their writes sets are separate:
> `t-d2-06` writes `domain/policy.py`, which `t-f1-02` froze, and the policy engine is one
> of the five areas in `CLAUDE.md` that no green suite protects. It gets the manual check
> and it never shares a wave with anything else touching that file.

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
6. **An anchor names a module, never a package.** `docs/WAVES.md` rule 2 asks whether two
   tasks write the same file, and a directory has no answer. If a task truly needs two
   files, name both.
7. **Take your migration id from the table at the end of this file, not from the end of
   `migrations.py`.** Define the `Migration` in your own module, as
   `PROFILE_SNAPSHOT_MIGRATION` does. `migrations.py` is not a shared write and must stay
   that way.
8. **A criterion that a fake cannot prove does not become a test.** Split it: the proxy a
   fake can prove closes `DONE`, and the real observation closes `MEASURED`. A green test
   standing in for a measurement certifies nothing while looking like coverage.


---

## F8 · Knowledge: Skills and `FULL_TEXT`

Done when an administrator changes a price through the admin endpoint and the very next
turn quotes the new price, with the change auditable — and when a second agent whose
profile does not list that collection cannot retrieve from it.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f8-01"></a>t-f8-01 | Knowledge domain types; `KnowledgePolicy.can_read()` — empty means **nothing**; `TenantKnowledgePolicy`, the tenant narrowing, whose tenant seat has no default | `domain/knowledge.py` | **DONE** |
| <a id="t-f8-02"></a>t-f8-02 | `KnowledgeBase` protocol — **read only, no write method exists**; every read scoped by a tenant-narrowed policy | `ports/knowledge_base.py` | **FROZEN — contract already correct, test is a regression lock** |
| <a id="t-f8-03"></a>t-f8-03 | `KnowledgeAdmin` protocol; `AdminIdentity` is a distinct type; `TenantAdminScope` scopes every write to one tenant | `ports/knowledge_admin.py` | **DONE** |
| <a id="t-f8-04"></a>t-f8-04 | Postgres adapter, `FULL_TEXT` only; versioned docs with `effective_from`; **the tenant predicate goes in the SQL** — see the note below; migration `0013` lives here | `adapters/driven/knowledge_pg/repository.py` | **DONE** |
| <a id="t-f8-05"></a>t-f8-05 | `knowledge_search` tool + untrusted-content wrapping of excerpts | `adapters/driven/agent_pydantic/runner.py` | TODO |
| <a id="t-f8-06"></a>t-f8-06 | Admin HTTP routes, separate from the chat surface | `adapters/driving/http/admin_routes.py` | TODO |
| <a id="t-f8-07"></a>t-f8-07 | Test: a chat caller cannot reach any write path | `Core/tests/unit/test_knowledge.py` | TODO |
| <a id="t-f8-08"></a>t-f8-08 | Test: cross-collection and cross-tenant retrieval both return empty — cross-collection stays here; cross-tenant is proven STRUCTURALLY in `test_ports_knowledge_base.py` and must ALSO be asserted against `t-f8-04`'s SQL, because a query with no tenant in it no longer type-checks | `Core/tests/unit/test_knowledge.py` | TODO |

> `SEMANTIC` and `HYBRID` are **not** implemented in F8. Enabling them later is one config
> line per collection plus pgvector in the existing instance.

> **`t-f8-02` told its adapter to filter on a value the contract never carried.** The port's
> own docstring said "filter by tenant IN THE QUERY, never post-retrieval — cross-tenant
> leakage through a shared index is the classic multi-tenant RAG breach", and then handed the
> adapter a `KnowledgePolicy`: `enabled`, `collections`, `mode`, `top_k`, `min_score`,
> `max_context_chars`. No tenant, anywhere in the contract. [`t-f8-04`](#t-f8-04) was the next
> task to run and it would have written the adapter that cannot obey the port it implements —
> and nothing would have failed, because a missing predicate returns MORE rows, not fewer.
>
> Same class as [`t-d2-06`](#t-d2-06), and closed the same way on purpose. `RuleSet` records
> the roles and the channel it was narrowed for, so a snapshot IS one caller's answer and cannot
> be pointed at another one. `TenantKnowledgePolicy` is a `KnowledgePolicy` narrowed for exactly
> one tenant, built by `TenantKnowledgePolicy.for_caller(caller, policy)`, and it is the only
> thing `KnowledgeBase` accepts. Two security boundaries, one shape for a reader to learn.
>
> It is a SUBCLASS rather than a field on `KnowledgePolicy`, because `KnowledgePolicy` is profile
> configuration: `_build_knowledge_policy` derives the accepted YAML keys from
> `fields(KnowledgePolicy)`, so a `tenant_id` there becomes an authorable key, and a profile file
> naming its own tenant is the breach with a config file's manners. It is not a second parameter
> for the reason `t-d2-06` already gives: a tenant passed beside the policy type-checks perfectly
> while naming a tenant that policy was never loaded for.
>
> **[`t-f8-03`](#t-f8-03) had the same hole, with no warning on it at all.** `AdminIdentity`
> carries `collections` and no tenant, and collection ids are not tenant-unique — that is settled
> by the read port needing BOTH a collection intersection AND a tenant predicate — so an
> administrator granted `pricing` was granted every tenant's `pricing`, on a WRITE path. The
> tenant could not join `AdminIdentity`: non-negotiable #9's runtime half is that
> `AdminIdentity(**asdict(caller))` has nowhere to put a caller's fields, and `tenant_id` is one
> of the three names that class docstring refuses for exactly that reason. It could not subclass
> one either — `def scope_for(caller) -> TenantAdminScope` would be the forbidden widening under
> a name the package sweep, which matches the return type by NAME, had never been told about. So
> `TenantAdminScope` WRAPS the identity, the sweep now knows both names, and every write method
> takes the scope.
>
> **What this changes downstream.** [`t-f8-04`](#t-f8-04) puts the tenant IN THE SQL: a NOT NULL
> `tenant_id` column in migration `0013`, and `WHERE tenant_id = %s` in every statement it writes,
> read and write alike — never a filter applied to rows already fetched. `search`, `get` and
> `full_context` take it from `policy.tenant_id`; `upsert`, `delete` and `list_docs` from
> `scope.tenant_id`; there is no other source and no way to call them without one.
> [`t-f8-08`](#t-f8-08) asserts it from the other side, and its cross-tenant half changes shape.
> The "tenant A asks for tenant B" case cannot be written as a domain test any more — the call
> does not type-check, which is the point — so that half is now two things: the structural
> assertions already in `Core/tests/unit/test_ports_knowledge_base.py`, and a behavioural case
> in the adapter suite proving the SQL that `t-f8-04` actually emits carries the predicate. Only
> the cross-collection half stays in `test_knowledge.py`.
>
> The lesson is about this file, not about that code: a port that instructs its adapter to filter
> on something the port never hands over is itself the defect. Fix the port — the adapter cannot.


---

## F9 · Agent-to-agent foundations

Done when a customer-service agent asks a personal-assistant agent a question, that agent
suspends to ask its human, and the answer returns to the first agent — with a hop limit
enforced and the answer wrapped as untrusted content.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f9-01"></a>t-f9-01 | Peer domain types; `PeerPolicy.may_ask()` — empty means nobody | `domain/peers.py` | **DONE** |
| <a id="t-f9-02"></a>t-f9-02 | `AgentMailbox` protocol, A2A-shaped | `ports/agent_mailbox.py` | **FROZEN — contract already correct, test is a regression lock** |
| <a id="t-f9-03"></a>t-f9-03 | Durable queue adapter; migration `0014` lives here | `adapters/driven/peers/mailbox.py` | **DONE** |
| <a id="t-f9-04"></a>t-f9-04 | `ask_peer` as an externally-executed deferred tool — **shared, not per-vertical** | `adapters/driven/tools/peers.py` | TODO |
| <a id="t-f9-05"></a>t-f9-05 | Hop limit + two-sided allowlist check | `adapters/driven/peers/hop_limit.py` | TODO |
| <a id="t-f9-06"></a>t-f9-06 | **Communicable suspension**: tell the user before suspending on a peer | `application/start_turn.py` | TODO |
| <a id="t-f9-07"></a>t-f9-07 | Test: A→B→A is refused at the hop limit | `Core/tests/unit/test_peers.py` | TODO |

---

## F10 · Transcript and audit API

Done when an operator can page a full conversation with tools, reasoning and pending state,
the same conversation rendered for the user shows only messages plus a pending placeholder,
and neither query can cross a tenant boundary.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f10-01"></a>t-f10-01 | Transcript domain types + the `VISIBLE_TO` table | `domain/transcript.py` | **FROZEN — contract already correct, test is a regression lock** |
| <a id="t-f10-02"></a>t-f10-02 | `TranscriptReader` protocol | `ports/transcript_reader.py` | **DONE** |
| <a id="t-f10-03"></a>t-f10-03 | Postgres projection; cursor pagination, tenant predicate in SQL; migration `0016` lives here | `adapters/driven/persistence_pg/transcript_repository.py` | **DONE** |
| <a id="t-f10-04"></a>t-f10-04 | Persist reasoning blocks (admin-only) in `ConversationStore`; migration `0015` lives here, next to `0009` | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE** |
| <a id="t-f10-05"></a>t-f10-05 | **Decide the reasoning retention window** and record it in `docs/DECISIONS.md` | `docs/DECISIONS.md` | TODO |
| <a id="t-f10-06"></a>t-f10-06 | The four read endpoints | `adapters/driving/http/transcript_routes.py` | TODO |
| <a id="t-f10-07"></a>t-f10-07 | Test: `VISIBLE_TO` covers every `EntryKind` | `Core/tests/unit/test_transcript.py` | **FROZEN — property already fully locked, no red reachable** |
| <a id="t-f10-08"></a>t-f10-08 | Test: the user projection never leaks a tool name | `Core/tests/unit/test_transcript.py` | TODO |
| <a id="t-f10-09"></a>t-f10-09 | Test: a transcript query cannot cross a tenant | `Core/tests/unit/test_transcript.py` | TODO |

> **`t-f10-01`'s visibility table was a default wearing a decision's clothes.** The table
> shipped as `{USER: frozenset({...three kinds...}), ADMIN: frozenset(EntryKind)}`, and
> `t-f10-07` — "the table covers every `EntryKind`" — passed the moment it was written. Not
> because the table was right: because ADMIN was DERIVED FROM THE ENUM. Every kind ever
> added was in it by construction, so the assertion restated `EntryKind` back to itself and
> could not fail. A new kind became admin-visible with nobody deciding whether a USER may
> see it, and the suite stayed green.
>
> Same class as [`t-f1-04`](#t-f1-04) and [`t-f2-04`](#t-f2-04), one level in: not a task
> that names the wrong file, but a guard that asks a question whose answer is already
> guaranteed. A test that cannot go red is documentation with a green tick next to it.
>
> `domain/transcript.py` now declares `VISIBILITY`, one row per `EntryKind` with a
> hand-written `True`/`False` per `Audience`, and derives `VISIBLE_TO` from it. Both axes
> now fail: a new kind leaves a missing row, a new audience leaves an unanswered cell in
> every row. Both were proven by adding a throwaway member and watching the suite go red.
> The reader's lookup is unchanged — `VISIBLE_TO[audience]` still returns a
> `frozenset[EntryKind]`, which is what [`t-f10-02`](#t-f10-02) froze in the port docstring.
>
> Writing the cells out forced one cell that had never been decided: ADMIN no longer sees
> `PENDING_PLACEHOLDER`. The placeholder is not stored — it is the substitute the projection
> hands a USER *instead of* a `PENDING_REQUEST`, and an operator reads the real request.
> `frozenset(EntryKind)` had admitted it to the admin view on its own, contradicting the
> visibility table in `docs/ARCHITECTURE.md` § "Store everything, filter on read", which has
> always shown that row as user-only. No USER cell was wrong: both halves of non-negotiable
> #11 held and are now asserted separately — `TOOL_CALL`, `TOOL_RESULT` and
> `PENDING_REQUEST` stay hidden (never WHICH tool), `PENDING_PLACEHOLDER` stays visible
> (always THAT something is pending).
>
> [`t-f10-07`](#t-f10-07) is now a real anchor rather than a tautology, and
> [`t-f10-02`](#t-f10-02), [`t-f10-03`](#t-f10-03) and [`t-f10-06`](#t-f10-06) can build the
> reader on top of it: the grid is total over `EntryKind` × `Audience`, so the projection may
> index it without a fallback branch, and any kind those three add later cannot reach an
> endpoint until someone has answered for it.

---

## D2 additions

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-d2-04"></a>t-d2-04 | Full A2A wire protocol as an adapter swap; `t-f9-03`'s module stays, this one replaces it behind `AgentMailbox` | `adapters/driven/peers/mailbox_a2a.py` | TODO |
| <a id="t-d2-05"></a>t-d2-05 | `SEMANTIC` retrieval via pgvector, per collection | `adapters/driven/knowledge_pg/semantic.py` | TODO |

> **Six anchors named a package instead of a module, and a package is not a writes set.**
> `t-f9-04`, `t-f8-04`, `t-f9-03`, `t-f9-05`, `t-d2-04` and `t-d2-05` each pointed at a
> directory. `docs/WAVES.md` rule 2 asks one question of every pair of tasks in a wave — do
> they write the same file? — and against a directory there is no answer: two anchors in
> `adapters/driven/peers/` might be disjoint modules or the same one, and the scheduler
> cannot tell without opening code that does not exist yet. It would either serialise work
> that could run at once, or run two agents into one file.
>
> Every anchor now names a module. A task that legitimately needs a second file says so in
> the anchor rather than widening back to the directory.


---

## F1 additions — profile versioning (D20)

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f1-17"></a>t-f1-17 | `AgentProfile.version`; monotonic per id, bumped on YAML change | `domain/profile.py` | **DONE** |
| <a id="t-f1-18"></a>t-f1-18 | Persist `profile_version` + resolved snapshot on every turn | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE** |
| <a id="t-f1-19"></a>t-f1-19 | Test: a turn's audit record reproduces the persona and rules in force | `Core/tests/unit/test_profile_versioning.py` | **DONE** |

## F2 additions — concurrency and coalescing (D19)

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f2-03"></a>t-f2-03 | Partitioned queue: `partition_concurrency=1`, key = session id | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f2-04"></a>t-f2-04 | Pending-input buffer table and its repository; migration `0011` lives here | `adapters/driven/persistence_pg/pending_input_repository.py` | **DONE** |
| <a id="t-f2-05"></a>t-f2-05 | Coalescing enqueue: `delay_seconds` + `deduplication_id` / `return-existing` | `adapters/driving/http/routes.py` | TODO |
| <a id="t-f2-06"></a>t-f2-06 | Typing-indicator extension of the window, where the channel supplies it | `adapters/driven/human/gateway.py` | TODO |
| <a id="t-f2-07"></a>t-f2-07 | Test: three messages inside the window produce **one** turn and one model call | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f2-08"></a>t-f2-08 | Test: two simultaneous sessions run in parallel; two messages on one session do not | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f2-09"></a>t-f2-09 | Test: a message arriving mid-turn becomes a follow-up turn, not a lost message | `Core/tests/integration/test_durability.py` | TODO |
| <a id="t-f2-10"></a>t-f2-10 | `_step_drain_pending` — the workflow's first step, drains `t-f2-04`'s buffer into the turn | `adapters/driving/workflow/turn_workflow.py` | TODO |

> **`t-f2-04` was split for the same reason `t-f1-04` was rewritten.** It read "Pending-input
> buffer table **+ drain as the workflow's first step**" and named the persistence package,
> but the drain is a step in `turn_workflow.py`, which `t-f2-03` owns. One anchor, two
> layers, two files, and a wave scheduler that cannot tell — either the repository author
> writes a DBOS step or the workflow author writes DDL, and whichever happens the other
> half is silently nobody's.
>
> Same class as [`t-f1-04`](#t-f1-04): a task naming one file and an activity that lives in
> another is itself the defect. The table half is `t-f2-04`, the step half is `t-f2-10`, and
> `t-f2-10` cannot share a wave with `t-f2-03` or `t-f3-07` — all three write the workflow.

---

## Migration ids — pre-allocated

Seven anchors still to come create a table. If each one reaches for the next free id when
its wave starts, `migrations.py` becomes a file seven anchors write and the scheduler must
serialise all seven — in every wave they appear in, across five phases, for no reason other
than a shared list.

`0009_turns_profile_snapshot` already showed the way out. It is not in `migrations.py`; it
is `PROFILE_SNAPSHOT_MIGRATION` in `conversation_repository.py`, the module that owns the
column, applied after `migrations.run_migrations`. **Every anchor below defines its own
`Migration` object in its own module, following that precedent**, so `migrations.py` is
never a shared write and no two anchors collide on it.

The ids are allocated here, in advance, because two agents picking "the next free id" in
parallel pick the same one:

| Id | Anchor | Table |
|---|---|---|
| `0010` | [`t-f3-04`](#t-f3-04) | human-decision correlation, unguessable ids |
| `0011` | [`t-f2-04`](#t-f2-04) | pending-input buffer |
| `0012` | [`t-d2-03`](#t-d2-03) | tenant column on `policy_rules` |
| `0013` | [`t-f8-04`](#t-f8-04) | knowledge documents, versioned by `effective_from`, with a **NOT NULL `tenant_id`** and an index leading on it |
| `0014` | [`t-f9-03`](#t-f9-03) | peer mailbox queue |
| `0015` | [`t-f10-04`](#t-f10-04) | reasoning blocks, admin-only |
| `0016` | [`t-f10-03`](#t-f10-03) | transcript projection |

An id is spent when it is written here, not when the table ships. An anchor that turns out
not to need a table leaves its id retired rather than recycling it — a reused id against a
database that already recorded the first one is a migration that silently never runs.
