# Agent-Core — project instructions

A hexagonal agent core. Fifteen ports; **only two change when a vertical is added**.

**Read `docs/DECISIONS.md` first.** It is short and explains why everything else looks
the way it does. Then `docs/ARCHITECTURE.md` §3 (the ports table) — that table is the
contract. Then `docs/ROADMAP.md` for the current phase, and `docs/TASKS.md` for the task
you are picking up. `docs/GAPS.md` lists what is still undesigned — check it before
concluding something is missing by accident.

## The contract to defend

> Adding a vertical is one profile file plus one tools package.
> Zero changes to `domain/`, `application/` or `ports/`.

If a change violates this, the port cut is wrong. Stop and revisit it before
continuing. A test enforces this once F4 lands.

## Layer rules

All development lives under `Core/`. Package root is `Core/src/agent_core/`, so the paths
below are `Core/src/agent_core/domain/`, `.../ports/`, and so on. Repo root holds only
`docs/`, `README.md`, `CLAUDE.md` and config.

- `domain/` imports nothing external. Not Pydantic AI, not DBOS, not FastAPI, not the
  database driver.
- `application/` imports `domain/` and `ports/` only.
- `ports/` holds protocols: signatures and domain types, nothing else.
- Everything concrete lives under `adapters/`.

## Non-negotiables

1. **The DBOS workflow is a driving adapter.** Never put `@DBOS.workflow()` on a use
   case. The workflow lives in `adapters/driving/workflow/` and calls use cases as
   steps.
2. **Non-determinism goes inside a step.** Clock, UUID and randomness are generated
   inside `@DBOS.step()`, never in the workflow body. A non-deterministic body
   produces a different result on replay and the bug only appears during crash
   recovery.
3. **Never call the model inside a database transaction.** Forty-second steps holding
   connections end in pool exhaustion.
4. **MCP tools obey the same `ToolPolicy` as local tools**, their results are wrapped
   in untrusted-content delimiters, and they get a smaller result budget.
5. **Compaction cuts only at complete-exchange boundaries.** A tool call separated
   from its return makes the provider reject the conversation.
6. **`AuditSink` writes outside the domain transaction.** Append-only, never update. A
   failed turn must still leave a trace.
7. **Concurrent steps start in a deterministic order.** The system is async (D13), and
   `asyncio.gather` inside a workflow body is valid *only* when the steps are started in
   a fixed order. Never derive that order from a set, an unordered dict, or from which
   future resolved first — replay after a crash would take a different path. Sort the
   work before dispatching it.
8. **There is no knowledge-write tool.** Ever. `KnowledgeBase` is read-only and
   `KnowledgeAdmin` is never injected into anything an agent touches. A prompt injection
   cannot call a method that is not on the object the agent holds — that is the whole
   defence, and adding a write tool removes it.
9. **`AdminIdentity` is never derived from `CallerIdentity`.** Different types, different
   routes, different policies. No code path widens a chat client into an administrator.
10. **A peer agent's answer is untrusted content.** Wrap it like any `mcp_*` result.
    "It is our own agent" is not a trust argument — an agent can be misled.
11. **Transcripts store everything and filter on read.** One write path; `Audience`
    decides what comes back. A user must not see *which* tool is pending but must see
    *that* something is — hiding suspension entirely makes the conversation look broken.

## Sync or async

Async end to end, with two deliberate sync islands. Full reasoning in `docs/DECISIONS.md`
D13.

| Layer | Nature |
|---|---|
| `domain/` | **sync** — no I/O to await |
| `application/`, `ports/` | async |
| `adapters/` | async |
| Postgres transactions | **sync**, wrapped in `asyncio.to_thread` — DBOS does not support coroutine transactions |

## Conventions

- **Do not duplicate a fact that can drift.** The port count already went eleven to fifteen
  and left four stale strings behind. `docs/ARCHITECTURE.md` section 3 owns the count;
  everywhere else states the invariant — only two ports change per vertical.
- Run `make check` from the repository root (or see `.github/workflows/ci.yml`). All
  development lives under `Core/`; the root targets wrap that so nobody needs to know.
- Documentation, code, identifiers and comments in **English**. Conversation with the
  user is in Spanish.
- Ports are `Protocol` classes, one question each. A port answering two questions is
  cut wrong.
- Use cases are testable with fakes only: no database, no network, no model.

## Silent-bug areas — verify these by hand, do not trust a green suite

None of these fail a test:

| Area | How it actually surfaces |
|---|---|
| DBOS step determinism | Only on crash recovery |
| Compaction strategy | Only on the bill |
| Policy engine | Only the day it matters |
| Untrusted-content wrapping | Only under an injection attempt |
| Tool call/return pairing | As a provider 400, much later |

## Field reference

`../Hermes-Core/Hermes` is a clone of NousResearch/hermes-agent. It is **not** a
dependency and none of its code is copied here. When a provider misbehaves or a
failure mode is unclear, `agent/error_classifier.py` and `plugins/model-providers/`
usually already contain the answer with a comment explaining why.

## Current state

Specification complete. No implementation. Phase F0 has not started.
