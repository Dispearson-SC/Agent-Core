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
| <a id="t-f0-05"></a>t-f0-05 | **Process entry point.** An ASGI app that builds the container once at startup and mounts the routes, plus `python -m agent_core` to serve it | `Core/src/agent_core/main.py` | **DONE** |
| <a id="t-f0-06"></a>t-f0-06 | `GET /turns/{turn_id}` — the pull half of the 202. **Also reconciles the two ids**: `run_turn_workflow` mints its `TurnId` inside `_step_new_turn_id`, so the id a caller could poll and the id every audit row is filed under are different values and nothing joins them. **Also export a send-side function from the workflow**: a `DecisionSignal` keyed on the domain `turn_id` cannot reach the running workflow through `DBOS.send_async(destination_id=...)` while the two ids differ, which blocks `t-f3-11` | `adapters/driving/http/routes.py` | **DONE — the two ids are one id: `_step_new_turn_id` now returns the running workflow's id** |
| <a id="t-f0-07"></a>t-f0-07 | **An operator console.** A REPL driving adapter for inspecting and exercising each agent: list profiles, read a profile's resolved configuration, see the toolset it actually gets, hold a conversation with it, and watch every tool call with the policy decision and `rule_id` that admitted or refused it | `adapters/driving/cli/console.py` | **DONE** |

