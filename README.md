# Agent-Core

A hexagonal agent core: **fifteen ports, of which only two change when a new vertical is
added**. Built to power different agentic products — customer service, delivery profit
optimization, fraud detection over databases, incident handling with evidence — from one
unchanged kernel.

**Status:** see [docs/STATE.md](docs/STATE.md) — it is the one place that answers "what
works right now" and it drifts if this file tries to answer it too (D24).

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

## Operator runbook

This is the part that used to live only in a conversation: how a fresh clone with an
empty PostgreSQL instance and a credentials file actually becomes a running system, by
hand, in the order a person needs it. `docs/STATE.md` and `docs/ROADMAP.md`'s F11 section
own *what is true and why* — this section owns *how*, and does not repeat either.

### 1. What has to be running: PostgreSQL, nothing else

One PostgreSQL instance. **The app never requires superuser to run** — only, optionally,
to bootstrap itself once. Two logical databases are used day 1: the application's own, and
a second one the durable engine (DBOS) manages.

- Set an administrative connection string (one that can `CREATE DATABASE`) and startup
  creates both databases and applies every migration, idempotently, on every start. Leave
  it set for local development and you get the one-command start this phase promises.
- Leave it unset and startup only migrates: a missing database fails loudly, naming the
  exact `CREATE DATABASE` statement to run by hand. Production sets the admin string for
  one bootstrap run and removes it afterwards — a deployment that hands out its
  application's superuser credentials permanently has a bigger problem than a missing
  table.

Which variable is which is in the next step.

### 2. Configure: copy `.env.example`

```
cp .env.example .env
```

`.env` is gitignored and never committed; `.env.example` ships with every credential blank
on purpose — **fill in your own values, never commit one**. `Settings.from_env` layers
`.env` *under* the real process environment: a variable you export in your shell always
wins over the file, and a missing `.env` is not an error — that is how production runs,
with real variables and no file at all.

What each name is for, grouped the way `.env.example` groups them:

| Variable | What it is for |
|---|---|
| `AGENT_CORE_DATABASE_URL` | Where the application's own data lives. Points at a database that does not exist yet on a fresh instance. |
| `AGENT_CORE_ADMIN_DATABASE_URL` | Optional. An administrative connection string — see step 1. Present, startup bootstraps; absent, startup only migrates and names the command if something is missing. |
| `AGENT_CORE_AUDIT_DATABASE_URL` | Optional. Only set this if the audit trail must write to a genuinely different database than the one above — day 1 leaves it unset and audit rows live beside the domain tables, on their own connection pool. |
| `MINIMAX_API` | The MiniMax credential, in this repository's own naming convention. `Settings.from_env` exports it as `MINIMAX_API_KEY` at startup — the name litellm (the library that actually makes the call) reads — so this file alone is enough; nothing needs exporting by hand. |
| `MINIMAX_API_BASE` | Optional. Overrides MiniMax's endpoint; leave unset to use the provider's default. |
| `GEMINI_API_KEY` | The credential for the `gemini/…` model family, if a profile is pointed at one. litellm reads this name directly — no mapping needed, unlike MiniMax's. |
| `AGENT_CORE_LITELLM_BASE_URL` | Unset on day 1 (library mode). Setting it switches every model call to a separate LiteLLM proxy process instead — a day-2 deployment choice, see `docs/ROADMAP.md`'s D2. |
| `TELEGRAM_BOT` | The Telegram bot token, if the Telegram channel is wired into this deployment. |
| `AGENT_CORE_WHATSAPP_ACCESS_TOKEN`, `AGENT_CORE_WHATSAPP_PHONE_NUMBER_ID` | The WhatsApp Business credentials, if that channel is wired in. |
| `AGENT_CORE_CHANNEL_TENANT_ID`, `AGENT_CORE_CHANNEL_PROFILE_ID` | Which tenant and which agent profile a channel adapter binds to. Day 1 is a single deployment, so every channel shares this one pair. |
| `AGENT_CORE_HUMAN_GATEWAY_CHANNEL_ID` | Which registered channel a pending approval is asked on. Unset resolves to the HTTP channel, which nobody is watching — set it to reach an actual human. |
| `AGENT_CORE_PROFILES_DIR`, `AGENT_CORE_SKILLS_DIR`, `AGENT_CORE_MEDIA_DIR`, `AGENT_CORE_POLICY_DIR` | Override the four `Core/…` directories this process reads from. Only needed running from an installed wheel with no `Core/` above it. |
| `AGENT_CORE_DBOS_APP_NAME`, `AGENT_CORE_DBOS_APPLICATION_VERSION` | Constants of the *source*, correct for a single-process deployment as shipped. Do not set the version override without reading `composition.dbos_config`'s docstring first — it is a pin, not a preference. |
| `DBOS_CONDUCTOR_KEY` | Optional. Connects this process to DBOS Conductor for observability; unset is a fully local, working deployment. |

