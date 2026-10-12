# Operations

Procedures an operator runs by hand. Each one names the command, what it reads, and what it
changes. Decisions behind them are in `docs/DECISIONS.md`.

## Bumping the DBOS application version

`composition.PINNED_DBOS_APPLICATION_VERSION` (currently `agent-core-turns.10`) is bumped when a
change makes turns that are already in flight incompatible with the new code: a workflow step
added, removed, renamed or reordered, or a changed workflow argument (D28 took `.1` to `.2`;
D31 took `.2` to `.3`: the peer job gained `answer_token` and the round gained the cancel
cascade steps; D32 took `.3` to `.4`: every round, including one that asks only people, is
collected and resumed once, so a turn suspended on a person under `.3` is stranded; D33 took `.4` to
`.5`: a peer workflow is named by a UUID instead of `peer-<id>`, so a peer workflow still open
under `.4` is stranded and the new build does not know its name, see "Deploying the UUID peer
workflow ids"; D36 took `.5` to `.6`: the request key of a peer ask names its round and position, see
"Deploying the ask keys"; D39 took `.6` to `.7`: a round waiting on a peer records one more step
(the fan-out policy), see "Deploying the fan-out policy"; D47 took `.7` to `.8`: a round that
asks a peer records the context step first, see "Deploying peer context"; D49 took `.8` to `.9`:
background asks, see "Deploying background asks". Peer asks in
flight on `.2` are stranded too: `stranded-turns` lists them, and
their mailbox rows stay `delivered` until the lease reclaims them: with
`AGENT_CORE_RETIRE_OTHER_VERSIONS=true` (D33) the lease treats a workflow left open under another
version as absent, cancels it and reruns the ask under the new build; by default it leaves it alone,
see "Peer redelivery lease").

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

## Deploying the UUID peer workflow ids (`.4` -> `.5`, D33)

