# Where this build stands

A running handover. `docs/TASKS.md` says what is left; this file says what is *true right
now* — what works, what cannot be run, what infrastructure exists, and what a fresh session
needs to know before touching anything.

Last updated: 2026-09-11, at the F11 wave 6 barrier. One agent is closing three defects
found by driving the shipped process by hand; everything else is committed.

## Verified state

```
849 passed, 7 skipped, 0 failed     ruff: clean
mypy strict: 238 source files        164 anchors, 3 open
```

Committed on branch `feat/f0-f10-core-implementation`. **Read `README.md` to run it** — that
is the operator runbook, and a test asserts every command and environment variable it names
actually exists.

## What works

`python -m agent_core console` is the surface agents are made, used and judged on. It
resolves a profile to its toolset, shows the policy verdict and the winning `rule_id` per
tool, holds a conversation, renders a tool call whole, scaffolds and reloads an agent, pages
sessions, and projects the last turn as either a USER or an ADMIN would see it.

`python -m agent_core preflight` answers *what is not ready* in one pass — databases,
schema, policy rules, every profile's servability, credentials by name, the model endpoint.
It exists because every failure in this phase surfaced one at a time, three commands apart.

`python -m agent_core serve` is the HTTP process: it applies every migration, launches DBOS
with a pinned `application_version`, and mounts the chat, admin and transcript routers.

**The acceptance run is the thing to trust.** `test_cli_acceptance.py` starts from an EMPTY
PostgreSQL instance, runs the real one command, and feeds nineteen scripted keystrokes
through the console — not through the use cases, because the console is what ships. It
passes 13 assertions and carries no xfails; the four it opened with have all closed.

Proved against real infrastructure: a crash mid-turn recovers without re-running the tool
that already ran (a real subprocess kill); an approval survives a redeploy; a tool denied by
policy does not execute and the refusal reaches the model with its `rule_id` in the audit
table; compaction saved 47.6% of tokens over identical real traffic.

## What is still true and worth knowing

**The console calls `StartTurn` directly, not the workflow**, and says so on startup.
Durability, the coalescing window and `HumanGateway.publish` are NOT exercised there. A turn
that works in the console proves nothing about them — which is why the banner lists them.

**D25 will refuse a one-operator approval**, by name. The approver may not be the requester,
and in a single-operator console they are the same person. That refusal is correct; it is
not a bug to work around.

**Adding a tool is CODE. Everything else is configuration.** Which tools an agent may use,
which MCP servers it connects to, which agents it may ask, what needs approval — all YAML.
That distinction is the architecture, and `test_contract.py` enforces it.

## Infrastructure

| What | Status | Env |
|---|---|---|
| MiniMax chat | `minimax/MiniMax-M3`, tool calling | `MINIMAX_API` in `.env`, exported as `MINIMAX_API_KEY` |
| MiniMax speech | `minimax/speech-2.6-turbo`, real audio | same key |
| Gemini | `gemini/gemini-3-flash-preview`, tool calling | `GEMINI_API_KEY` |
| Telegram | `@agentcoresc_bot`, verified live | `TELEGRAM_BOT` |
| LiteLLM proxy | per-team spend separation verified | `LITELLM_PROXY_URL`, two per-tenant keys |
| Postgres (local) | portable EDB 17 in the scratchpad — **the suite runs here**, no pgvector | `postgresql://postgres:postgres@localhost:5432` |
| Postgres (remote) | Coolify 17.11 Debian, **pgvector 0.8.6 available**, `agent_core_app` absent | `AGENT_CORE_DATABASE_URL` |

**`.env` currently points at the REMOTE instance, where the app database does not exist.**
Starting from it fails with a message naming the host, the port, both databases to create
and the optional admin URL. That failure is correct and informative; it is also the first
thing a new operator will hit. Copy `.env.example` for a local default that works.

**A capability belongs to a NAMED database, never to "Postgres".** Writing those two rows as
one sent a `CREATE EXTENSION vector` migration at the cluster that cannot have it, and one
impossible migration takes the whole startup down.

## The shape this build kept hitting, eight times

**A collaborator every test supplied and production never bound.** Each time, the fixture
that provided the missing piece is exactly what stopped anyone noticing it was missing:

| Missing in production | What hid it |
|---|---|
| the `ToolProvider` adapter | a test double whose own docstring says it is not an adapter |
| the day-1 model endpoint | a test that injected its own model factory |
| every migration | each integration test applying its own by hand |
| `lookup_turn` / `decide` route seats | route tests that injected the seats |
| the three evidence seats | the same |
| `fraud` absent from the tool packages | no test ever served that profile |
| the store and the runner disagreeing on history encoding | every test injected its own history |
| six console commands and the whole peer mechanism | `test_console.py` passed a loader; peer tests passed a mailbox |

The rule that closes it: **when a test has to build something before it can run, ask who
builds it in production — and if the answer is "the test", that is the anchor.** The
acceptance run exists because it is the only test that refuses to build anything.

## The other recurring defect: an anchor wider than its assertion

`t-f1-04`, `t-f1-12`, `t-f1-13`, `t-f5-10`. Each time an agent satisfied exactly what it was
asked and the anchor claimed more. The fix is never to re-open the anchor and hope; it is to
**split it so a criterion and an assertion are the same size**.

And its mirror: **a frozen port with no adapter is not a finished port.**

## Open

Three anchors, all found by driving the shipped process by hand *after* the suite was green
— `t-f11-38` (the preflight exits 0 with a FAIL check), `t-f11-39` (a shipped profile points
its MCP server at a test fixture, by a path relative to the working directory) and
`t-f11-40` (a handled degraded start prints an `ExceptionGroup` traceback).

`t-f2-06` is **BLOCKED**, not open: it needs inbound typing state, which D22 established only
Chatwoot's web widget supplies, and D23 put Chatwoot in phase two. TODO is work nobody has
done; BLOCKED is work nobody can do.

## Rules this build paid for

- **Verify combinations against behaviour, not against the argument list.** A signature check
  proves a parameter exists; only running it proves two of them compose. `docs/FIELD-NOTES.md`
  carries a RETRACTED section where that cost a wave.
- **A task that widens a port owns every implementation of it, in the same wave** — and the
  writes set is the orchestrator's. Three consecutive barriers ended red on this.
- **Never `git checkout --` a file mid-wave**, and commit at every barrier. That command cost
  765 lines of uncommitted work here. A green suite is not a save point.
- **"No red available" is never "no verification owed."** A test-only anchor over correct
  behaviour still owes a mutation proof. Fifty-six mutations found one live hole nothing in
  the repository could catch.
- **A mutation that fails to kill a test does not always mean a weak test** — sometimes the
  scenario never reached the code. Finding out which is the job.
- **An anchor is a claim, not an instruction.** One was wrong about which end a negative slice
  keeps; the implementing agent checked instead of implementing it.
