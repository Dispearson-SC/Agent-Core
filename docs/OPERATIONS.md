# Operations

Procedures an operator runs by hand. Each one names the command, what it reads, and what it
changes. Decisions behind them are in `docs/DECISIONS.md`.

## Bumping the DBOS application version

`composition.PINNED_DBOS_APPLICATION_VERSION` (currently `agent-core-turns.4`) is bumped when a
change makes turns that are already in flight incompatible with the new code: a workflow step
added, removed, renamed or reordered, or a changed workflow argument (D28 took `.1` to `.2`;
D31 took `.2` to `.3`: the peer job gained `answer_token` and the round gained the cancel
cascade steps; D32 took `.3` to `.4`: every round, including one that asks only people, is
collected and resumed once, so a turn suspended on a person under `.3` is stranded. Peer asks in
flight on `.2` are stranded too: `stranded-turns` lists them, and
their mailbox rows stay `delivered` until the lease reclaims them: since D32 the lease treats a
workflow left open under another version as absent, cancels it and reruns the ask under the new
build, see "Peer redelivery lease").

DBOS recovers a PENDING workflow only when its row names the version the recovering process
claims. After a bump, a turn that was suspended under the old version (waiting for a person or
for a peer) is **not** recovered and **not** failed: it stays `PENDING` for ever. Because
`agent-core-turns` runs one turn at a time per session, it also blocks every later turn of that
conversation. Nothing raises.

### 1. Before the deploy: see what is open

```bash
python -m agent_core stranded-turns
```

Run it with the **old** build's settings to see what is in flight now, and with the **new**
build after the deploy to see what the bump stranded. It reads the DBOS system database
(`<app database>_dbos_sys`, table `dbos.workflow_status`) with one SELECT. It does not launch
DBOS, does not recover anything and writes nothing.

| Exit | Meaning |
|---|---|
| 0 | no open workflow (`PENDING`, `ENQUEUED`, `DELAYED`) under a version other than the one this build claims |
| 1 | stranded workflows were listed (id, status, version, queue, age) |
| 2 | the check could not run (system database unreachable); never read this as clean |

### 2. Drain (preferred)

Before the deploy, stop new traffic and let the in-flight turns finish: people decide their
approvals, peers answer, or the turn expires on its own (`THREE_DAYS` for a person, the asker's
`reply_timeout_seconds` for a peer). Run the check against the old build until it reports 0
open turns, then deploy.

### 3. Cancel what is left (after the deploy, or when draining is not possible)

A stranded turn will never resume into the new code, and replaying it against a different step
sequence is exactly what the bump exists to prevent. Cancel it so its session's slot is freed:

```bash
python -m agent_core stranded-turns          # note the workflow ids
dbos workflow cancel <workflow_id> \
  --sys-db-url "<system database url, i.e. the app url with _dbos_sys appended to the database>"
```

`dbos workflow list --status PENDING --application-version <old version> --sys-db-url ...`
lists the same rows. The person or the asking agent waiting on a cancelled turn gets no reply;
tell them to ask again. Run `stranded-turns` once more: it must exit 0.

Do **not** edit `dbos.workflow_status` by hand and do **not** set
`AGENT_CORE_DBOS_APPLICATION_VERSION` back to the old value to "recover" them against changed
code.

## Peer redelivery lease

The peer worker claims an ask (`delivered`) and then enqueues the DBOS workflow
`peer-<correlation_id>`. If the worker dies between the two, nothing runs the ask. Every worker
pass therefore puts back asks that are `delivered`, unanswered, older than the lease and whose
workflow does not exist (D31, C3).

- Lease: `AGENT_CORE_PEER_LEASE_SECONDS`, default `300`. A positive whole number; anything else
  stops startup. Keep it well above the time between a claim and the enqueue, and well below the
  asking profile's `reply_timeout_seconds`.
- What to look at: `SELECT correlation_id, state, redeliveries, delivered_at FROM peer_messages
  WHERE redeliveries > 0;` shows asks that were redelivered. The worker prints
  `[peer] <agent> reclaimed <id>` for each.
- A row that stays `delivered` past the lease with a workflow that exists is not reclaimed: look
  at that workflow (`dbos workflow get peer-<id>`), not at the queue.
- **After a version bump** (D32). A callee workflow a previous build left `PENDING` is never
  recovered by the new one, so for the lease it counts as absent. Each worker pass cancels it
  (and the asks it made to third agents), requeues the ask with `redeliveries` + 1 and the next
  claim runs it as `peer-<id>-r<n>`; the worker prints `[peer] <agent> reclaimed <id>`. The stranded
  workflow stays visible as `CANCELLED`. The rerun starts the callee's turn from the beginning, so a
  tool with side effects that the first run had already executed can run twice; approvals the first
  run published stay open until they expire. If you would rather not rerun such an ask, cancel it
  yourself before the lease runs out (`cancel_turn` on the asker cancels the row).
  This needs the lease to be on (`AGENT_CORE_PEER_LEASE_SECONDS`, on by default) and a worker
  running under the NEW build; asks whose asker is also stranded still get their answer recorded
  on the row, but the asking workflow will not wake, so `stranded-turns` remains the check for it.

## Cancelling a turn that is waiting on peers

`turn_workflow.cancel_turn(turn_id, reason)` cancels the asking workflow, marks every open ask it
made `cancelled` and cancels the callee workflows `peer-<correlation_id>`, then the asks and
workflows those callees made in turn (A -> B -> C, D32). A bare
`dbos workflow cancel <turn id>` cancels only that workflow: its callees keep running and their
answers land on rows nobody reads. To see what a cancel did:
`SELECT correlation_id, cancel_reason, cancelled_at FROM peer_messages WHERE state = 'cancelled';`
A reply timeout and an expired asking turn cancel their asks the same way, with reasons
`reply timeout: ...` and `asking turn expired ...`.

## Poison turns (attempt budget)

A turn or callee whose step keeps killing its worker (a crash in a tool, an out-of-memory kill)
used to be re-run by every restart for ever. Each workflow body now counts how often it was
entered in a row without having been parked on a wait (D32, C5), and the entry past the budget is
refused.

- Budget: `AGENT_CORE_MAX_TURN_ATTEMPTS`, default `10`. A positive whole number; anything else
  stops startup. A restart of a turn that was waiting for a person or a peer is not an attempt.
- What you see: the workflow ends `ERROR` with an error that starts `workflow '<id>' was started
  N attempts in a row ...` (`dbos workflow get <id>`), an ERROR log line with the same text, and
  the row `SELECT * FROM workflow_attempts;` (the count of a poisoned workflow is kept; rows of
  workflows that ended normally are removed). A callee's asker gets the fixed "could not be
  answered: the answering agent failed" answer and is woken; asks the poisoned turn still had
  open are cancelled.
- The person on the other end of a poisoned ordinary turn is not told anything: tell them, then
  look for what killed the process (the last step in `dbos workflow steps <id>` is the one that
  never completed).
- To retry a poisoned workflow on purpose after fixing the cause, delete its ledger row and fork
  or resubmit it; do not raise the budget to "push it through".
