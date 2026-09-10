# Agent-Core

A hexagonal agent core: **fifteen ports, of which only two change when a new vertical is
added**. Built to power different agentic products — customer service, delivery profit
optimization, fraud detection over databases, incident handling with evidence — from one
unchanged kernel.

**Status:** specification and skeleton complete. Zero behaviour implemented. Phase F0 has
not started.

---

## What this is, in one paragraph

The core owns **policy, budget, audit, human-interaction lifecycle, context governance and
knowledge access**. It does not own the reasoning loop, provider quirks, durability or
transport — those are rented from Pydantic AI, DBOS and LiteLLM. An agent is an
`AgentProfile` plus a tools package, so characterizing a new agent never touches `domain/`,
`application/` or `ports/`.

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Agent loop, tools, MCP, multimodal, compaction mechanics | Pydantic AI | Exposes the loop instead of hiding it; hooks at seven lifecycle stages |
| Durability, queues, long pauses, crash recovery | DBOS Transact | In-process library over Postgres; partitioned queues give per-session serialization natively |
| Multi-provider, failover | LiteLLM | Library on day 1, proxy on day 2 |
| HTTP surface | FastAPI | Async end to end (D13) |
| Storage | Postgres | Two logical databases day 1, three on day 2, one instance |

No Redis, no message broker — see D21. Async throughout with two deliberate sync islands —
see D13.

## Layout

```
Agent-Core/
├── docs/                specification — start here
└── Core/                all development
    ├── pyproject.toml
    ├── profiles/        <vertical>.yaml — an agent is data, not a subclass
    ├── skills/          <name>/SKILL.md — how to do things
    ├── src/agent_core/  domain · application · ports · adapters
    └── tests/           conftest · fakes · unit · integration
```

## Documents

| Document | Read it for |
|----------|-------------|
| [docs/DECISIONS.md](docs/DECISIONS.md) | D1–D23, each with the evidence that produced it. **Read this first.** |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The fifteen ports, layer rules, and every subsystem design |
| [docs/TASKS.md](docs/TASKS.md) | The task spine — 90 anchored tasks every code stub points at |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases F0→F10 and D2, each with a verifiable done-criterion, plus sizing and effort |
| [docs/GAPS.md](docs/GAPS.md) | What is still undesigned, grouped by what it blocks |
| [CLAUDE.md](CLAUDE.md) | Layer rules, non-negotiables, silent-bug areas |

SDD context lives in Engram under the topic key `sdd-init/agent-core`, not in a file. An
earlier filesystem fallback (`docs/SDD-CONTEXT.md`) existed only while Engram was
unreachable and has been superseded.

## Quick path

1. Read `docs/DECISIONS.md`. It is the shortest route to understanding why everything else
   looks the way it does.
2. Read `docs/ARCHITECTURE.md` §3 — the ports table. **That table is the contract.**
3. Open `docs/TASKS.md` at phase F0 and start at `t-f0-01`.

Every stub in `Core/src/` carries its phase, its task anchor and its status in the module
docstring, plus the pseudo-code an implementer needs. Follow the anchor to `docs/TASKS.md`
for the done criterion.

## The contract to defend

> Adding a vertical is one profile file plus one tools package.
> Zero changes to `domain/`, `application/` or `ports/`.

Fifteen ports now, up from eleven in the first draft. Every capability added since —
compaction, skills, MCP, multimodal, knowledge, peers, transcripts — entered as **data and
composition**, never as a per-vertical code change. That invariant, not the port count, is
what the architecture defends.

If a change violates it, the port cut is wrong. Stop and revisit it before continuing.
`Core/tests/unit/test_contract.py` enforces this once F4 lands.

## Silent-bug areas

Five things in this system fail no test. They are listed in `CLAUDE.md` and each one has its
own reasoning in the spec: DBOS step determinism (crash recovery only), compaction strategy
(the bill only), the policy engine (the day it matters), untrusted-content wrapping (under
an injection attempt), and tool call/return pairing (a provider 400, much later).

Verify them by hand. A green suite says nothing about any of them.

## Related work

`../Hermes-Core/` holds a clone of NousResearch/hermes-agent plus a completed extraction of
its agent core (245 modules, 112,973 lines). That extraction is **not** the basis for this
repo — it is the field reference. When a provider misbehaves or a failure mode is unclear,
its `agent/error_classifier.py` and `plugins/model-providers/` usually already contain the
answer with a comment explaining why.
