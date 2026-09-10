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

**So F2's D19 additions are mostly configuration, not construction.** Read this section
before writing `t-f2-03` or `t-f2-05`; hand-rolling either would reimplement something the
dependency already ships, and would have to be maintained against it forever.

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