`.env.example` also carries a **day-2 proxy section** (a LiteLLM proxy URL and one API key
per tenant) that this phase does not use at all — `AGENT_CORE_LITELLM_BASE_URL` above is
the only switch day 1 reads, and the proxy variables belong to `docs/ROADMAP.md`'s D2.

### 3. Run this FIRST: `python -m agent_core preflight`

Before starting anything, ask what is not ready:

```
python -m agent_core preflight
```

This exists because every failure in this phase surfaced **one at a time, three commands
apart** — the database refused, then a credential was missing, then a model id would not
resolve, then the policy table was empty. Each fix revealed the next, and preflight asks
the whole question in one pass instead. It prints one block per check and exits non-zero
if anything is a hard failure; a warning is not a refusal.

What it reports, in the categories it checks:

- **database** — is the server reachable, does the application database exist, has its
  schema been migrated, and is the second logical database DBOS wants there too (a
  warning, not a failure — the engine can create it itself).
- **policy** — is there a reviewed policy directory, and is `policy_rules` non-empty. An
  *empty* policy table is a hard failure, not a warning: it denies every tool call and, if
  the table were merely unreachable, would report a database problem that does not exist —
  which is exactly the confusion this phase is named for.
- **profile** — can every loaded profile actually take a turn: does it resolve its
  toolsets and, if it declares one, compose its MCP servers.
- **credential** — for every model a loaded profile names, is that provider's credential
  resolvable *by name* in the process environment — never its value. `MINIMAX_API_KEY:
  set` and `GEMINI_API_KEY: missing` is the whole vocabulary; nothing here ever prints a
  credential.
- **model** — does an actual client resolve for that model id and endpoint (no request is
  made — resolution is local).

Every failing line carries the exact remedy: the command to run, the variable to set, the
file to edit. Preflight never fixes anything itself — a tool that repairs what it finds is
a tool nobody can run to find out what is wrong.

### 4. Run it: `console` to work with an agent, `serve` for the HTTP process

```
python -m agent_core console      # an operator REPL — make, use and judge an agent
python -m agent_core serve        # the HTTP process
python -m agent_core peer-worker  # answers one agent's questions to another
python -m agent_core              # same as `serve`
```

**`peer-worker` is a second process, and delegation does not complete without it.** When
one agent asks another, the question lands on a durable queue and the asking turn suspends
— it does not block. Something has to claim that question, run a turn as the *answering*
agent, and post the reply. That is this process. Without it the ask is queued, correct, and
answered by nobody until the asking agent's `reply_timeout_seconds` runs out.

It runs the answering turn under the target agent's own profile, toolset, policy and
budget — never the asker's. Running it as the asking agent would make delegation a
privilege escalation dressed as a question, and an invisible one: the audit row would name
the asker and everything would look correct.

Both start the same container, apply the same migrations, and run the same preflight
before opening a socket or a prompt. The console is where an operator actually creates an
agent, gives it a turn, watches a tool call, and reads what happened — type `:help` inside
it for the full command list (switching agents, scaffolding a new profile, reading
`:policy`, `:audit`, `:pending`, `:approve`, `:trace`, and more). `serve` is the ASGI
process behind the HTTP surface, which answers `202` and is polled — it shows none of
that, which is exactly why the console exists as its own surface rather than as an
accessory to it.

### 5. Adding to the system: one line decides whether it is a TOOL or a FILE

This is the distinction the whole phase is built to prove, and it is worth reading twice:

> **A tool is behaviour, so adding one is code.** Everything else — which tools an agent
> may use, which MCP server it borrows tools from, which other agent it may ask, what
> needs a human's approval — is configuration, and needs no code change at all.

