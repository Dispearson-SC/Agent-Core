# Field notes

Facts about the dependencies, verified against the versions actually installed rather than
against documentation or memory. `docs/TASKS.md` warns in more than one place that a
signature was assumed and never checked; this file is where the checking goes.

Each entry names the version it was verified on. When a version moves, re-verify before
trusting the entry — an outdated note here is worse than no note, because it reads as
authority.

---

## DBOS 2.31.1

`docs/TASKS.md` F2 carries the warning: *"Verify `DBOS.recv()` / `DBOS.send()` signatures
against the installed version before coding. Confirmed the durable-notification primitives
exist; the exact signatures were not verified."* They are verified now.

### The primitives

```python
DBOS.recv(topic: str | None = None, timeout_seconds: float = 60) -> Any
DBOS.send(destination_id: str, message: Any, topic: str | None = None, *,
          idempotency_key: str | None = None, send_to_forks: bool = False) -> None
DBOS.start_workflow(func, *args, **kwargs) -> WorkflowHandle[R]
DBOS.set_event(key: str, value: Any) -> None
DBOS.get_event(workflow_id: str, key: str, timeout_seconds: float = 60) -> Any
DBOS.sleep(seconds: float) -> None
```

**`recv` defaults to a 60-second timeout.** That default is a trap for F3. The phase's
criterion is that an approval survives a redeploy — a human might answer tomorrow. Pass an
explicit long timeout, or the durable wait quietly becomes a one-minute wait and the test
that proves it will look fine on a fast machine.

**`send` takes an `idempotency_key`.** F3's "resolving the same request twice is a noop"
does not need to be built by hand.

**`DBOS.step` disables retries by default**: `retries_allowed=False`, and when enabled,
`max_attempts=3`, `interval_seconds=1.0`, `backoff_rate=2.0`. A step is not automatically
retried just because it is a step.

### Use the async variants

The system is async end to end (D13). DBOS ships async counterparts for every primitive the
workflow needs, and these are the ones to call:

```
recv_async   send_async   start_workflow_async   run_step_async
set_event_async   get_event_async   sleep_async   enqueue_workflow_async
get_result_async   retrieve_workflow_async
```

Calling the sync forms from async code is the kind of mistake that works on one machine and
deadlocks on another.

### `Queue` already does D19's partitioning

`t-f2-03` describes a partitioned queue with `partition_concurrency=1` keyed by session id.
`dbos.Queue` takes exactly that:

```python
Queue(name, concurrency=None, *, worker_concurrency=None, global_concurrency=None,
      partition_concurrency=None, partition_worker_concurrency=None,
      partition_limiter=None, priority_enabled=False, partition_queue=False)
```

The partition key travels per-enqueue as `queue_partition_key`, so one queue serves every
session and the per-session serialisation is a parameter rather than a design.

### `Debouncer` already does D19's coalescing

`t-f2-05` describes a coalescing enqueue with `delay_seconds` plus
`deduplication_id` / `return-existing`. `EnqueueOptions` carries every one of those names:

```
workflow_id · delay_seconds · deduplication_id
duplication_policy: Literal["reject", "return-existing"]
queue_partition_key · priority · workflow_timeout · max_recovery_attempts
```

and they can be set around a call with `SetEnqueueOptions(...)`. On top of that:

```python
Debouncer.create(workflow, *, debounce_timeout_sec=None, queue=None)
    .debounce_async(debounce_key: str, debounce_period_sec: float, *args, **kwargs)
```

`debounce_key` keyed by session and `debounce_period_sec` as the window is, directly, the
behaviour `t-f2-07` asserts — three messages inside the window producing one turn and one
model call.

### RETRACTED 2026-09-10 — partitioning and deduplication are mutually exclusive

**This section previously concluded "F2's D19 additions are mostly configuration, not
construction." That is WRONG on the installed version, and it is retracted rather than
edited away, because it was read as authority and a wave was scheduled on it.**

The two configurations above cannot be applied to one queue. `Queue._validate_enqueue`
(`dbos/_queue.py`) raises `Deduplication is not supported for partitioned queues` when
`queue_partition_key` and `deduplication_id` are both set — which is exactly the
combination D19 prescribes. Verified against the installed source and reproduced by a test.

Every route around it is closed too:

- A partitioned queue **requires** a partition key (same function), so the turn cannot be
  enqueued onto it without one.