A peer workflow is now named `uuid5(namespace, "<correlation id>:<generation>")` (it is also the
callee's turn id, stored in UUID columns). Nothing recognises a peer workflow by a prefix any more:
`dbos workflow list` shows opaque UUIDs. To find the workflow of an ask compute its id:

```bash
python - <<'PY'
from agent_core.adapters.driving.workflow.turn_workflow import peer_workflow_id
print(peer_workflow_id("<correlation id>", 0))   # generation = peer_messages.redeliveries
PY
```

Before deploying `.5`, DRAIN peer asks: no `delivered` row of `peer_messages` should be unanswered
(`SELECT correlation_id FROM peer_messages WHERE state = 'delivered';` empty). A `.4` callee still open
under `.4` is stranded like any other stale-version workflow (`stranded-turns` lists it, cancel it
as above). Since D34 the lease also looks for the old `peer-<id>[-r<n>]` name of each attempt: a `.4`
callee still open is left alone by default and cancelled/rerun only with
`AGENT_CORE_RETIRE_OTHER_VERSIONS=true`. Draining first (or cancelling the old workflow) still avoids
a duplicate run of side-effecting tools.

Migration `0029` (`peer_messages.turn_id` TEXT) stays: it is shipped, harmless, and still lets a
non-UUID asking turn id be recorded. It is no longer REQUIRED by the peer workflow ids.

## Deploying the ask keys (`.5` -> `.6`, D36)

An ask is now filed under the key `r<round>.s<position>.<tool call id>` and a repeat of a question
in a later round is a fresh ask (migration `0031` drops the per-question uniqueness of keyed asks).
A turn suspended under `.5` is stranded like any stale-version workflow, so before deploying `.6`:

1. Let turns finish or drain them: `python -m agent_core stranded-turns` against the OLD build must
   report 0 (see "Deploying a version bump" above), and no `peer_messages` row may be `delivered`
   and unanswered.
2. Deploy `.6`; run `stranded-turns` again and cancel what it lists, as above.

Why it cannot be left to recover: an ask step interrupted under `.5` (the row inserted, the step
not yet recorded) would be re-executed under `.6` with the new key, derive another correlation id
and post a SECOND row for the same question: the callee runs twice and its tools can run twice.
Migration `0031` is applied by the new build at startup and is NOT safe to apply under a running
`.5` build: it drops the constraint the `.5` ask statement names (`ON CONFLICT ON CONSTRAINT
uq_peer_messages_turn_target_question`), so every ask a `.5` process makes after it fails. Stop the
`.5` processes first (the drain above), then start `.6`.

## Deploying the fan-out policy (`.6` -> `.7`, D39)

A round that waits on a peer now records one more step (`_step_peer_fan_out`, the asking profile's
`peers.fan_out`) and the answer read returns the callee's failure flag. A turn suspended under `.6`
would replay into another step sequence, so it is stranded like any stale-version workflow: drain
before deploying `.7` exactly as for `.6` ("Deploying the ask keys" above: `stranded-turns` against the
OLD build must report 0), then deploy `.7`, run `stranded-turns` again and cancel what it lists.
Migration `0032` (`peer_messages.answer_failed`, `callee_failures`) is additive with defaults, so it
is safe under a running `.6` build.

## Deploying peer context (`.7` -> `.8`, D47)

A round that asks a peer now records one more step (`_step_peer_context`, what the asking profile's
`peers.visibility` lets its peers know of the conversation) before the ask step. A turn suspended on
a peer under `.7` would replay into another step sequence, so it is stranded like any stale-version
workflow: drain before deploying `.8` as for `.7` (`stranded-turns` against the OLD build must report
0), deploy `.8`, run `stranded-turns` again and cancel what it lists. The batch also adds migrations
`0034` (`peer_messages.peer_session_id`) and `0035` (the owner chain columns and the two transcript
views); both are additive, but `0035` re-creates `transcript_entries` and `transcript_conversations`,
so apply it in the same window as the `.8` build and not under a running `.7` reader you cannot
restart. Nothing was deployed under `.3`-`.8` on this branch, so the bumps collapse into one before
the first release.

## Deploying background asks (`.8` -> `.9`, D49)

`PeerDispatch` and `PeerOwner` (pickled into the operation log and into the peer workflow's input)
gained fields, and the peer workflow records `_step_background_follow_up` for a background ask. A
turn or callee suspended under `.8` is stranded like any stale-version workflow: drain before
deploying `.9` as for `.8` (`stranded-turns` against the OLD build must report 0), deploy `.9`, run
`stranded-turns` again and cancel what it lists. Migration `0036` (`peer_messages.background`, nullable
JSONB) is additive and safe under a running `.8` build. Nothing was deployed under `.3`-`.8` on this
branch, so the bumps collapse into one before the first release.

### Background asks: looking and stopping

Background asks of one conversation:
`SELECT correlation_id, target_agent_id, state, answered_at, cancelled_at FROM peer_messages WHERE
from_tenant_id = '<tenant>' AND from_session_id = '<session>' AND background IS NOT NULL;`

Closing a conversation stops its running background asks (rows, callee workflows and what those
asked in turn) and returns the handles it ended; like `cancel_turn` it runs outside a workflow:
`await turn_workflow.close_conversation(SessionRef(...), "conversation closed")`. Cancelling the
asking TURN (`cancel_turn`) does not touch them. A follow-up turn already created for an ask that
finished before the close still runs. If the answer is recorded and the follow-up never appeared (the
enqueue failed: the callee workflow is in `ERROR`), the answer is on the row
(`SELECT answer FROM peer_messages WHERE correlation_id = '<handle>'`); nothing re-enqueues it. The
same holds for a callee given up as poison: its failure answer is recorded, and a follow-up that
could not be enqueued is logged at `ERROR` ("could not enqueue the follow-up for poisoned background
ask <handle>").

## Deploying progress events (`.9` -> `.10`, D52)

Every turn now records `_step_progress` steps (`started` after the turn id, `finished` or `failed`
after the turn), so a turn or callee suspended under `.9` replays into a different step sequence and
is stranded like any stale-version workflow: drain before deploying `.10` (`stranded-turns` against
the OLD build must report 0), deploy, run `stranded-turns` again and cancel what it lists. Migration
`0037` (`turn_events`) is additive and safe under a running `.9` build. Nothing was deployed under
`.3`-`.9` on this branch, so the bumps collapse into one before the first release.

### Progress events: looking and cleaning

The events of one tree: `SELECT seq, at, turn_id, agent, kind, tool, elapsed_ms, detail FROM
turn_events WHERE tenant_id = '<tenant>' AND root_turn_id = '<root turn>' ORDER BY seq;`

Nothing deletes events. Keep what a feed or an investigation needs and trim the rest on your own
schedule: `DELETE FROM turn_events WHERE at < now() - interval '30 days';` (a feed that reconnects
after its events were trimmed gets a snapshot of whatever remains, C22). A failing event store is
logged as `could not publish the <kind> event of turn ...` at WARNING and never fails a turn.

### The event feed

`GET /turns/<turn id>/events` (same `X-Subject-Id` / `X-Tenant-Id` headers as `GET /turns/<id>`):
`curl -N -H 'X-Subject-Id: ops' -H 'X-Tenant-Id: <tenant>' http://<host>/turns/<id>/events`. Each
connection costs one open request and a 64-event queue; it never holds a database connection (the
broker holds ONE long-lived `LISTEN` connection per process and reads in a thread; the event
publisher, the feed's reader and the graph reader share a separate pool of at most 4 connections,
opened on first use). Behind a reverse proxy
disable response buffering for the path (the app sends `X-Accel-Buffering: no`) and keep the idle
timeout above 15 s (the keepalive interval). A feed that cannot read the database logs `the event feed
could not read new events; retrying` at WARNING and recovers by itself; clients reconnect to a snapshot.
The stream ends only when a snapshot shows the tree settled (no running turn and no queued or running
ask under it, so a background ask keeps it open), and every 5 s of quiet (or the keepalive interval,
if shorter) the feed reads a snapshot, so a missed event is corrected by the next one. A tree of more
than 2000 events is shown from its newest events; the snapshot says `"truncated": true` and
`omitted_events`.

### The task graph

`GET /turns/<root turn id>/graph` (same headers as `GET /turns/<id>`) reads `peer_messages` for the
tenant and root, oldest 500 asks; past that the response says `"truncated": true` and `omitted_asks`. By hand: `SELECT correlation_id, target_agent_id, from_agent_id,
turn_id, state, enqueued_at, delivered_at, answered_at FROM peer_messages WHERE from_tenant_id =
'<tenant>' AND root_turn_id = '<root>' ORDER BY enqueued_at;`. Migration `0038` (an index) is
additive and safe under a running build.

## Model tiering and its measurement (C24, D56)

`model_fast: <provider/model>` on a profile sends the peer answers that agent gives (a turn another
agent started with `ask_peer`) to a smaller model; `fast_steps: [peer_answer]` is the default and the
only kind today. Nothing changes until a profile declares it. Declaring it changes the profile's
content hash, so the next start registers a new profile version. The preflight checks the fast
model's credential like the main one.

Measure before and after with the C21 timings: a peer answer's `started` and `finished` events carry
its elapsed time, and the turn row carries the profile version that ran it, so the version that
turned `model_fast` on is the cut. Median and 95th percentile of the peer answers per profile and
version:

```sql
SELECT t.profile_id, t.profile_version, count(*) AS answers,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY f.elapsed_ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY f.elapsed_ms) AS p95_ms
FROM turn_events f
JOIN turns t ON t.turn_id::text = f.turn_id
WHERE f.kind = 'finished' AND f.asker_turn_id IS NOT NULL
GROUP BY t.profile_id, t.profile_version ORDER BY t.profile_id, t.profile_version;
```

A resumed turn (one that waited for a person) continues on the main model, so a tiered answer that
needed an approval is not in the fast numbers after the approval.

## Peer redelivery lease

The peer worker claims an ask (`delivered`) and then enqueues the callee workflow of the ask
(`peer_workflow_id`). If the worker dies between the two, nothing runs the ask. Every worker
pass therefore puts back asks that are `delivered`, unanswered, older than the lease and whose
workflow does not exist (D31, C3).

- Lease: `AGENT_CORE_PEER_LEASE_SECONDS`, default `300`. A positive whole number; anything else
  stops startup. Keep it well above the time between a claim and the enqueue, and well below the
  asking profile's `reply_timeout_seconds`.
- What to look at: `SELECT correlation_id, state, redeliveries, delivered_at FROM peer_messages
  WHERE redeliveries > 0;` shows asks that were redelivered. The worker prints
  `[peer] <agent> reclaimed <id>` for each.
- A row that stays `delivered` past the lease with a workflow that exists is not reclaimed: look
  at that workflow (`dbos workflow get <peer_workflow_id>`, see above), not at the queue.
- **After a version bump** (D32, gated by D33). A callee workflow a previous build left `PENDING`
  is never recovered by the new one, so for the lease it counts as absent - but ONLY when
  `AGENT_CORE_RETIRE_OTHER_VERSIONS=true` (below); by default it is left alone. Each worker pass cancels it
  (and the asks it made to third agents), requeues the ask with `redeliveries` + 1 and the next
  claim runs it as generation `n` (`peer_workflow_id(<id>, n)`); the worker prints `[peer] <agent> reclaimed <id>`. The stranded
  workflow stays visible as `CANCELLED`. The rerun starts the callee's turn from the beginning, so a
  tool with side effects that the first run had already executed can run twice; approvals the first
  run published stay open until they expire. If you would rather not rerun such an ask, cancel it
  yourself before the lease runs out (`cancel_turn` on the asker cancels the row).
  This needs the lease to be on (`AGENT_CORE_PEER_LEASE_SECONDS`, on by default) and a worker
  running under the NEW build; asks whose asker is also stranded still get their answer recorded
  on the row, but the asking workflow will not wake, so `stranded-turns` remains the check for it.

- **Rerun cap** (D34, C1k): `AGENT_CORE_PEER_MAX_REDELIVERIES`, default `3`, `0` = never rerun. A
  callee that keeps ending in `ERROR` or `CANCELLED` is "absent" to the lease and would be
  requeued every lease for ever (its poison budget restarts with each new workflow id). Once
  `redeliveries` exceeds the cap, the next claim closes the ask with the fixed failure answer, wakes
  the asker, and the worker prints `[peer] <agent> <id> abandoned`.
- **`.4` workflow names** (D34): during the `.4` -> `.5` rollout the lease, `retire` and the cancel
  cascade also look for the old `peer-<id>` / `peer-<id>-r<n>` workflow of each attempt, so a callee
  still running under a live `.4` executor is not mistaken for absent. Same rules as any other
  version: left alone unless the flag below is on.

### Rolling deploys: `AGENT_CORE_RETIRE_OTHER_VERSIONS` (D33)

By default the lease NEVER cancels a callee workflow that is still open under another application
version: during a rolling deploy the old executor may be running it, or it may be parked on a
person. An ask behind such a workflow stays `delivered`. Once every executor of the old version is
stopped, set `AGENT_CORE_RETIRE_OTHER_VERSIONS=true` on the peer worker (and restart it): its
next passes cancel those workflows and rerun the asks as described above. Turn it off again once
`stranded-turns` reports 0. A workflow that is already `CANCELLED` or `ERROR` behind an unanswered
`delivered` row (an interrupted retire) is requeued without the flag.

## A2A asks do not survive a restart of the asking process (D33)

When peers are reached over A2A (`A2AAgentMailbox`), the secret that authenticates each peer's
answer is held in the asking process's memory only. If that process restarts while an ask is
open, the late answer is dropped and the process logs `dropping a delivered answer for A2A task
<id>: no answer token on file for it` (the id, never the secret). The asker's reply timeout then
ends the ask. Treat that warning as "an answer was lost, ask again"; it is expected only around
restarts. The Postgres mailbox keeps its tokens on the row and has no such window.

## Cancelling a turn that is waiting on peers

`turn_workflow.cancel_turn(turn_id, reason)` cancels the asking workflow, marks every open ask it
made `cancelled` and cancels the callee workflows, then the asks and
workflows those callees made in turn (A -> B -> C, D32). Every generation of the callee is cancelled, from 0 up to
`peer_messages.redeliveries`, even if an earlier one never started (D33). A bare
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