> **`t-f0-05` had no anchor, and that is why nothing could be started.** Every F0 anchor
> above was closed and the repository was still a library with tests: no `main`, no ASGI
> app, no process. The gap was recorded in `docs/STATE.md` under "what cannot be run" and
> nowhere else, so no wave could ever be scheduled to close it — the same defect this file
> names three times already, one layer further out. A phase whose done-when is "a POST
> returns a model response" needs something for the POST to arrive at.

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
| <a id="t-f1-13"></a>t-f1-13 | Postgres `ConversationStore`; index on `(session_id, seq)` | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE — `load_history` and `append` only; `append_outcome` split out to [`t-f1-22`](#t-f1-22)** |
| <a id="t-f1-22"></a>t-f1-22 | `PgConversationStore.append_outcome` — persist a turn's terminal state; `StartTurn` step 7 awaits it on **every** turn, finished or suspended | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE** |
| <a id="t-f1-24"></a>t-f1-24 | **The store and the runner disagree about how history is encoded, and no test ever crossed that seam.** A second turn in one session raises `UnsupportedHistoryError: history carries a dict; this adapter accepts only Pydantic AI ModelMessage values`. Every test injected its own history, so the round trip `append` → `load_history` → `run` was never driven end to end | `adapters/driven/persistence_pg/conversation_repository.py` | **DONE — the STORE was wrong, not the runner** |
| <a id="t-f1-21"></a>t-f1-21 | `ToolProvider` adapter — build a profile's toolset from the registered tool packages, and wire it into the container. **`t-f1-07` froze the port; nothing implements it** | `adapters/driven/tools/provider.py` | **DONE** |
| <a id="t-f1-23"></a>t-f1-23 | **Production applies no migrations at all.** `composition.py` calls neither `run_migrations` nor any `apply_*_migration`, so with the sibling-migration convention now in use there is no production applier for ANY of them — every integration test applies its own by hand | `Core/src/agent_core/composition.py` | **DONE — discovery-based, so the eighth sibling migration cannot be forgotten; `main.py` now starts through `start_container`** |
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

> **`t-f1-13` was mis-marked DONE, and `t-f1-21` was never marked at all.** Both are the
> same defect wearing different clothes, and both blocked the one thing the build could not
> do: start.
>
> `t-f1-13` closed on an assertion covering `load_history`, while `append_outcome` in the
> same class still raised `NotImplementedError`. That is the **third** instance of *the
> anchor is wider than the assertion it was given* — see [`t-f1-04`](#t-f1-04) and
> [`t-f1-12`](#t-f1-12). The fix here is the same one applied there: split the anchor so a
> criterion and an assertion are the same size. `append_outcome` is [`t-f1-22`](#t-f1-22).
>
> `t-f1-21` is the opposite failure. `t-f1-07` froze the `ToolProvider` Protocol and no
> anchor ever claimed the adapter, so `composition.py` could build every other seat and
> still refuse to produce a working `start_turn`, and the only implementation in the
> repository was `_DeliveryToolProvider` in `tests/integration/test_f0_end_to_end.py` —
> whose own docstring calls it *a TEST double, not an adapter*. **A frozen port with no
> adapter is not a finished port**, and a test double standing in for the missing one is
> exactly what hides that, the same way the injected model factory hid
> [`t-f0-04`](#t-f0-04)'s missing endpoint.

> **`t-f1-23` is that same shape a third time, and it is the widest one yet.** Seven
> migrations now live in their own sibling modules — the convention
> [`t-f1-16`](#t-f1-16)'s note established so `migrations.py` would never be a shared write —
> and **nothing in production applies any of them.** Every integration test applies its own by
> hand, so the suite is green and a fresh deployment has no schema.
>
> A per-test applier is a test double for a startup step nobody wrote. It hid the gap exactly
> the way `_DeliveryToolProvider` hid the missing `ToolProvider` and the injected factory hid
> the missing model endpoint: **the fixture that supplies the missing piece is what stops
> anyone noticing it is missing.** Three instances now, in three different layers. When a test
> has to build something before it can run, ask who builds it in production — and if the
> answer is "the test", that is the anchor.

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
| <a id="t-f2-02"></a>t-f2-02 | Crash-injection test | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |

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
| <a id="t-f3-05"></a>t-f3-05 | Decide four-eyes: must the approver differ from the requester? Write the answer down | `application/decide_approval.py` | **DONE — D25** |
| <a id="t-f3-06"></a>t-f3-06 | **Wiring only**: populate `t-f3-13`'s registry with the concrete channels, shared by `ChannelHumanGateway` and turn delivery (D23). Lands after `t-f3-08` and `t-f3-09` | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f3-07"></a>t-f3-07 | `_step_deliver` — generic outbound-delivery step for a finished `TurnResult`, dispatched through the channel registry; no new port (D23) | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f3-08"></a>t-f3-08 | WhatsApp Cloud API webhook adapter — inbound POST and outbound send, registered in the channel registry | `adapters/driving/channels/whatsapp.py` | **DONE — payload mapping only, not verified live (no WABA token and no public webhook)** |
| <a id="t-f3-09"></a>t-f3-09 | Telegram Bot API webhook adapter — inbound webhook (or `getUpdates`) and outbound send, registered in the channel registry | `adapters/driving/channels/telegram.py` | **DONE — verified live against the real Bot API (`getMe` + a real `getUpdates` payload); the webhook path is still unverified, no public HTTPS here** |
| <a id="t-f3-10"></a>t-f3-10 | `PydanticAgentRunner.resume()` — feed an outside answer back as the pending tool's result; `tool_call_id` verbatim | `adapters/driven/agent_pydantic/runner.py` | **DONE** |
| <a id="t-f3-11"></a>t-f3-11 | `POST /decisions/{corr_id}` — the human's approve/refuse route; resolves the correlation, never blocks | `adapters/driving/http/routes.py` | **DONE** |
| <a id="t-f3-12"></a>t-f3-12 | Enable the three skipped F3 tests: approval survives a redeploy, `tool_call_id` round-trips verbatim, resolving twice is a no-op | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |
| <a id="t-f3-13"></a>t-f3-13 | `Channel` structural Protocol (`send`) + the registry type it keys. **Lands before `t-f3-08`, `t-f3-09` and `t-f3-06`** | `adapters/driving/channels/registry.py` | **DONE** |
| <a id="t-f3-14"></a>t-f3-14 | `AuditSink` member for a **refused** decision. A four-eyes rejection currently writes nothing — the sink has no member for it, so the one decision worth auditing is the one that leaves no trace | `ports/audit_sink.py` | **DONE — port, adapter, migration `0019` and the `DecideApproval` call site** |
| <a id="t-f3-15"></a>t-f3-15 | Wire `DecideApproval` in the container. Nothing constructs it, so D25's requester seat has no binder and the approval route has nothing to call | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f3-18"></a>t-f3-18 | **`UnknownChannelError` does not survive the durable boundary.** DBOS pickles a workflow's exception and `BaseException.__reduce__` yields only `args`, so unpickling calls the two-argument `__init__` with one: `get_result()` raises `TypeError` and the channel id is gone from the message. `DuplicateChannelError` has the same shape | `adapters/driving/channels/registry.py` | **DONE** |
| <a id="t-f3-19"></a>t-f3-19 | **`render_ask` promises an exhaustiveness its code cannot deliver.** Its docstring says a kind it has no sentence for "must be a type error at the moment the domain grows one"; a third `PendingKind` was added and mypy strict reported nothing, because the `match` assigns to a local instead of returning. A new kind is an `UnboundLocalError`, not a type error | `adapters/driven/human/gateway.py` | **DONE** |
| <a id="t-f3-20"></a>t-f3-20 | Bind the `resume_turn` seat in `bind_turn_workflow`. `_step_resume` is wired and the container does not hand it a `ResumeTurn`, so a real process still cannot resume a turn — it fails loudly naming the fix, exactly as `human_gateway` did before [`t-f3-17`](#t-f3-17) | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f3-16"></a>t-f3-16 | **`AgentRunner.resume` carries no `CallerIdentity` and no `SessionRef`.** The resumed call is the one a human authorised — but any FURTHER tool call the model makes after resuming runs with **no policy check, no audit row and no compaction**, because neither capability can be attached | `ports/agent_runner.py` | **DONE — port widened; the call sites it moved under are the wave-14 barrier** |
| <a id="t-f3-17"></a>t-f3-17 | Wire `TurnWorkflowDependencies.human_gateway` and retire `_step_publish` / `_step_resume`'s stale `NotImplementedError` — its stated reason ("HumanGateway has no adapter yet") stopped being true at `t-f3-04` | `adapters/driving/workflow/turn_workflow.py` | **DONE** |

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
| <a id="t-f4-03"></a>t-f4-03 | Enable the contract test | `Core/tests/unit/test_contract.py` | **DONE** |

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
| <a id="t-f5-05"></a>t-f5-05 | Pairing test per rung | `Core/tests/unit/test_compaction.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |
| <a id="t-f5-06"></a>t-f5-06 | **Proxy test**, not the criterion: over 200 simulated turns, tokens resent per turn stay bounded and the prompt prefix is rewritten no more than the ladder's trigger allows | `Core/tests/unit/test_compaction.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |
| <a id="t-f5-07"></a>t-f5-07 | `ProcessHistory` wiring — compaction reaches the model request through the runner, not around it | `adapters/driven/agent_pydantic/runner.py` | **DONE** |
| <a id="t-f5-08"></a>t-f5-08 | `_step_compact` — R5's step wrapping `CompactContext`; DBOS's durable lock gives one pass per session | `adapters/driving/workflow/turn_workflow.py` | **DONE — runs AFTER delivery, so a summariser pass never delays the answer** |
| <a id="t-f5-09"></a>t-f5-09 | **Measure** cost per turn against the provider bill over real F4 traffic; record the number, its date, its model and its traffic in `docs/FIELD-NOTES.md` | `docs/FIELD-NOTES.md` | **MEASURED — 17,666 vs 33,684 tokens over identical real traffic, 2026-09-11; read what it does NOT show** |
| <a id="t-f5-10"></a>t-f5-10 | `Summariser` adapter. **`LadderContextEngine` takes one optionally and no implementation exists anywhere**, so production is wired with `None` and rungs L3/L4 free nothing on every turn | `adapters/driven/context/summariser.py` | **DONE — the adapter; see the note, production was still wiring `None` until the barrier** |
| <a id="t-f5-11"></a>t-f5-11 | `update_from_response` accumulates `input_tokens + output_tokens` **monotonically and never decreases after a compaction**, so `estimated_tokens(session)` is TOTAL SPEND, not context size. Fed to the trigger it would latch `True` forever and compact every turn — the exact cache destruction F5 warns about | `adapters/driven/context/engine.py` | **DONE** |

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

> **`t-f5-11` is the same shape as `t-f5-10`, and it is dormant rather than harmless.** The
> accumulator is not read today: `runner.py::_history_processor` builds its state from
> `self._token_estimator(messages)` and never touches it, while `start_turn.py` keeps feeding
> it. So there is a value that LOOKS like the trigger's input, is wired as if it were, and is
> wrong — a dead accumulator sitting exactly where a live one belongs. Whoever connects it
> next inherits a trigger that is permanently true.
>
> **`t-f5-10` also has no live-provider check.** `tests/conftest.py` forbids a network in
> `tests/unit/`, and the anchor's writes set covered no integration file, so the summariser's
> real MiniMax route is exercised by nothing. That is the same gap the injected model factory
> left behind in `t-f0-04` — the code path production uses is the one no test drives.

> **`t-f5-10` is the silent-bug table happening in front of us.** `CLAUDE.md` says the
> compaction strategy surfaces "only on the bill", and here is the instance: the ladder is
> fully implemented, fully tested, wired into the runner by `t-f5-07`, and its top two rungs
> do nothing, because the collaborator they call is `None` in production. Every test passes.
> Nothing logs a warning. The ladder LOOKS wired and half of it is inert, and the only place
> that shows up is the invoice — which is exactly the failure mode the table names.
>
> It went unnoticed because an optional constructor argument is a decision deferred to
> whoever wires it, and nobody wired it. An optional seat with no adapter is not a seat, it
> is a gap with a default. `t-f5-09` cannot be measured until this lands: measuring a ladder
> whose expensive rungs are switched off measures the wrong thing and writes the number down
> as if it were the right one.

---

## F6 · Skills and MCP

Done when the agent solves a task by reading a skill that was not in its prompt, and an
MCP tool denied by policy is recorded exactly like a local one.

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f6-01"></a>t-f6-01 | `SkillRegistry` protocol; index is metadata only | `ports/skill_registry.py` | **DONE** |
| <a id="t-f6-02"></a>t-f6-02 | Filesystem registry; cache by `(path, mtime)`; no path traversal | `adapters/driven/skills_fs/registry.py` | **DONE** |
| <a id="t-f6-03"></a>t-f6-03 | `MCPToolset` composed into `ToolProvider`; name prefixing | `adapters/driven/mcp/toolsets.py` | **DONE** |
| <a id="t-f6-04"></a>t-f6-04 | Untrusted-content wrapper + reduced budget for `mcp_*` | `adapters/driven/agent_pydantic/runner.py` | **DONE** |
| <a id="t-f6-05"></a>t-f6-05 | MCP schema cache so policy filtering never spawns a server | `adapters/driven/mcp/toolsets.py` | **DONE** |
| <a id="t-f6-06"></a>t-f6-06 | Hostile-server shadowing test | `Core/tests/unit/test_policy.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |

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
| <a id="t-f7-05"></a>t-f7-05 | `request_evidence` as an externally-executed deferred tool — **shared, not per-vertical** | `adapters/driven/tools/evidence.py` | **DONE** |
| <a id="t-f7-06"></a>t-f7-06 | **Write down the bytes-vs-signed-URL decision** in `docs/DECISIONS.md` | `docs/DECISIONS.md` | **DONE — D26** |
| <a id="t-f7-07"></a>t-f7-07 | The BYTES vs SIGNED_URL branch in the runner, implementing `t-f7-06`'s decision | `adapters/driven/agent_pydantic/runner.py` | **DONE — bytes by D26; SIGNED_URL refuses loudly, see [`t-f7-10`](#t-f7-10)** |
| <a id="t-f7-08"></a>t-f7-08 | `POST /evidence/{corr_id}` — the upload route; size first, then sniff, then accept (`t-f7-03`) | `adapters/driving/http/routes.py` | **DONE — and it now wakes the turn, see [`t-f7-11`](#t-f7-11)** |
| <a id="t-f7-09"></a>t-f7-09 | Enable the skipped F7 test: an evidence request resumes with the uploaded image | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |
| <a id="t-f7-10"></a>t-f7-10 | **`signed_url` loses the media type, so SIGNED_URL delivery is unusable against the shipped store.** It returns a bare string and `media_fs` signs a content-addressed path with no extension, so `MediaRef.kind` and `mime_type` are gone before the payload reaches the runner. `t-f7-07` refuses loudly rather than guess a type a provider would answer about anyway | `ports/media_store.py` | **DONE — `SignedMedia` carries the type; fakes follow at the barrier** |
| <a id="t-f7-11"></a>t-f7-11 | The evidence wire was typed too narrowly: `HumanAnswer.note` was `str | None`, so no type-checked caller could put a `MediaRef` on the durable wire and `t-f7-09` needed a visible `cast`. `POST /evidence` also stored and never woke the turn | `adapters/driving/workflow/turn_workflow.py` | **DONE** |

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
| <a id="t-d2-03"></a>t-d2-03 | Tenant dimension in policy rules — **SQL predicate and migration `0012` only**; if the condition below fails, the domain half is `t-d2-06` | `adapters/driven/persistence_pg/policy_repository.py` | **DONE** |
| <a id="t-d2-06"></a>t-d2-06 | **TAKEN — condition checked 2026-09-10 and it fails.** Carry the tenant through `RuleSet.applicable` / `PolicyRule.matches` — `RuleSet` records no tenant, so the SQL alone cannot make it safe | `domain/policy.py` | **DONE** |

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
| <a id="t-later-01"></a>t-later-01 | Scheduled turns; service identity, fresh session per run | `adapters/driving/scheduler/cron.py` | **DONE** |
| <a id="t-later-02"></a>t-later-02 | Fraud vertical — the real proof the core did not change | `adapters/driven/tools/fraud/tools.py` | **DONE** |
| <a id="t-later-03"></a>t-later-03 | `classify_error` — five strategies, only after a second provider is live | `ports/model_gateway.py` | **DONE** |

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
| <a id="t-f8-05"></a>t-f8-05 | `knowledge_search` tool + untrusted-content wrapping of excerpts. **Also the only place `KnowledgePolicy.enabled` can be enforced** — `can_read` is pure membership and `PgKnowledgeBase` never consults it, so today a profile with `enabled: false` and a non-empty `collections` retrieves normally and nothing fails | `adapters/driven/agent_pydantic/runner.py` | **DONE — including the `enabled` gate, enforced by ABSENCE of the tool** |
| <a id="t-f8-06"></a>t-f8-06 | Admin HTTP routes, separate from the chat surface | `adapters/driving/http/admin_routes.py` | **DONE — plus the `knowledge_pg/admin.py` adapter nobody owned** |
| <a id="t-f8-07"></a>t-f8-07 | Test: a chat caller cannot reach any write path | `Core/tests/unit/test_knowledge.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |
| <a id="t-f8-08"></a>t-f8-08 | Test: cross-collection and cross-tenant retrieval both return empty — cross-collection stays here; cross-tenant is proven STRUCTURALLY in `test_ports_knowledge_base.py` and must ALSO be asserted against `t-f8-04`'s SQL, because a query with no tenant in it no longer type-checks | `Core/tests/unit/test_knowledge.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |

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
| <a id="t-f9-04"></a>t-f9-04 | `ask_peer` as an externally-executed deferred tool — **shared, not per-vertical** | `adapters/driven/tools/peers.py` | **DONE** |
| <a id="t-f9-05"></a>t-f9-05 | Hop limit + two-sided allowlist check | `adapters/driven/peers/hop_limit.py` | **DONE** |
| <a id="t-f9-06"></a>t-f9-06 | **Communicable suspension**: tell the user before suspending on a peer | `application/start_turn.py` | **DONE** |
| <a id="t-f9-07"></a>t-f9-07 | Test: A→B→A is refused at the hop limit | `Core/tests/unit/test_peers.py` | **FROZEN — contract already correct; regression lock, teeth being proven by mutation at the wave-12 barrier** |
| <a id="t-f9-08"></a>t-f9-08 | **`PendingKind` has no member for an agent-executed deferred call**, so `ask_peer` is indistinguishable by kind from a request for a photo. Two modules now key on the same tool-name string to tell them apart — a workaround that a third vertical will copy | `domain/turn.py` | **DONE** |
| <a id="t-f9-09"></a>t-f9-09 | `read_answer` is on both mailbox adapters and **not on the `AgentMailbox` Protocol**, so application code typed against the port cannot read an answer back — only code typed against a concrete adapter can, which is the port cut leaking | `ports/agent_mailbox.py` | **DONE** |

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
| <a id="t-f10-05"></a>t-f10-05 | **Decide the reasoning retention window** and record it in `docs/DECISIONS.md` | `docs/DECISIONS.md` | **DONE — D27** |
| <a id="t-f10-06"></a>t-f10-06 | The four read endpoints | `adapters/driving/http/transcript_routes.py` | **DONE** |
| <a id="t-f10-07"></a>t-f10-07 | Test: `VISIBLE_TO` covers every `EntryKind` | `Core/tests/unit/test_transcript.py` | **FROZEN — property already fully locked, no red reachable** |
| <a id="t-f10-08"></a>t-f10-08 | Test: the user projection never leaks a tool name | `Core/tests/unit/test_transcript.py` | **DONE** |
| <a id="t-f10-09"></a>t-f10-09 | Test: a transcript query cannot cross a tenant | `Core/tests/unit/test_transcript.py` | **DONE** |
| <a id="t-f10-10"></a>t-f10-10 | D27's retention sweep, and the index it needs. `reasoning_migration.py` claims `created_at` is indexed for a range delete, but it is the **third** column of `ix_turn_reasoning_session_created` — a global sweep by age is a sequential scan | `adapters/driven/persistence_pg/reasoning_migration.py` | **DONE** |

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
| <a id="t-d2-04"></a>t-d2-04 | Full A2A wire protocol as an adapter swap; `t-f9-03`'s module stays, this one replaces it behind `AgentMailbox` | `adapters/driven/peers/mailbox_a2a.py` | **DONE** |
| <a id="t-d2-05"></a>t-d2-05 | `SEMANTIC` retrieval via pgvector, per collection | `adapters/driven/knowledge_pg/semantic.py` | **DONE — and it proved F8's claim only half true; see the note** |
| <a id="t-d2-07"></a>t-d2-07 | **Nothing in `ports/` produces an embedding.** `ModelGateway` answers "which model", not "embed this text", and `KnowledgeAdmin.upsert` prescribes "re-embed only the changed chunks" with nothing to call — so `SEMANTIC` runs on an injected callable and a document written before anything embeds it never matches, silently | `ports/embedder.py` | **DONE — the port; no adapter implements it yet, see the note** |

> **Six anchors named a package instead of a module, and a package is not a writes set.**> **`t-d2-05` tested F8's claim and it is only half true.** F8 says enabling `SEMANTIC` later is
> "one config line per collection plus pgvector in the existing instance". For the **port** that
> held exactly: no signature changed, `KnowledgeBase` stayed read-only, and the tenant predicate
> went into the vector SQL the same way it went into the full-text SQL. For **deployment** it did
> not. It is a config line **plus an explicitly applied migration, on a server that has pgvector**
> — and the server the suite runs against does not. See the corrected section in
> `docs/FIELD-NOTES.md`.
>
> It also surfaced [`t-d2-07`](#t-d2-07), which is the more serious half. Nothing in `ports/`
> produces an embedding, so the adapter takes an injected callable and `embedding` is NULLABLE
> with `embedding IS NOT NULL` in the WHERE clause. **A document stored before anything embeds it
> never matches a SEMANTIC query, and nothing reports it.** A retrieval that silently returns
> nothing is indistinguishable from an empty corpus, which is the one failure mode this anchor was
> told not to ship — it is named here instead of hidden.


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
| <a id="t-f2-05"></a>t-f2-05 | Coalescing enqueue: `delay_seconds` + `deduplication_id` / `return-existing` | `adapters/driving/http/routes.py` | **DONE — unblocked by `t-f2-11`** |
| <a id="t-f2-06"></a>t-f2-06 | Typing-indicator extension of the window, where the channel supplies it | `adapters/driven/human/gateway.py` | **BLOCKED — no reachable subject in phase one; see the note** |
| <a id="t-f2-07"></a>t-f2-07 | Test: three messages inside the window produce **one** turn and one model call | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |
| <a id="t-f2-08"></a>t-f2-08 | Test: two simultaneous sessions run in parallel; two messages on one session do not | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |
| <a id="t-f2-09"></a>t-f2-09 | Test: a message arriving mid-turn becomes a follow-up turn, not a lost message | `Core/tests/integration/test_durability.py` | **DONE — proved against a REAL subprocess kill; teeth shown by three production mutations, each restored byte-exact** |
| <a id="t-f2-10"></a>t-f2-10 | `_step_drain_pending` — the workflow's first step, drains `t-f2-04`'s buffer into the turn | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f2-11"></a>t-f2-11 | **The coalescing window itself**: a separate NON-partitioned queue carrying a debounced workflow that, on expiry, enqueues the real turn onto the partitioned queue. `t-f2-05` is the `routes.py` half and cannot land without this | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f2-12"></a>t-f2-12 | **A worker killed mid-turn silences that session forever, and nothing raises.** `partition_concurrency=1` plus a PENDING row blocks the partition for every worker, while startup recovery is scoped to the CURRENT `application_version` — and the default version is a hash of registered source, so every deploy is a new one. **Nothing in `Core/src` ever constructs `DBOSConfig`** | `Core/src/agent_core/composition.py` | **DONE — `application_version` pinned; the price is in the docstring** |
| <a id="t-f2-13"></a>t-f2-13 | **Nothing in `Core/src` launches DBOS at all**, so [`t-f2-12`](#t-f2-12)'s pinned config is owned and unconsumed. `main.py` cannot call it (`dbos` is banned there and its docstring says so) and the workflow package exposes no bootstrap — so a launcher will assemble a second config by hand and the deadlock returns | `adapters/driving/workflow/bootstrap.py` | **DONE — `launch_dbos` cannot be handed a version; `main.py` calls it** |

> **`t-f2-06` is blocked by a dependency, not by effort, and it stays that way on purpose.**
> It extends the coalescing window while a user is still typing, and that needs inbound typing
> state from the channel. D22 established that only Chatwoot's web widget supplies it, and D23
> put Chatwoot in phase two — so phase one has **no reachable subject at all**, not a hard one.
>
> It is marked BLOCKED rather than TODO because those mean different things to whoever plans the
> next phase: TODO is work nobody has done, BLOCKED is work nobody can do. Leaving it TODO would
> make a phase look unfinished forever and eventually get it "closed" by someone who did not read
> this far.

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

> **The F2, F3 and F7 acceptance criteria are proved, and how they were proved is the part
> worth keeping.** Five of these tests shipped `@pytest.mark.skip` from the beginning, and
> the note above says why that mattered: *a phase whose own acceptance criterion is an
> unowned skipped test cannot be finished, only declared finished.*
>
> The crash test kills a **real subprocess** parked inside `_step_deliver`. On restart the
> ledger reads `tool=1, deliver=2` — the step that had been recorded replayed, the one that
> had not was re-executed. So recovery genuinely happens, and since the test goes through
> `dbos_config`, unpinning [`t-f2-12`](#t-f2-12)'s `application_version` turns it red. That
> is the only real proof that fix works.
>
> Teeth were shown by three production mutations, each restored byte-exact: removing
> `@DBOS.step()` from `_step_start` ran the tool twice; dropping `signal_decision`'s
> `idempotency_key` resolved the same call twice; making `_step_drain_pending` replace
> instead of extend lost the first sentence.
>
> **And the idempotency mutation passed on the first try.** A turn that finishes on its
> first resume never re-reads the durable topic, so the duplicate is unread *whatever the
> code does*. Rather than accept the green, the test was rewritten to drive a SECOND
> suspension — the only place a duplicate is observable — and the reason is in its
> docstring. A mutation that fails to kill a test is not always a weak test; sometimes it
> means the scenario never reached the code. Finding out which is the whole job.

> **`t-f2-05` is BLOCKED, and D19 prescribes a shape the dependency does not have.** On the
> installed dbos 2.31.1, `queue_partition_key` and `deduplication_id` cannot be set on the
> same enqueue — `Queue._validate_enqueue` raises *"Deduplication is not supported for
> partitioned queues"*, and `Debouncer` refuses the identical pair saying *"partitioned
> queues do not support the deduplication a debounce requires"*. D19 asks for both at once.
> The full evidence, and what it retracts, is in `docs/FIELD-NOTES.md`.
>
> The escape is the shape `Debouncer` itself uses — a separate non-partitioned window queue
> whose debounced workflow enqueues the real turn onto the partitioned queue on expiry — and
> that workflow lives in `turn_workflow.py`, which `t-f2-05` does not own. So this is the
> same defect the note above describes for `t-f2-04`, for the third time: **one anchor
> naming one file and requiring an activity in another.** The window half is
> [`t-f2-11`](#t-f2-11); `t-f2-05` keeps the `routes.py` half and unblocks when it lands.
>
> Two things were done rather than deferred, and both are worth copying. The implementation
> was written and then **reverted**: shipping code that always raises would have looked like
> progress in the diff and failed on the first real message. And the two tests stayed in
> `test_coalescing.py` as **strict xfail**, so they turn green — and stop being xfail — the
> day `t-f2-11` lands, instead of waiting for someone to remember to re-enable them. A
> blocked anchor with a live test is honest; a blocked anchor with a deleted test is a
> promise nobody is holding.
>
> `t-f2-11` and `t-f2-10` must also settle one question together, or they will disagree
> silently: does the drained buffer REPLACE `request.input`? A debounce bounce keeps the LAST
> message's arguments and `return-existing` keeps the FIRST. They need the same answer, or
> the drain either duplicates a sentence or loses one.

> **The flaky concurrency test was a measuring instrument, not a race.** `dbos.Queue` polls
> once a second, so two enqueues straddling a tick are dequeued about a second apart and a
> fixed 0.5s hold can never overlap. The old test was measuring scheduler jitter and calling
> it parallelism. Timing is gone from it: parallelism is now proved by a **rendezvous** —
> every turn blocks until three distinct partitions are in flight — and serialisation by an
> **occupancy count** taken on enter and leave. Both halves were proven falsifiable against a
> deliberately wrong copy before being trusted, which is the step that separates a guard from
> a decoration.

---

---

## F11 · Cold start and the operator surface

Done when a fresh clone with an empty PostgreSQL instance and a credentials file starts
with one command, and an operator can create an agent, give it tools, watch it be refused
one, approve that refusal, and read the whole exchange in the audit trail — **without
writing a line of SQL**. See `docs/ROADMAP.md` for why each row below exists.

### Cold start

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-01"></a>t-f11-01 | **The process never reads `.env`.** Only tests do, which is why every credential had to be exported by hand. Load it at startup as a FALLBACK — a real environment variable always wins — and never log a value | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f11-02"></a>t-f11-02 | Optional `AGENT_CORE_ADMIN_DATABASE_URL`. Present: `ensure_databases` runs before migrations, idempotently. Absent: migrations only, and a missing database fails naming the exact command. **The app must never REQUIRE superuser** | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f11-03"></a>t-f11-03 | **Policy as reviewed configuration.** Nothing in production writes `policy_rules`; only tests do. Rules live in `Core/policy/*.yaml` and are applied at startup like a migration — declarative, idempotent, reviewed. NOT a console command: that would be non-negotiable #9's widening | `adapters/driven/policy_fs/loader.py` | **DONE — RECONCILES, so a rule deleted from the file stops being in force** |
| <a id="t-f11-04"></a>t-f11-04 | A startup **preflight** that names everything unreachable in ONE place — database, model endpoint, credential, and any profile that cannot be served — instead of failing one layer at a time, three commands apart | `adapters/driving/cli/preflight.py` | **DONE — reports every gap in one pass, and names credentials never values** |

### What blocks the console today

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-05"></a>t-f11-05 | `fraud_analyst` loads, assigns a version, and **dies on every turn** at `StartTurn` step 3: `fraud` is absent from `DEFAULT_TOOL_PACKAGES`. Register it, and make an unservable profile fail at LOAD rather than at turn three — a profile that parses is not a profile that works | `Core/src/agent_core/composition.py` | **DONE — an unservable profile now fails at LOAD, naming profile and toolset** |
| <a id="t-f11-06"></a>t-f11-06 | **`PgToolPolicy.decide` reports "the policy store was unreachable" whenever the snapshot is empty.** A reachable, empty `policy_rules` therefore tells the model and the operator the database is down. The verdict is right — fail closed — and the explanation is false, and that explanation is what a human reads to diagnose | `adapters/driven/persistence_pg/policy_repository.py` | **DONE — the verdict never moved; only the explanation did** |
| <a id="t-f11-07"></a>t-f11-07 | `audit_tool_calls` has no `reason` column, so the trail records WHICH rule won and never WHAT IT SAID — not the sentence the model gets on DENY, nor the one a human gets on NEEDS_APPROVAL. Migration `0022` | `adapters/driven/persistence_pg/audit_repository.py` | **DONE — migration `0022`, nullable with no default; a backfilled constant would lie about every older denial** |
| <a id="t-f11-08"></a>t-f11-08 | **There is no audit-read port.** `AuditSink` is write-only by design and `TranscriptReader` projects `transcript_entries`, which nothing in the tree writes. The console declares its own protocol and `main.py` binds a third driven adapter because `Container` has no seat | `ports/audit_reader.py` | **DONE — the container seat is [`t-f11-18`](#t-f11-18)** |

### Making, using and judging an agent from the terminal

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-09"></a>t-f11-09 | `:new <id>` scaffolds a profile from a template and `:reload` re-reads the directory without restarting the process — an agent is EDITED far more often than it is invented | `adapters/driving/cli/console.py` | **DONE — but see [`t-f11-33`](#t-f11-33): unreachable from the shipped process** |
| <a id="t-f11-10"></a>t-f11-10 | The approval path from the terminal: `:pending`, `:approve <id>`, `:refuse <id>`. **D25's four-eyes rule applies here and will usually REFUSE**, because the operator asking is the operator approving — it must say so by name rather than appear broken | `adapters/driving/cli/console.py` | **DONE — but see [`t-f11-33`](#t-f11-33): unreachable from the shipped process** |
| <a id="t-f11-11"></a>t-f11-11 | Show a tool call's ARGUMENTS and its RESULT, not just its name — and render untrusted-content wrapping AS wrapping, so an operator can see the boundary that defends non-negotiables #4 and #10 rather than take it on faith | `adapters/driving/cli/console.py` | **DONE — but see [`t-f11-33`](#t-f11-33): unreachable from the shipped process** |
| <a id="t-f11-12"></a>t-f11-12 | `:sessions` and `:resume <id>`, so a conversation can be picked up. A console that can only start fresh cannot test compaction, which needs a long one | `adapters/driving/cli/console.py` | **DONE — but see [`t-f11-33`](#t-f11-33): unreachable from the shipped process** |
| <a id="t-f11-13"></a>t-f11-13 | `:trace [user\|admin]` — the last turn as the `TranscriptReader` projects it, in EITHER audience. An operator seeing exactly what a USER would have seen is the only practical check on non-negotiable #11 | `adapters/driving/cli/console.py` | **DONE — but see [`t-f11-33`](#t-f11-33): unreachable from the shipped process** |

> **Five anchors write `console.py`, so they are five waves — or one task.** They are one
> task. `t-f11-09` through `t-f11-13` are a single surface being finished, they share its
> rendering, and splitting them across five barriers would cost four waves to avoid a
> collision that a single agent does not have. `docs/WAVES.md` rule 2 asks whether two
> tasks write the same file; the answer here is to stop making them two tasks.

### Relationships, orchestration, and the acceptance run

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-14"></a>t-f11-14 | **A profile cannot name a peer.** `_build_peer_policy` excludes `peers` from the accepted keys — "no loader yet" — so `PeerPolicy.peers` is always empty, and empty means NOBODY. Every A2A mechanism in the tree is unreachable from configuration | `domain/profile.py` | **DONE** |
| <a id="t-f11-15"></a>t-f11-15 | A shipped relationship, as configuration: two profiles that genuinely ask each other, with the **two-sided** allowlist and the hop limit in force. One agent orchestrating another must be a YAML change, not a code change — that is the claim, and this anchor is the proof | `Core/profiles/support_triage.yaml`, `Core/profiles/billing_specialist.yaml` | **DONE** |
| <a id="t-f11-16"></a>t-f11-16 | An MCP server configured in a profile and reached end to end: its tools arrive `mcp_*`-prefixed, obey the same `ToolPolicy`, get the smaller result budget, and come back wrapped as untrusted content. `mcp_servers: []` in both shipped profiles today | `Core/profiles/delivery_optimizer.yaml` | **DONE — teeth proven by mutating `UNTRUSTED_PREFIXES`** |
| <a id="t-f11-17"></a>t-f11-17 | **The acceptance run.** From an empty PostgreSQL instance: one command starts it, an operator creates an agent, gives it a tool, watches a second tool refused, approves that refusal, has one agent orchestrate another, and reads the whole exchange in the trail — scripted, so the claim is checked rather than remembered | `Core/tests/integration/test_cli_acceptance.py` | **DONE — 9 passed, 4 strict xfail, each naming a gap rather than weakening an assertion** |
| <a id="t-f11-31"></a>t-f11-31 | **The operator runbook.** Starting this system from zero is currently reconstructable only from a conversation. Write it down: what to set, what each subcommand does, what the preflight will tell you, and what to do about each thing it names | `README.md` | **DONE** |

### What wave 1 surfaced

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-18"></a>t-f11-18 | Bind the seats wave 1 built: `audit_reader` on `Container` (over the **audit** pool, never the domain one), and retire `main._PgToolCallLog` — the third driven adapter `main.py` hand-builds because the container has no seat | `Core/src/agent_core/composition.py` | **DONE** |
| <a id="t-f11-19"></a>t-f11-19 | **`composition._decision_signal` still raises `NotImplementedError`**, so an approval recorded from anywhere never wakes its turn. D25 records the decision and the turn waits forever | `Core/src/agent_core/composition.py` | **DONE — proved by a real DBOS wait woken through the container** |
| <a id="t-f11-20"></a>t-f11-20 | **The bootstrap creates a database DBOS never opens.** `ensure_databases` makes `<app>_dbos` — the repo-wide convention — while dbos 2.31.1 derives its system database as `<app>_dbos_sys` from the app URL and creates that itself. One is unused; the other is created by nobody who meant to | `adapters/driven/persistence_pg/migrations.py` | **DONE** |
| <a id="t-f11-21"></a>t-f11-21 | **`audit_tool_calls` has no `tenant_id`**, so `AuditReader` can be scoped only to a turn. The fix is NOT a join to `turns`: the audit write is outside the domain transaction ON PURPOSE, so a turn whose domain writes rolled back has audit rows and no `turns` row — an inner join would hide exactly the rows non-negotiable #6 exists to preserve. A column, written by the sink from `caller.tenant_id`. Migration `0023` | `adapters/driven/persistence_pg/audit_repository.py` | **DONE — a column, never a join; migration `0023`** |
| <a id="t-f11-22"></a>t-f11-22 | `migrations.run_migrations` applies only F1's `APP_MIGRATIONS`, so any database built with it lacks migration `0012`'s `tenant_id` and **every policy SELECT fails with `UndefinedColumn`** — which the adapter then correctly reports as an unreachable store. `test_policy_tenant_sql.py` builds its fixture that way and is one query from testing a broken schema | `adapters/driven/persistence_pg/migrations.py` | **DONE** |

### What wave 2 surfaced

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-23"></a>t-f11-23 | **A credential in `.env` is invisible to the model client.** `Settings.from_env` layers the file under the environment for ITS OWN fields and exports nothing, while litellm resolves `MINIMAX_API_KEY` from `os.environ` — and the file carries the value as `MINIMAX_API`. The preflight reports it; nothing maps it | `Core/src/agent_core/composition.py` | **DONE — code landed before the session limit; the 9 tests that pin it are green** |
| <a id="t-f11-24"></a>t-f11-24 | The checked-in `.env` points `AGENT_CORE_DATABASE_URL` at the remote instance, where `agent_core_app` does not exist — so `python -m agent_core serve` cannot start from the credentials file as it stands. Decide what a checked-in default should point at, and make the failure say which | `.env.example` | **DONE** |
| <a id="t-f11-25"></a>t-f11-25 | `MCPServerRef.result_budget_chars` is **inert configuration**: the runner budgets by name prefix (`budget_for`) and nothing calls `mcp_toolsets.result_budget_for`, so a per-server number in a profile changes nothing. A knob that does not turn is worse than no knob | `adapters/driven/agent_pydantic/runner.py` | **DONE — a profile may only ever LOWER the ceiling, never raise it** |
| <a id="t-f11-32"></a>t-f11-32 | **`MCPServerRef.result_budget_chars` validates nothing.** It was harmless while the number was inert; now that `t-f11-25` consults it, a negative value makes the truncation NOTICE lie: `omitted = len(body) - budget` claims more characters were removed than the body ever held. **Corrected: `body[:-100]` keeps the HEAD, not the tail — this anchor said the opposite and was wrong** | `domain/profile.py` | **DONE** |
| <a id="t-f11-26"></a>t-f11-26 | Tests pinning the schema truths `t-f11-20`/`t-f11-22` moved: `test_migrations.py` asserts the `_dbos` name nothing opens and asserts the applied set equals `APP_MIGRATIONS`; `test_audit_repository.py`'s fixture writes a `turns` row **production can never write** (no `profile_version`, NOT NULL since `0009`) and passed only because `run_migrations` skipped `0009`; `test_cold_start.py` asserts the old database names | `Core/tests/integration/test_migrations.py` | **DONE** |
| <a id="t-f11-27"></a>t-f11-27 | `test_profile.py` asserts `model == "claude-sonnet-5"` against a profile now pointing at `minimax/MiniMax-M3`. A test pinning a value that is deployment configuration, not behaviour | `Core/tests/unit/test_profile.py` | **DONE — it was pinning deployment configuration, not behaviour** |

### The two that block the goal

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-28"></a>t-f11-28 | **`LocalToolProvider` cannot compose an MCP server**, so a profile that declares one makes `preflight` refuse to start the whole process. `t-f11-16` put a real server in `delivery_optimizer.yaml` and the F6 machinery has no path from a profile to a running toolset | `adapters/driven/tools/provider.py` | **DONE** |
| <a id="t-f11-29"></a>t-f11-29 | **Nothing in production ever writes a `ModelResponse`.** `TurnOutcome` carries no messages, so a stored history is the user's prompts and nothing else — the agent's own replies are never persisted. A conversation with no agent turns in it is not a conversation | `adapters/driven/agent_pydantic/runner.py` | **DONE** |


### What the acceptance run found

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-33"></a>t-f11-33 | **The console surface is finished and unreachable from the shipped process.** `main.build_console` binds no `profiles_dir`, `load_profiles`, `approvals` or `transcripts`, and `Container` has no transcript-reader seat — so `:new`, `:reload`, `:approve`, `:refuse`, `:sessions` and `:trace` all answer "this console has no … wired" | `Core/src/agent_core/main.py` | TODO |
| <a id="t-f11-34"></a>t-f11-34 | **Orchestration is unreachable too.** `support_triage` names `billing_specialist` and resolves to NO `ask_peer` tool: `TOOL_PACKAGES` is `{delivery, fraud}` and nothing wires an `AgentMailbox` into the container. Six F9 anchors plus `t-f11-15` are reachable only from unit tests | `Core/src/agent_core/composition.py` | TODO |
| <a id="t-f11-35"></a>t-f11-35 | `audit_repository.ARGUMENT_ALLOWLIST` is `{}`, so every call in the trail renders "arguments: none recorded" — `t-f11-11`'s "show a call WHOLE" can never show two of its three parts | `adapters/driven/persistence_pg/audit_repository.py` | TODO |
| <a id="t-f11-36"></a>t-f11-36 | `preflight.py` restates `_DBOS_DATABASE_SUFFIX = "_dbos"`, the name `t-f11-20` retired: a correctly bootstrapped instance is warned that `<app>_dbos` is missing and handed a remedy creating a database nothing opens, while `<app>_dbos_sys` is never checked | `adapters/driving/cli/preflight.py` | TODO |
| <a id="t-f11-38"></a>t-f11-38 | **`preflight` exits 0 with a FAIL check.** It printed "1 not ready" and returned success, so a deploy script that gates on it gets a green light on a deployment that cannot serve a turn | `adapters/driving/cli/preflight.py` | TODO |
| <a id="t-f11-39"></a>t-f11-39 | **A shipped profile points its MCP server at a TEST FIXTURE**, `Core/tests/fixtures/mcp_echo_server.py`, by a path relative to the working directory — so it resolves only when the process is started from one place, and it ships a test as production configuration | `Core/profiles/delivery_optimizer.yaml` | TODO |
| <a id="t-f11-40"></a>t-f11-40 | A raw `ExceptionGroup` traceback is printed to the operator's terminal when an MCP server does not answer. The `[warn]` line beside it is correct and sufficient; the traceback makes a handled, documented degraded start look like a crash | `adapters/driven/mcp/toolsets.py` | TODO |
| <a id="t-f11-41"></a>t-f11-41 | **A deferred tool call crashes the turn.** With `ask_peer` allowed, the model calls it and the runner raises `UserError: A deferred tool call was present, but DeferredToolRequests is not among output types`. Neither `ask_peer` nor `request_evidence` — the two shared deferred mechanisms — has ever left a turn in production | `adapters/driven/agent_pydantic/runner.py` | **DONE — `output_type=[str, DeferredToolRequests]`; an inline handler would have to hold a coroutine open across a redeploy** |
| <a id="t-f11-42"></a>t-f11-42 | **The ask never leaves.** Nothing turns a deferred `ask_peer` call into `AgentMailbox.ask()`, so the correlation id is never minted or persisted and the question never reaches the queue. `TurnWorkflowDependencies` has no mailbox seat to bind | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f11-43"></a>t-f11-43 | **Nobody runs the peer's turn.** The mailbox has an exactly-once `claim_next` and no consumer: a driving adapter must claim an ask, run a turn as the TARGET agent under its own profile and policy, and `answer()` — the sibling of `scheduler/cron.py`, and the piece nothing in this build anticipated | `adapters/driving/peers/worker.py` | **DONE — runs the turn as the TARGET agent** |
| <a id="t-f11-44"></a>t-f11-44 | **The answer never comes back.** Nothing calls `read_answer` in production, so even a claimed and answered ask never resumes the turn that is waiting for it — under the provider's original `tool_call_id`, verbatim | `adapters/driving/workflow/turn_workflow.py` | **DONE** |
| <a id="t-f11-45"></a>t-f11-45 | `PolicyEnforcement` **blocks** on NEEDS_APPROVAL instead of raising `ApprovalRequired`. That was `t-f3-02`'s open decision because the output type did not exist; it exists now, so an approval can suspend through the same deferred path a peer ask does | `adapters/driven/agent_pydantic/runner.py` | **DONE — an approval now suspends through the same deferred path a peer ask does** |
| <a id="t-f11-46"></a>t-f11-46 | Two words that mislead a reader: the console labels a DEFERRED call `- executed` (the sink records intent and verdict before execution, correctly — but a call that deferred never ran), and `start_turn._notice_for` overwrites a DELEGATION's reason for **every** audience, so an admin can never see the raw ask | `adapters/driving/cli/console.py` | **DONE** |
| <a id="t-f11-47"></a>t-f11-47 | **`max_hops` is not enforced.** `TurnRequest` has no seat for `hop`, so an answering turn starts at 0 and a cycle A→B→A resets the counter at every hop. Both allowlists and both `enabled` switches ARE enforced — this is the one half that is not | `domain/turn.py` | **DONE — `hop: int = 0`, and the default is a statement: a turn no peer delegated is at depth zero** |
| <a id="t-f11-48"></a>t-f11-48 | **`PeerAsk` carries no asking `AgentId`**, so the ANSWERING side cannot re-run its own `may_ask` check on arrival. The asking side's gate still applies; this is the defence in depth that is missing, and it needs a column on `peer_messages` | `adapters/driven/peers/mailbox.py` | **PARTIAL — column, adapters and seat landed; the port widening is [`t-f11-50`](#t-f11-50)** |
| <a id="t-f11-49"></a>t-f11-49 | The loop is wired and **cannot complete**: `main.py` leaves the wake seat unbound, `rules.yaml` has no rule a peer turn can reach (it runs as subject `peer-agent` on channel `peer`, so every tool is denied and B answers from its persona alone), and `test_durability.py` binds no `peers` seat | `Core/src/agent_core/main.py` | **DONE — but see [`t-f11-51`](#t-f11-51): nothing a human can type reaches the loop** |
| <a id="t-f11-50"></a>t-f11-50 | Finish `t-f11-48`: widen `AgentMailbox.ask` with the `asker` seat, **and every stub and call site in the same change** — mypy rejects an implementation missing even a DEFAULTED protocol keyword, so the port cannot move alone. `_step_ask_peers` already computes the caller and drops it | `ports/agent_mailbox.py` | TODO |
| <a id="t-f11-51"></a>t-f11-51 | **Nothing a human can type reaches the peer loop.** `_step_ask_peers` lives in the DBOS workflow; the console calls `StartTurn` DIRECTLY, so an `ask_peer` from the console suspends and no mailbox is ever asked. The only path through the workflow is `POST /turns` on channel `http`, where the `ask_peer` rule does not match — and no `cli` channel is registered, so a workflow turn on `cli` dies in `_step_deliver` | `adapters/driving/cli/console.py` | TODO |
| <a id="t-f11-37"></a>t-f11-37 | **`max_cost_usd` accepts a non-finite `Decimal` and fails OPEN.** `Decimal("nan")` parses, every comparison against NaN is False, so `cost_exhausted` can never be True and the agent has NO SPEND LIMIT. This file already rejects non-finite literals in approval conditions and never applied the rule to its own money field | `domain/profile.py` | TODO |






> **`t-f11-51` is the tenth instance, and the purest one yet: the loop works and nothing a
> human can type reaches it.** Every move is wired, tested and green end to end — the ask
> leaves, a worker runs the answering agent under its own identity, the answer comes back
> under the provider's original `tool_call_id`, and the hop count travels. The test that
> proves it drives the workflow directly.
>
> An operator has two doors and neither opens. The console calls `StartTurn` directly — which
> is deliberate, documented in its own banner, and the reason a REPL answers immediately — so
> the workflow step that asks the mailbox never runs. `POST /turns` does go through the
> workflow, and arrives on channel `http`, where the `ask_peer` rule scoped to `cli` does not
> match. Two correct decisions that do not compose.
>
> Whoever closes this is making a real choice, not a wiring fix: either the console gains a
> durable path through the workflow — and its banner stops being true in the way it is true
> today — or the grant names the channel the workflow actually serves, and an operator tests
> delegation over HTTP rather than at the terminal they asked for. Write down which, and what
> it costs.

> **`t-f11-47` is the honest half of a security control, and the agent said so rather than
> implying the whole thing worked.** The two-sided allowlist and both `enabled` switches are
> enforced exactly as written — an agent cannot ask a peer neither side named. What is NOT
> enforced is the DEPTH: `hop` is turn-level state, `TurnRequest` has no seat for it, so the
> answering turn starts at zero and A→B→A→B never trips `max_hops`.
>
> The failure it allows is a loop between two agents that each legitimately allow the other —
> not an unauthorised call, but an unbounded authorised one, which is a bill and a hung
> conversation rather than a breach. Worth knowing precisely, which is why it is written this
> way instead of as "hop limit: done".

> **`t-f11-42`..`t-f11-44` are one loop, and the middle one is the piece nobody anticipated.**
> F9 built the durable mailbox, the hop limit, the two-sided allowlist, the A2A adapter and
> `ask_peer` as a shared deferred tool. `t-f11-14` made a profile able to name a peer. All of it
> is the ASK side and the TRANSPORT — and **nothing ever runs the answering agent's turn.**
> `PgAgentMailbox` has an exactly-once `claim_next` with a concurrent-claim test behind it, and
> no consumer: the queue was built for a worker nobody wrote.
>
> It is a DRIVING adapter, the sibling of `scheduler/cron.py`: it claims work from outside and
> calls a use case, and it must run the turn as the TARGET agent — B's profile, B's toolset,
> B's policy, B's budget. Running it under A's identity would make delegation a privilege
> escalation dressed as a question, which is the one thing the two-sided allowlist exists to
> prevent.
>
> And B may itself suspend — to ask a human, or another peer. So the worker cannot assume an
> answer arrives within one turn; that is why `ask` and `answer` are separated by a durable
> queue rather than by a function call.

> **`t-f11-41` is the ninth instance, and it was one policy rule away the whole time.**
> `ask_peer` resolved into the toolset, the profile named its peer, the mailbox was bound — and
> the tool was `deny [no matching rule]`, so the model was never offered it and the deferred
> path was never walked. Granting it in YAML, which is the thing F11 claims should be enough,
> is what reached the code nobody had run.
>
> Every test that exercised a deferred tool built the deferred result itself and handed it back
> — `t-f3-10` resumes from a `ToolResolution`, `t-f9-04` asserts the suspension shape. None of
> them asked Pydantic AI to PRODUCE one, because none of them ran an agent whose toolset
> actually contained a deferred tool the model was allowed to call.

> **`t-f11-38` is the one that would have shipped.** The preflight was built because every
> failure in this phase surfaced one at a time, three commands apart — and it answers that
> beautifully, in one pass, naming the host, the port, the database and the exact remedy. Then
> it returns 0. **A readiness check whose exit code does not carry its verdict is a readiness
> check nothing can gate on**, and the first thing anyone does with one is put it in front of a
> deploy. The report was right and the only machine-readable part of it was wrong.

> **`t-f11-33` and `t-f11-34` are the seventh and eighth instances of one shape, and they are the
> two that matter most.** Every command an operator would use to MAKE an agent — `:new`,
> `:reload` — and every command to JUDGE one — `:approve`, `:trace`, `:sessions` — is written,
> tested and green, and answers "not wired" in the shipped process. The same is true of every A2A
> anchor: six of them, plus the two profiles that finally made a relationship expressible, and
> nothing hands the container a mailbox.
>
> They were invisible for the same reason all six earlier instances were: **each unit test
> supplied the collaborator itself.** `test_console.py` passes a `load_profiles`; the peer tests
> pass a mailbox. The acceptance run is the first thing that drives the process the way an
> operator will, which is exactly why it was written as a script — and it found both on its first
> execution.
>
> Note how it reported them: **four strict xfails naming the gaps, not four weakened assertions.**
> A strict xfail turns green the day the gap closes and fails if it is silently "fixed" by
> deleting the test. That is the difference between a test that documents a gap and a test that
> hides one.

> **`t-f11-37` is worse than the budget bug that uncovered it, and worse in the way that matters.**
> A profile writing `max_cost_usd: nan` loads cleanly and carries `Decimal('NaN')`. Every
> comparison against NaN is False, so `cost_exhausted` is permanently False and the agent runs
> with **no spend limit at all** — silently, unboundedly, surfacing only on the bill, which is
> exactly the failure mode `CLAUDE.md`'s silent-bug table names.
>
> `domain/profile.py` already knows this rule. `_decimal`'s own docstring rejects non-finite
> literals in approval conditions because *"a condition that silently never matches is precisely
> the fail-open this module exists to prevent."* The rule was written down, and the file's own
> money field was never held to it.

> **`t-f11-32` is what closing `t-f11-25` bought, and it is worth seeing the shape.** An
> unvalidated number sat in a profile for as long as nothing read it, and validating it would
> have looked like ceremony. The moment a knob starts turning, every value it can hold becomes
> reachable — and this one reaches somewhere bad: a negative budget keeps the TAIL of an
> omission count lies about how much. **Inert configuration is unvalidated configuration
> waiting for its first reader.**
>
> **And the anchor's own reasoning was wrong, which the implementing agent checked rather than
> took.** It claimed a negative slice keeps the TAIL, where an injection puts its instructions.
> `'0123456789'[:-3]` is `'0123456'`: it keeps the HEAD. The defect is real for a different
> reason — `omitted = len(body) - budget` with `budget=-100` reports `len(body) + 100` removed,
> and that notice sits OUTSIDE the delimiters precisely so a reader can trust our accounting
> over the payload's imitation of it. Here our accounting is the part that lies. `budget=0`
> empties every fence instead, which is `tool_exclude`'s job done invisibly. Both refuse at load
> now. **An anchor is a claim, not an instruction — verify it before implementing it.**

> **`t-f11-29` is the last and largest instance of the shape this whole build kept hitting**, and
> it hid behind the one that came before it. `t-f1-24` found the store and the runner disagreeing
> about how a history is encoded; fixing that made a second turn work, and made this visible
> underneath — the history round-trips correctly and it is only ever half a conversation.
>
> Every test that needed an agent reply put one there itself. `test_history_round_trip.py` drives
> two real turns and asserts the model was handed the earlier question back, which is true and is
> the user's half. The agent's half has no writer at all, because `TurnOutcome` — the one thing
> that crosses from the runner back to the use case — carries a result and a pending list and no
> messages.
>
> Whoever closes it must also answer what a `ThinkingPart` does on the way out: reasoning is
> admin-only (`t-f10-04`), `transcript_entries` now projects a serialised `ModelMessage`, and the
> moment a `ModelResponse` is stored, a reasoning block is one unnamed projection away from a USER
> read. The module docstring already records that; this is the anchor that makes it urgent.

> **Two migrations took the id `0023`, and nothing said so.** `audit_tenant_migration.py` took it
> outside the table at the same time the table allocated it, and `DuplicateMigrationIdError`
> compares WHOLE ids — so `0023_audit_tenant` and `0023_something_else` coexist silently, which is
> the exact failure the pre-allocation table exists to prevent, one character narrower than the
> check that guards it. The table below is corrected and extended; the check comparing whole ids
> rather than the numeric prefix is [`t-f11-30`](#t-f11-30).

| Anchor | Task | File | Status |
|---|---|---|---|
| <a id="t-f11-30"></a>t-f11-30 | `DuplicateMigrationIdError` compares whole ids, so two migrations sharing a numeric prefix pass discovery. Compare the prefix — that number is the allocation, and the suffix is a comment | `adapters/driven/persistence_pg/migrations.py` | **DONE** |

> **`t-f11-26` is the same trap twice, mirrored, and it is worth reading before writing the fix.**
> `run_migrations` applied only F1's list, so a fixture built on it silently lacked `0009` and
> `0012`. One test then wrote a `turns` row with no `profile_version` — **a row production can
> never write**, because `0009` made that column NOT NULL — and passed for months. The other
> recorded a tool call whose INSERT names `reason` (`0022`) against a genuinely fresh database and
> would have raised `UndefinedColumn`; it passed only on a developer machine where the test
> database was left over from an earlier run.
>
> Both are the same shape: **a fixture that builds a schema production does not have is a test
> asserting against a system that does not exist.** The fix is not to patch the fixtures. It is
> that `apply_all_migrations` is the only applier, and a second partial one that looks like the
> real thing is a trap — "it is only used by tests" is what that trap says right before someone
> uses it.

> **`t-f11-21` is the shape of a good "no".** The obvious fix — join `audit_tool_calls` to
> `turns` for the tenant — is wrong for a reason that only shows up under failure, and the agent
> that found it said so instead of taking it. Non-negotiable #6 puts the audit write OUTSIDE the
> domain transaction precisely so that a turn whose domain writes rolled back still leaves a
> trace. An inner join would then hide exactly those rows: the trail would be complete on every
> successful turn and silently missing on every failed one, which is the opposite of what an audit
> log is for. **A fix that works on the happy path and erases evidence on the sad one is worse
> than the gap it closes.**

> **`t-f11-14` is why nothing orchestrates, and it reads as a small omission.** It is not.
> `PeerPolicy.may_ask` is an ALLOWLIST — `t-f9-01` pinned that empty means nobody, deliberately,
> because a permission model whose default is "anyone" is not a permission model. The loader then
> excluded the one field that can populate it, with a comment saying a profile setting it should
> fail loudly rather than be silently dropped. **That was the right call at the time** and it is
> exactly why this is safe to close now: nobody has been quietly relying on a half-working peer
> list, because a profile that named one refused to load at all.
>
> Everything downstream is already built and already tested — `may_ask`, the hop limit, the
> two-sided check, the durable mailbox, the A2A adapter, `ask_peer` as a shared deferred tool, the
> untrusted-content wrapping of a peer's answer. Six anchors of machinery reachable only from a
> test. This is the seat they were all built for.

> **`t-f11-17` is the anchor that decides whether F11 is finished**, and it is deliberately a
> script rather than a checklist. Every other row in this phase exists because a human performed a
> step by hand and nothing complained; a phase that closes those gaps and then verifies itself by
> hand has not learned the lesson it was created by. The run must start from an EMPTY database, so
> that a step quietly depending on state left by a previous run fails here rather than on the first
> machine that is genuinely fresh.

> **Why `t-f11-04` exists as its own anchor.** Every failure in the list at the top of
> F11's roadmap section surfaced one at a time, each three commands after the last: the
> database refused, then the credential was missing, then the model id did not resolve,
> then the policy table was empty, then the toolset was unregistered. Each fix revealed the
> next. A preflight that answers "what is not ready" in one pass is worth more than any of
> the five individual fixes, because it is the thing that stops the sixth from being
> discovered the same way.

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
| `0017` | [`t-f1-22`](#t-f1-22) | turn outcome — **taken outside this table**, see below |
| `0018` | [`t-f2-04`](#t-f2-04) | index widening: `(tenant_id, session_id, id)` on `pending_inputs`, replacing the session-only index |
| `0019` | [`t-f3-14`](#t-f3-14) | `audit_rejected_decisions` — the table the sink member has no home in yet |
| `0020` | [`t-f10-10`](#t-f10-10) | a `created_at`-leading index for D27's retention sweep |
| `0021` | [`t-d2-05`](#t-d2-05) | pgvector embedding column + index for `SEMANTIC` retrieval |
| `0022` | [`t-f11-07`](#t-f11-07) | `reason` on `audit_tool_calls` — what the winning rule actually said |
| `0023` | [`t-f11-21`](#t-f11-21) | `tenant_id` on `audit_tool_calls` — scoping a read without an evidence-erasing join |
| `0024` | [`t-f1-24`](#t-f1-24) | `messages` re-encoded as `ModelMessage`; the unreadable legacy rows are deleted, not migrated |
| `0025` | [`t-f11-48`](#t-f11-48) | `from_agent_id` on `peer_messages` — who asked, so the answering side can refuse |
| `0026` | *unallocated* | held free |
| `0027` | *unallocated* | held free |
| `0024` | *unallocated* | held free |

An id is spent when it is written here, not when the table ships. An anchor that turns out
not to need a table leaves its id retired rather than recycling it — a reused id against a
database that already recorded the first one is a migration that silently never runs.

> **The table ran out, and that is the table working.** All of `0010`–`0016` were spent by
> the wave-12 barrier, and `0017_turn_outcome` had been taken in
> `conversation_repository.py` **without being written here** — so the next agent needing an
> index found no free id and, correctly, **refused to invent one**. It reported the block
> instead of reaching for `0018`, which is precisely the collision this table exists to
> prevent: two agents running in parallel both pick "the next free id" and both pick the same
> one.
>
> It also checked the escape hatch and closed it, which is the part worth keeping. Editing an
> already-applied migration in place does NOT work here: `schema_migrations` on the live
> database records `0011_pending_inputs`, and the apply function returns early on that row, so
> a rewritten `0011` would never execute anywhere it had already run. **A migration is
> append-only for the same reason an audit log is.**
>
> `0018` is now allocated above. Two rules earned by this: an id taken outside this table is a
> silent double-allocation waiting to happen — `0017` is recorded retroactively so it cannot
> be handed out twice — and **running out is a signal to allocate more, never a reason to
> reuse**.