| To… | Change… | Why |
|---|---|---|
| **Add a tool** | A Python function in a tools package under `Core/src/agent_core/adapters/driven/tools/<vertical>/`, registered once in `composition.TOOL_PACKAGES` | It runs code against the real world. That is behaviour, and behaviour is reviewed as code. |
| **Make a new agent** | A profile file — copy an existing `Core/profiles/*.yaml`, or run `:new <id>` inside the console to scaffold one, then edit it | A profile is *data*: persona, model, which toolsets and MCP servers it may use, its budget, its approval rules. Reading it top to bottom answers what the agent may do, with no code open. |
| **Give an agent a tool** | The `toolsets:` list in its profile | Naming a package it may already reach. |
| **Connect it to an MCP server** | The `mcp_servers:` list in its profile — transport, command, and a name it prefixes every borrowed tool with | The server's tools arrive under `mcp_<name>_*`, obey the same policy as any local tool, and come back wrapped as untrusted content — all without a line of code, whatever the server actually does behind that transport. |
| **Point one agent at another** | The `peers:` block in its profile — `enabled`, the peer's `agent_id`, `max_hops`, `visibility` | The allowlist is checked on *both* sides: this agent has to name the peer, and the peer has to name it back, or the question never leaves. Empty means nobody. See `Core/profiles/support_triage.yaml` for a shipped, working example. |
| **Decide what needs a human** | `Core/policy/*.yaml` — `effect: allow \| needs_approval \| deny`, per tool name pattern | Reconciled into the policy table at every startup: a rule added there is granted, a rule deleted from there stops applying. There is no console command for this on purpose — a permission granted at a prompt is a permission granted with no record, and policy is administrative, reviewed configuration, never a runtime switch. |

Watching a refusal and approving it end to end, with the shipped `delivery_optimizer`
profile: `:use delivery_optimizer`, ask for a price change, watch `pricing_apply` come
back `needs_approval` (`Core/policy/rules.yaml` is why), then `:pending` and `:approve
<id>`.

**Orchestration end to end needs two more things than it looks like**, and neither is a
detail you can discover later:

```
python -m agent_core peer-worker            # in one terminal
python -m agent_core console --durable --role operator   # in another
```

then `:use support_triage` and ask something only `billing_specialist` can answer — the
two shipped profiles already name each other.

`--durable` matters because the step that hands a question to the mailbox lives in the
workflow, and the console's default mode calls the use case directly. In direct mode the
turn suspends and no mailbox is ever asked. `--role operator` matters because the grant in
`Core/policy/rules.yaml` names both a role and a channel; without the role it does not
match and the delegation is refused with `[no matching rule]`. **That refusal is the policy
engine working, not a misconfiguration** — it is the same narrowing that stops a chat
channel from reaching a tool an operator can use.

### 6. What the console does NOT prove

**This applies to the console's DIRECT mode, which is the default.** `:mode durable`
enqueues through the DBOS workflow instead and exercises all three of the things below;
the prompt carries `[direct]` or `[durable]` on every line so the mode is never something
you have to remember. Direct mode stays the default because a REPL that answers
immediately is what makes an agent worth iterating on — the trade is stated here rather
than hidden.

In direct mode the console calls `StartTurn` directly and does not go through the workflow
at all. Reading a turn there is real proof of the profile, the tool provider, the policy
engine and the audit sink, because those are the exact collaborators this deployment was
built with. It is **not** proof of three things a person judging this system needs to know
are still untested by it:

- **Durability** — a crash mid-turn is not recovered and not replayed here; there is no
  workflow to recover from, because none was started.
- **Coalescing** — the per-session partitioned queue and its debounce window never run;
  every console turn executes immediately, alone.
- **Publication** — `HumanGateway.publish` never runs, so a suspension raised in the
  console has no correlation handle a real channel could hand back; `:pending` still shows
  it and `:approve` still answers it, but only because the console is standing in for both
  sides of that handoff.

`serve` and `:mode durable` are what actually exercise all three. A turn that works in the
console's direct mode is proof of policy and tools; it is not proof of the process that
will run it in production — and delegation is the one thing that does not merely go
*unproven* there, it does not work at all, because the step that asks the mailbox is in the
workflow this mode skips.

## Documents

| Document | Read it for |
|----------|-------------|
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every decision, each carrying the evidence that produced it. **Read this first.** |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The fifteen ports, layer rules, and every subsystem design |
| [docs/TASKS.md](docs/TASKS.md) | The task spine — the anchored task every code stub points at |
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