- `Debouncer` refuses the same pair explicitly, and says why:
  *"partitioned queues do not support the deduplication a debounce requires"*
  (`dbos/_debouncer.py`, `_reject_conflicting_options`).
- A plain `deduplication_id` is released when the workflow **completes**
  (`_sys_db.update_workflow_outcome`), not when the delay expires — only `is_debounced`
  rows are cleared in `transition_delayed_workflows`. So a hand-built two-queue relay would
  make a message arriving MID-TURN join the running turn and vanish, instead of becoming the
  follow-up turn `t-f2-09` asserts.

The only shape left is the one `Debouncer` itself uses: a separate **non-partitioned**
window queue carrying a debounced workflow that, on expiry, enqueues the real turn onto the
partitioned queue. That is now [`t-f2-11`](TASKS.md#t-f2-11), and it lives in
`turn_workflow.py` — not in `routes.py`, which is why `t-f2-05` alone could never have
landed.

**What the retracted claim cost, and the rule it earns.** Both facts above were read
straight from the installed signatures, and both were true in isolation: `Queue` really does
take `partition_concurrency`, `EnqueueOptions` really does carry `deduplication_id`. The
error was combining two verified parameters and calling the combination verified. **A
signature check proves a parameter exists; only running it proves two of them compose.**
Verify combinations against behaviour, not against the argument list.

---

## LiteLLM 1.100.1 and MiniMax

### Names and endpoints

LiteLLM reads the key from **`MINIMAX_API_KEY`** and the base from `MINIMAX_API_BASE`,
defaulting to `https://api.minimax.io/v1` for chat completions. This repository's gitignored
`.env` stores the key under `MINIMAX_API`, so the two names must be mapped; the integration
tests do it at runtime and never write the value anywhere.

Direct chat models known to the registry: `minimax/MiniMax-M2`, `M2.1`, `M2.1-lightning`,
`M2.5`, `M2.5-lightning`, `M3` — all with `supports_function_calling=True` and
`supports_reasoning=True`.

### `reasoning_effort` is rejected unless you allow it

On this version the minimax provider raises `UnsupportedParamsError` for `reasoning_effort`
unless it is passed through explicitly:

```python
litellm.completion(..., allowed_openai_params=["reasoning_effort"])
```

The usual workaround, `drop_params=True`, **silently discards the parameter**. A test that
sets `drop_params=True` to compare a "thinking" run against a default one is comparing two
identical default runs and proving nothing. This is the shape of bug the silent-bug table in
`CLAUDE.md` is about: nothing fails, and the result is meaningless.

### Resolving an endpoint and a credential WITHOUT `litellm.completion`

`litellm.get_llm_provider(model)` returns `(wire_model, provider, key, base)` — but on this
version the last two come back `None` for minimax, so it answers only half the question.
The other half is on the per-provider config:

```python
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager

config = ProviderConfigManager.get_provider_chat_config(
    model="MiniMax-M3", provider=LlmProviders("minimax")
)
config.get_api_base()   # 'https://api.minimax.io/v1', or MINIMAX_API_BASE
config.get_api_key()    # MINIMAX_API_KEY
```

That pair is what `adapters/driven/llm_litellm/models.py` uses to build day-1's model, so
no endpoint and no credential name is hard-coded in this repository. `get_provider_chat_config`
returns litellm's INTERNAL per-provider class; `get_api_base` / `get_api_key` are shared by
convention rather than through a published base class, which is why the adapter reaches
them through a `Protocol` and refuses by name when they are absent.

**Both defaults are dangerous when they are missing.** An OpenAI-compatible client built
with `base_url=None` targets api.openai.com, and one built with `api_key=None` reads
`OPENAI_API_KEY`. Verified on this version: `OpenAIProvider(base_url="https://api.minimax.io/v1",
api_key=None)` produces a client holding the OpenAI key, pointed at MiniMax. Neither fails
a test.

### M3 always reasons

`reasoning_content` comes back populated in **both** modes — 128 characters on a default
call and 91 with `reasoning_effort="medium"` in the same smoke test. `reasoning_effort`
regulates how much thinking happens, it does not switch thinking off. Do not write an
assertion that one mode has reasoning and the other does not; it will be wrong, and it will
be wrong intermittently, which is worse.

### Repeated-character filler makes M3 return an empty answer, nondeterministically

Feeding MiniMax-M3 a long run of one repeated character — `"u" * 8000` as padding to force a
compaction — makes its `ThinkingPart` consume the entire `max_tokens` budget and come back
with an **empty** `TextPart`. Run to run, not every time. In `ModelSummariser` that trips the
empty-answer guard into `_extractive_fallback`, so a test asserting the model path was taken
fails intermittently while the code is correct.

A repeated natural-language SENTENCE does not trigger it. Use natural-language filler in any
test that pads a conversation against a live model. Discovered while writing the summariser's
live-provider check, which is the test that exists because nothing else drives that route.

---

## Gemini, and why a second provider exists at all

`t-later-03` (`classify_error`, the five `RecoveryStrategy` values) is gated on a second
provider being live, and the gate is not bureaucracy. With one provider you cannot tell
`ROTATE_KEY` ("this credential is the problem") from `FALLBACK_MODEL` ("this route is the
problem, the key is fine"), because there is nowhere to fall back to. Written against one
provider, the mapping overfits to that provider's error shapes and is wrong the day a second
one arrives.

Verified working: **`gemini/gemini-3-flash-preview`**, tool calling confirmed, key read from
`GEMINI_API_KEY`.

**The model list lies.** `GET https://generativelanguage.googleapis.com/v1beta/models`
returns models this account cannot actually call. `gemini-2.5-flash` and `gemini-2.0-flash`
both appear in `supportedGenerationMethods: [generateContent]` and both return
`404 "no longer available to new users"` on the first real request. `gemini-flash-latest`
answers but returned no tool call on the probe. Probe before trusting the listing, and pin
an exact model rather than an alias.

---

## PostgreSQL for the integration suite

The integration tests need `postgresql://postgres:postgres@localhost:5432/postgres` and
skip cleanly when it is absent. Where no Postgres, Docker or Podman exists, a portable
cluster runs the suite without installing anything — no service, no registry entry, and
deleting the directory removes it:

```sh
curl -L -o pg.zip https://get.enterprisedb.com/postgresql/postgresql-17.2-1-windows-x64-binaries.zip
python -c "import zipfile; zipfile.ZipFile('pg.zip').extractall('pgroot')"
echo postgres > pw.txt
pgroot/pgsql/bin/initdb.exe -D pgdata -U postgres --pwfile=pw.txt -E UTF8 --no-locale
rm pw.txt
pgroot/pgsql/bin/pg_ctl.exe -D pgdata -l pg.log -o "-p 5432" -W start
```

**Use `-W`, not `-w`.** On Windows `pg_ctl start -w` blocks long after the server is already
accepting connections; poll `pg_isready` instead. Worse, if the blocked wrapper is later
killed it takes the server down with it — which looks exactly like a suite that regressed
from 101 passing to 97 for no reason.

### The local cluster has no pgvector. The remote one does. Verified 2026-09-10.

| Database | Version | `vector` |
|---|---|---|
| portable EDB cluster in the scratchpad — **the suite runs here** | 17 (Windows binaries) | **absent**; there is no `vector.control` in `pgsql/share/extension/` |
| remote Coolify instance | 17.11 (Debian, `pgdg12`) | **0.8.6 available**, not yet `CREATE EXTENSION`-ed |

The EDB Windows binaries ship no pgvector and it is not a file you can drop in — it is a C
extension that must be compiled against the server. The Debian image has it packaged.

**Do not write "Postgres has pgvector" anywhere.** A capability belongs to a named database.
That sentence, written as a single row in `docs/STATE.md`, was copied into a wave prompt and
produced a `CREATE EXTENSION vector` migration aimed at the cluster that cannot have it —
and because migration discovery applies every sibling module it finds, one impossible
migration took the entire startup path down with it.

---

## What a green suite still does not prove

`CLAUDE.md` lists five areas where a passing test means nothing. Two of them now have a
concrete instance recorded here, which is the point of writing them down:

| Area | The instance |
|---|---|
| Policy engine | Two independent reducers existed — the fake and `PgToolPolicy` — and only the fake was pinned. Perturbing the winning `rule_id` kept every effect correct, so the tool was still denied correctly while the audit row named the wrong rule. Six tests stayed green. |
| Untrusted content and tool pairing | Not yet instantiated. F6 and F3 will get there. |

The layer rule has its own guard, and it earned its place immediately:
`test_contract.py::test_domain_and_application_import_nothing_external` walks the AST of
`domain/` and `application/` and found `import yaml` in `domain/profile.py` on its first
real run. Its own docstring had predicted the shape of the catch — *"ruff's banned-api
catches the four named libraries; this catches the fifth one nobody thought to ban"*. The
fifth was `yaml`. It is now banned by name too.

---

## Compaction cost, measured against a real bill (t-f5-09)

**Date: 2026-09-11. Model: `minimax/MiniMax-M3` (bare alias `MiniMax-M3` through the live
LiteLLM proxy, `LITELLM_KEY_TENANT_A` for the ladder condition, `LITELLM_KEY_TENANT_B` for
the control — the same per-team spend separation `test_proxy_mode.py` already verified).
Harness: `Core/tests/integration/test_compaction_cost.py`.**

**Traffic: one identical fourteen-turn onboarding conversation** (a senior engineer
walking a teammate through a data-pipeline design — real, distinct, natural-language
paragraphs, never repeated-character filler) **run twice, real provider both times,
through the real `LadderContextEngine` on one run and with no engine at all on the
other.** `context_window` was set to 3,000 for this run — deliberately small, so a
bounded, cheap, real conversation actually crosses `CompactionPolicy`'s default
`trigger_fraction` (0.75) instead of needing hundreds of turns to do it for real. That is
the one deliberately unrealistic knob in this measurement; everything else (the trigger,
the ladder, the summariser, the wire calls) is the same code and the same kind of call
production makes.

### The numbers

| Condition | Total tokens billed (14 turns) | Tokens/turn | Rungs applied |
|---|---|---|---|
| With ladder (tenant A) | **17,666** | 1,261.9 | `L1_PRUNE_TOOL_OUTPUT`, `L2_SLIDING_WINDOW`, `L3_SUMMARISE_MIDDLE` (fired once, at turn 8) |
| Without ladder (tenant B) | **33,684** | 2,406.0 | — |

Per-turn tokens, with ladder: `534, 586, 943, 1299, 1654, 2013, 2367, 627, 683, 453, 517,
871, 1083, 1431` — the drop from 2,367 to 627 at turn 8 is the one real `L3` pass. Per-turn
tokens, without ladder, over the identical script: `534, 586, 943, 1299, 1654, 2013, 2367,
2728, 2756, 3099, 3460, 3813, 4081, 4351` — monotonic, as an uncompacted history has to be.

**Verdict at this trigger, on this traffic: the ladder saved money.** 17,666 vs 33,684
total tokens is a 47.6% reduction, and the summariser's own real model call (one `L3`
pass, folded into the 17,666 total above, not hidden from it) cost far less than the
prompt growth it avoided over turns 8–14.

### What this run does NOT show, so the next reader does not over-read it

- **Only one compaction pass fired.** Fourteen turns at `context_window=3000` crossed the
  trigger exactly once. This says nothing about the failure mode `domain/compaction.py`
  actually warns about — repeated compaction, many times over a much longer conversation,
  each pass rewriting the prompt prefix and invalidating the provider's cache. That
  requires a much longer real run than this harness's cost budget covers, and is still an
  open question.
- **LiteLLM's own `/team/info` spend did not move within a 60-second poll on either
  tenant** (`spend_before == spend_after` for both — see the harness output). This is the
  same asynchronous-posting lag `test_proxy_mode.py` documented (25–65s observed there,
  sometimes longer); the token counts above are the authoritative numbers this entry
  relies on, taken directly from each response's own `usage` block — a real,
  provider-reported figure, never a `CHARS_PER_TOKEN` estimate. A future run wanting a
  dollar figure needs a longer poll budget than this one used.
- **`context_window=3000` is not a production value.** Shrinking it was the only way to
  make a bounded, cheap real conversation cross `trigger_fraction=0.75` at all. It changes
  how SOON the ladder fires, not what code runs when it does — but the ratio measured
  here (how much the one paid `L3` call cost against how much prompt growth it avoided)
  will shift at a real window size, where more material gets folded into each summary.
- **No tool calls in this script.** The pairing guarantee (`t-f5-06`, `t-f5-10`'s own live
  test) is proven elsewhere; this run measures cost only, over plain text turns.

The hypothesis this anchor was free to disprove — that compacting can cost more than it
saves — did NOT hold on this traffic, at this trigger. That is a real result for this one
shape of conversation, not a general proof that the ladder always pays for itself.
