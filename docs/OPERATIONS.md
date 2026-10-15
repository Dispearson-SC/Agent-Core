# Operations

Procedures an operator runs by hand. Each one names the command, what it reads, and what it
changes. Decisions behind them are in `docs/DECISIONS.md`.

## Bumping the DBOS application version

`composition.PINNED_DBOS_APPLICATION_VERSION` (currently `agent-core-turns.12`) is bumped when a
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

## Deploying subagents (`.10` -> `.11`, D62)

`PeerOwner` (the owner chain a callee turn carries) gained `kind` and `subagent_depth`, and the
round's recorded steps gained a `kind`, so a callee or a turn suspended on peers under `.10` cannot be
recovered by `.11`: drain before deploying (`stranded-turns` against the OLD build must report 0),
deploy, run `stranded-turns` again and cancel what it lists. Migration `0041` (`peer_messages.kind`
and `.subagent_depth`, both `NOT NULL` with a constant default, plus a check on `kind` added `NOT
VALID`) and `0042` (`VALIDATE CONSTRAINT`, a lock that lets reads and writes go on) are additive
and safe under a running `.10` build: its inserts name neither column and read as `peer`, depth 0.
Nothing was deployed under `.3`-`.10` on this branch, so the bumps collapse into one before the
first release.

## Deploying the turn router (`.11` -> `.12`, D95)

Every turn a person or a channel starts now records `_step_route` between the drain and the turn id,
whether routing is on or not (the replay shape never depends on configuration), so a turn suspended
under `.11` is stranded: drain before deploying (`stranded-turns` against the OLD build must report
0), deploy, run `stranded-turns` again and cancel what it lists. Migration `0045`
(`audit_turn_routes`) is additive and safe under a running `.11` build.

Routing is OFF until `AGENT_CORE_TURN_ROUTING=true`. Then a profile with a `routing:` block consults
its router (an enabled addon's `provides.routers`) before the model; startup stops when such a
profile names a router no enabled addon provides, or a target that is not loaded or that a channel
cannot start. `AGENT_CORE_TURN_ROUTER_TIMEOUT_MS` (default 1500) bounds one consultation. Turning it
off again needs no drain: the step stays and simply returns the request unchanged.

A turn that continues a conversation (its session already holds another turn) is NOT routed: the
router is not asked and the audit row says `fallback_reason = 'follow_up'`. A router reads the
message alone, so "and which of those is the most urgent?" lands on whatever "those" sounds like,
and an intent plan's compose has no history. `AGENT_CORE_TURN_ROUTING_FOLLOW_UPS=true` routes
follow-ups too. No drain either way: the check runs inside the recorded route step. The check
and the router share the one `AGENT_CORE_TURN_ROUTER_TIMEOUT_MS` deadline: a history read that hangs
runs the normal path with `fallback_reason = 'timeout'`.

A routed turn is visible from its route on: `GET /turns/{id}` answers `running` (and `/events` streams)
as soon as the route step recorded its `routed` event, although an intent plan writes the turn's
row only together with its reply. It is not shown running for ever: with still no row, a turn whose
workflow recorded `failed`, or whose route is older than 15 minutes
(`ROUTED_TURN_ROW_MAX_AGE_SECONDS`), answers `failed` - a status `GET /turns/{id}` gives only
there. Once the row exists (DBOS recovered the workflow) the row answers again. The CLI console's
durable poll does not know `failed` and waits out its timeout.

An intent plan's reply is ONE tool-less model call (the compose). It is bounded per deployment:
`AGENT_CORE_COMPOSE_MAX_TOKENS` (default 4096, reasoning included on providers that count it there)
and `AGENT_CORE_COMPOSE_REASONING_EFFORT` (`minimal`/`low`/`medium`/`high`, default `low`; set it
EMPTY to send no effort, for a provider or a LiteLLM proxy that refuses `reasoning_effort`). A
compose the provider refuses with a 400 naming the parameter is sent once more without it, and the
log says `the provider refused the compose reasoning effort` (each compose pays that refused call
until the variable is emptied). A compose cut at the
bound, or with no text, falls back too. Measured on the glazed deployment (reasoning model, L18):
unbounded composes spent 7-17k reasoning tokens (47-112 s); `low` effort took one from 33-38 s to
3-7 s.

What the router did, per turn:

```postgresql
SELECT turn_id, requested_profile, profile_id, decision, label, confidence, calibrated, model,
       elapsed_ms, fallback_reason, reason, input_tokens, output_tokens, at
FROM audit_turn_routes ORDER BY id DESC LIMIT 50;
```

A route that lands runs delegated (D98): the routed turn is policed as a peer turn of the target
(`peer-agent` on the `peer` channel), on behalf of the person. Both identities are on the trail
(migration `0048`, additive): `audit_turn_routes.actor` / `on_behalf_of`,
`audit_turn_intents.actor` / `on_behalf_of` and `audit_tool_calls.on_behalf_of` (its `caller` is
the subject the call ran as). An approval in a routed turn is requested by the person
(`COALESCE(on_behalf_of, caller)`), so four-eyes is unchanged. No new step and no new pinned
version: a turn recovered across this deploy reads its recorded identity back.

```postgresql
SELECT turn_id, caller, on_behalf_of, tool, effect, rule_id, at
FROM audit_tool_calls WHERE on_behalf_of IS NOT NULL ORDER BY at DESC LIMIT 50;
```

`reason` is the router's own code for what it answered (the System One router: `escape`,
`below_threshold`, `missing_api_key`, `http_<n>`, `timeout`...) and `input_tokens` /
`output_tokens` what the consultation spent; NULL when the router said nothing (migration `0046`,
additive).

`fallback_reason` is `timeout`, `error:<ExceptionClass>`, `target_not_allowed`,
`target_not_startable`, `intent_not_supported` (the profile did not opt into intents, or no addon
declares plans), `intent_unknown` (no plan for that intent) or `audit_unavailable` (a route is
applied only after its audit row is written).

### Intent plans (D97)

An addon's `provides.intents` plans run when routing is on and the entry profile says
`routing.intents: true` (docs/ADDONS.md, "Intent plans"). Startup stops when a plan's owner is not
loaded, cannot be started by a channel, is typed or lacks a step's tool. No drain: a turn without
an intent records exactly the steps it did under `.12`, and migration `0047`
(`audit_turn_intents`) is additive. How the plans did:

```postgresql
SELECT intent, plan_used, fallback_reason, count(*), avg(tool_count)
FROM audit_turn_intents GROUP BY 1, 2, 3 ORDER BY 1, 2 DESC;
```

The plan's tool calls are ordinary `audit_tool_calls` rows of the turn; the feed shows a `planned`
event (`plan` or `fallback:<reason>`) after `started`. The event feed shows a `routed` event before `started`, its `detail`
`routed`, `passed` or `fallback:<reason>`, never the label.

### Subagents: looking and stopping

`spawn_subagent` is off until a profile says `subagents: {enabled: true, agents: [...]}` AND a policy
rule allows the tool (`tool_pattern: spawn_subagent`, optionally narrowed with `resource: <agent id>`;
a `deny` for an agent hides it and wins over any allow). The spawned profile must have `mode:
subagent` (or `all`) AND list the spawner in its own `subagents: {accept_from: [...]}` (empty means
nobody, whether or not the worker itself may spawn; D64). A subagent is a row of `peer_messages` with `kind = 'subagent'`:
`SELECT correlation_id, target_agent_id, subagent_depth, state, cancel_reason FROM peer_messages WHERE
kind = 'subagent' ORDER BY enqueued_at;` The peer worker must poll the subagent's queue: it does for
any profile with `mode: subagent` and for any agent a profile with `subagents.enabled` lists. The
graph (`GET /turns/<id>/graph`) labels each node `kind: peer | subagent`. A background subagent is
listed, checked and stopped with `peer_list`, `peer_status` and `peer_stop`; `peer_message` refuses
it. Aborting the parent (`turn_workflow.cancel_turn`, a reply timeout, an expiry or `fail_fast`)
cancels its subagents exactly as it cancels peers.

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
A SNAPSHOT read that fails is retried at the next idle period, at most 5 times in a row (D64): then
the stream sends `event: error` (`{"error": "snapshot_unavailable"}`) and ends, and the client
reconnects. The count of open asks is exact even for a tree with more asks than one read returns
(`count_open`, a SQL count), so such a tree still settles.
The stream ends only when a snapshot shows the tree settled (no running turn and no queued or running
ask under it, so a background ask keeps it open), and every 5 s of quiet (or the keepalive interval,
if shorter) the feed reads a snapshot, so a missed event is corrected by the next one. A tree of more
than 2000 events is shown from its newest events; the snapshot says `"truncated": true` and
`omitted_events`.

### Streaming replies (C26, D58)

Off by default; `AGENT_CORE_STREAM_REPLIES=true` turns it on for the process (the `serve` process and
anything else that runs person-started turns). It changes how the model is called for those turns (a
streaming request), so switch it on where a front-end reads the feed, and check that the provider or
LiteLLM proxy streams. On the feed, a turn then emits `event: delta` frames
(`data.text` = a chunk, `data.detail` = `<run>:<response>:<index>`) and every snapshot has
`turns[].reply_text`. A client speaks `text` as chunks arrive; when a chunk's run or response
differs from the previous one, discard what it held for that turn (the step was retried, or the
model answered again after a tool call) and start over from `reply_text` of the next snapshot. The
recorded answer (`GET /turns/{id}`) is the truth; the chunks are a copy.

A routed turn (turn routing, D95/D98: a route to a specialist or an intent plan, run on behalf of
the person) is the person's turn and streams like one, the plan's compose included; an agent's
peer turn never streams. When a plan's compose is cut at `AGENT_CORE_COMPOSE_MAX_TOKENS` or fails
while it streams, the turn falls back to the normal path and the compose takes its text back with
ONE empty chunk (`text` = `""`) of the next response of its run: by the rule above a client
discards what it held, and the snapshot's `reply_text` is empty until the fallback's reply streams.

Cost: about one `turn_events` row per sentence of a reply (a chunk is at most 240 characters).
They are trimmed with the other events (see "Progress events: looking and cleaning"); a feed that
reconnects after trimming gets a snapshot with whatever remains. A turn that suspends for a person
and is resumed finishes without streaming. If publishing fails, the log says `could not publish the
reply of turn ... to the event feed` once for that run and the turn is unaffected.

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

## The peer answer cache (C25, D57)

Off by default, and it takes TWO opt-ins: the deployment (`AGENT_CORE_PEER_ANSWER_CACHE=true`) and
the answering profile (`peers.answer_cache_seconds: <1-86400>`, with `peers.answer_cache_scope:
subject | roles`, default `subject`). With both, an `ok` answer from that profile is remembered in
`peer_answer_cache` (migration `0039`, additive) and handed to the same asker for the same question
until it expires, without running the peer. The envelope says `cached="true"` and has no `session`.

What is and is not cached: a foreground ask with no follow-up and no conversation context (so not
with `peers.visibility` other than `none`); never an error, a timeout, a refusal, a cancellation or
a typed answer that broke its schema; never a background ask. The policy gate (allowlists, hops,
per-peer rules, approvals) runs BEFORE the lookup, so a peer an asker may no longer ask stays refused.

Ways an entry stops being served: its TTL; the answering profile changing (its content hash is part
of the key); `AGENT_CORE_PEER_CACHE_DATA_VERSION` changing (set it to a label for the data load and
change it after the next one); or an explicit purge:

```
python -m agent_core peer-cache purge --tenant <tenant> [--target <agent>]
```

It prints how many entries went (exit 2 if the database cannot be reached). The table holds a digest
of the key and the callee's wrapped answer, never the question. Expired rows are removed by the next
write; nothing else needs scheduling. A cache that cannot be read or written costs a miss, logged
at WARNING (`the peer answer cache could not be read/written`), never a turn.

To see the effect: a cache hit has no peer run, so the asking turn has no `peer_messages` row for it
and the peer has no `started` event; compare the asks per minute with the C24 query above.

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

## Tool calls a crash interrupted (replay policy, C30, D65)

A tool declares `replay: safe | interrupted` in its metadata (`Tool(fn, metadata=...)`; the default
is `interrupted`). A call of a tool that is not `safe` is written to `tool_executions` (migration
`0043`, additive: nothing else reads it, no app-version bump) before its body runs and marked
finished after it. If a turn step is repeated after a crash and meets a call it already knows (the
same turn and the same call key, which an approved call keeps across the approval), the
tool is NOT run again: the model is told `Tool call interrupted and not repeated ...` and decides.

- What was interrupted: `SELECT turn_id, tool_call_id, tool, started_at FROM tool_executions WHERE
  finished_at IS NULL ORDER BY started_at;` A row stays unfinished when the process died inside the
  tool; it may or may not have taken effect, which is why the model is told to check first.
- The rows are small and are the evidence of an interruption; trim them on your own schedule:
  `DELETE FROM tool_executions WHERE started_at < now() - interval '30 days';`
- Declare a tool `safe` only if running it twice is harmless (it reads, or it only defers). The
  built-in `ask_peer`, `spawn_subagent`, `request_evidence`, `knowledge_search`, `peer_list`,
  `peer_status` and `peer_stop` are safe; `peer_message` and an unclassified tool (a vertical's, an
  MCP server's) are not.
- The `tool_call_id` column holds the call KEY `j1:r<n>:<provider id>` (`j1` is the key version, D69):
  `<n>` is the number of model responses in the history when the call ran, so a provider that reuses an id in a later response
  (D68) no longer has its second call answered `interrupted`. Upgrading from a build that wrote the
  bare provider id (or from the unversioned `r<n>:` key): a call that began under the old key and is replayed after the upgrade is not
  recognised (it looks fresh), so drain first (let turns that are inside a non-safe tool call
  finish, or cancel them) when that matters. No app-version bump: recorded steps are unchanged.
- A journal that cannot be reached when a call begins (the pool checkout waits at most 5 s,
  `TOOL_JOURNAL_CHECKOUT_TIMEOUT_SECONDS`, or the database is down) fails the tool call with
  `ToolJournalUnavailableError` before the tool runs (the turn step fails and is retried like any
  other failed step); there is no silent fallback to running unguarded. If it cannot be reached
  AFTER the tool ran, the result is kept and a warning is logged (`tool_journal_finish_failed`); the
  row stays `started`, which a replay reads as interrupted. If a deferral (approval, outside
  answer) cannot forget its row, `tool_journal_abandon_failed` is logged at ERROR with the turn,
  tool and key: the approved run will answer `interrupted` for that call, so delete the row
  (`DELETE FROM tool_executions WHERE turn_id = ... AND tool_call_id = ...`) before approving, or
  ask the person to repeat the request. Alert on that token.

## Provider cassettes for adapter tests (C31, D66)

`Core/tests/fixtures/cassettes/*.json` hold recorded HTTP exchanges with a model provider;
`tests/support/http_cassette.py` (`CassetteTransport`) replays them through the `http_client` seam of
`llm_litellm.models.model_for(...)`, so an adapter test runs the real request-building and
response-parsing code with no network and no cost.

- Replay is the default and needs nothing. A request the cassette does not hold fails as
  `CassetteMiss` (never as a network call).
- To record or refresh one, on a developer machine with the provider's key in the environment (never
  committed, never printed): `AGENT_CORE_RECORD_CASSETTES=1 pytest <test>` with the test's client
  built as `httpx.AsyncClient(transport=CassetteTransport(path))`. The cassette is written
  REDACTED: credential headers masked, other request/response headers dropped, secret-named query
  parameters masked, and every string scrubbed of the request's own credential, of the value of
  every `*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*` environment variable and of key-shaped text
  (`sk-...`, `Bearer ...`). Read the diff before committing: redaction cannot know about a secret
  that appears only as ordinary text and is not in the environment.
- The first import of `litellm` fetches its model-cost map from the internet; export
  `LITELLM_LOCAL_MODEL_COST_MAP=True` (the cassette tests do) wherever that must not happen.

## The eval harness (C32, D67)

`make eval` runs scripted multi-agent scenarios (`Core/evals/`) through the production runner and
writes `report.json` and `report.md`. It is not part of `pytest` (`testpaths = ["tests"]`) and it is
opt-in, because against a real model it takes minutes and costs money.

- Nothing set: `eval skipped: set AGENT_CORE_EVAL=1 ...`, exit 0, no files.
- Real model: `AGENT_CORE_EVAL=1` plus the provider's key in the environment (`MINIMAX_API_KEY` for
  the default `minimax/MiniMax-M3`). A missing key is a skip that names the variable.
- Offline, for the machinery: `AGENT_CORE_EVAL_MODEL=scripted make eval` (the scripted fake model,
  no key, no network).
- Knobs: `AGENT_CORE_EVAL_MODEL` (litellm id), `AGENT_CORE_EVAL_REPETITIONS` (default 3),
  `AGENT_CORE_EVAL_SCENARIOS` (comma list), `AGENT_CORE_EVAL_REPORT_DIR` (default
  `Core/evals/reports`, git-ignored), `AGENT_CORE_EVAL_TRIAL_TIMEOUT` (seconds, default 180),
  `AGENT_CORE_EVAL_MIN_PASS_RATE` (exit 1 below it, default 0).
- A real model is not deterministic: read the pass rate over the repetitions, not a single trial.
  A trial that raises or times out is a failed trial with its error in the report; the report never
  contains keys, prompts or the environment.
- To add a scenario: an async function in `evals/scenarios.py` that drives agents through
  `PydanticAgentRunner` and returns `CheckResult`s, plus the scripts the scripted model plays.

### The routing benchmark (Phase 5 P4, D95)

`python -m evals.routing_cli` (from `Core/`) measures a turn router against a labelled question
set: each question names the route it should take, `pass`, `profile:<id>` or `intent:<name>`. The
set runs twice, routing off (every turn on the entry profile) and routing on, and `routing.json`
and `routing.md` report accuracy, accuracy over what the router committed to, the confusion
(expected x got), the fallback rate (timeout, error), the abstention rate (the router said pass)
and p50/p95 latency of the router and, when whole turns run, of the turn. The set format (YAML or
JSONL) is documented in `Core/evals/routing.py`; a vertical ships its set in its addon (for
example `addons/glazed_vertical/evals/routing_questions.yaml`, synthetic until real recorded
questions replace it).

- Offline, no opt-in: `AGENT_CORE_EVAL_ROUTING_SET=<set file>` alone runs the set's own
  `keyword_router` rules. A floor and a check of the machinery, not a real router.
- A real router (an addon's `provides.routers`): `AGENT_CORE_EVAL=1 AGENT_CORE_EVAL_ROUTER=<name>`
  plus the addon variables Core reads (`AGENT_CORE_ADDONS`, `AGENT_CORE_ADDONS_DIR`, the
  `AGENT_CORE_ADDON__<ADDON>__<KEY>` settings). Without `AGENT_CORE_EVAL=1` it is a clean skip.
- Whole turns: add `AGENT_CORE_EVAL_TURNS_URL=<a running Agent-Core>` (and
  `AGENT_CORE_EVAL_TURNS_TENANT`/`_SUBJECT`/`_CHANNEL`, `AGENT_CORE_EVAL_TURN_TIMEOUT`). Each
  question is a real turn: on the profile its route names, else on the entry profile. An intent
  question is posted to the entry profile so the deployment's router and intent plan answer it
  (D97: routing and `routing.intents` on there); the driver then reads the turn's snapshot from
  `GET /turns/{id}/events` and the report counts `plan / fallback / no plan` per run. A deployment
  with routing on also routes the baseline run's turns: compare turn latency across two runs
  (deployment routing off, then on). It costs model calls.
- Knobs: `AGENT_CORE_EVAL_ROUTER_TIMEOUT_MS` (default 1500, as production),
  `AGENT_CORE_EVAL_REPORT_DIR` (default `Core/evals/reports`), `AGENT_CORE_EVAL_MIN_ACCURACY`
  (exit 1 when routing-on accuracy is below it).
- The later real run (not done yet): a classifier router addon enabled, the glazed set, whole
  turns against a deployment with the glazed vertical: once with routing off in that deployment
  (the benchmark routes on the client side, posting each turn where the route lands), once with
  routing and intents on (intent questions run their plans).

## Addons: enabling them and setting them (Phase 2, D70-D72)

This section is the operator's side. Writing an addon: [ADDONS.md](ADDONS.md).

| Variable | Meaning |
|---|---|
| `AGENT_CORE_ADDONS` | Comma list of the addons to enable (`sample_base,sample_extra`). Default: none. |
| `AGENT_CORE_ADDONS_DIR` | Where the addons live. Default `<repository>/addons`; set it for an installed wheel. |
| `AGENT_CORE_ADDONS_CONFIG` | Optional YAML file: `addon_name: {setting: value, group: {key: value}}`. |
| `AGENT_CORE_ADDON__<ADDON>__<KEY>` | One setting, upper case, `__` between segments (`AGENT_CORE_ADDON__SAMPLE_BASE__MAX_ITEMS=8`). Wins over the file. |

- Installing or removing an addon needs a restart. Startup stops, with a message naming the addon
  and the field or key, when: a manifest is invalid; a listed addon has no directory; a dependency
  is not listed (list it: dependencies are never enabled for you); there is a cycle; the addon's
  `core` range does not contain this Core's version; a setting is invalid, unknown or missing; or a
  setting (file or variable) names an addon that is not active.
- A bridge (`auto_install: true`) needs no entry in the list: it activates when every addon it
  depends on is active. Do not set its variables unless that is the case, or startup stops.
- A wrong value is never printed back (it may be a secret); the message names the variable instead.

### What an enabled addon registers (A4, D74)

An addon contributes exactly what `provides` in its `addon.yaml` declares. No new variable: the
list above is the only switch, and everything below happens at startup (build and `start_container`).

| `provides.` | Where it lives in the addon | Registered as |
|---|---|---|
| `toolsets: [name]` | `toolsets/<name>.py` with `build_toolset()` or `build_toolset(settings)` returning a `FunctionToolset` | a toolset a profile lists in `toolsets:` |
| `audit.allow` / `audit.presence_only` | `addon.yaml` only (`tool: [argument, ...]`); the toolset module may NOT declare `AUDIT_ALLOW` | audit argument policy, for the addon's own tools only |
| `policy: path` | one rules file, same format as `Core/policy/*.yaml` | rules reconciled with Core's; `allow` rules may name only the addon's own tools |
| `fragments: dir/` | `<name>.md` files | names a profile can put in `include:` |
| `profiles: dir/` | `*.yaml` profile files | profiles served next to Core's |
| `inserts: [{profile, slot, fragment}]` | nothing (uses one of the addon's own `fragments`) | that fragment's text at the profile's `{{slot:name}}`, after the slot's defaults, in activation order |
| `wrappers: [{tool, wrapper}]` | `wrappers/<wrapper>.py` with `wrap(original, arguments, ctx)` | the tool runs through `wrap`; `await original()` calls the tool (or the wrapper loaded before) |
| `channels: [id]` | nothing (no code) | pull-mode channel ids, like `AGENT_CORE_PULL_CHANNELS` |

- Startup stops, naming the addon and the `provides.*` key, when: a declared file or directory is missing
  or empty, a toolset fails to import or its `build_toolset()` raises, an addon profile is not valid YAML; a toolset, tool, fragment, profile id, channel or rule id is already used by Core
  or by another addon (or, for a channel, listed in `AGENT_CORE_PULL_CHANNELS`); an audit entry names
  a tool the addon does not export, or contradicts an existing decision; an `allow` rule reaches a
  tool the addon does not own (`deny` and `needs_approval` rules may name any tool); or the addon
  contributes rules but the deployment has no policy directory.
- An addon's rules are part of the reviewed set: removing the addon from `AGENT_CORE_ADDONS` and
  restarting revokes them, exactly like deleting a rule from `Core/policy/`.
- `:reload` in the console and `preflight` see the addons' profiles and toolsets too.

### Tool wrappers (A6, D78)

```yaml
# addons/shop_audit/addon.yaml
depends: [shop]
provides:
  wrappers: [{tool: cart_add, wrapper: stamp}]
```

```python
# addons/shop_audit/wrappers/stamp.py
async def wrap(original, arguments, ctx):
    result = await original()          # the tool, or the wrapper loaded before this one
    return f"{result} (checked)"
```

The tool keeps its name, schema and replay class; policy rules, approvals and the audit row still
apply to the tool name and run BEFORE the wrapper, which therefore never sees a refused call. The
audit row is written once and lists the wrapping addons in its arguments as `_wrapped_by`.
`original()` takes no arguments: it runs with the arguments the policy decided on. Wrappers on one
tool nest in activation order (the last loaded is outermost). A wrapper may wrap a tool of Core or
of an addon it depends on (directly or not), never its own or an unrelated addon's: startup stops
naming the addon otherwise. An exception from wrapper code reaches the model as a tool error (the
exception text goes to the log, not to the model); the original tool's own errors are unchanged,
even when the wrapper retries `original()` and lets the last failure through (D79). The audit key
`_wrapped_by` is set by the runner only: a value the model sends under that name never takes its place. A tool that really receives an argument by that name has it audited as `_wrapped_by_received`, under the tool's own audit decision for `_wrapped_by` (D81).

### Install state: versioned migrations and `post_init` (A7, D80)

```yaml
# addons/shop/addon.yaml
version: 1.1.0
migrations: migrations/         # 0001_items.sql, 0002_label.sql: applied in name order
hooks:
  post_init: hooks:post_init    # addons/shop/hooks.py, function post_init(context)
```

After Core's schema is applied (migration `0044_addons` creates the `addons` and `addon_migrations`
tables) and before policy is reconciled, startup brings each enabled addon, dependencies first, up
to its manifest:

1. Every `migrations/*.sql` file not yet recorded runs, in file-name order, each in its own
   transaction together with its record. A file may hold several statements. Name files with a
   zero-padded prefix; never edit a file that already ran (it is not run again, and nothing
   compares it): add a new one.
2. If the addon is new or its manifest `version` differs from the recorded one, `post_init(context)`
   runs and the version is recorded in one transaction. `context` has `name`, `version`,
   `previous_version` (`None` on first install, the old version on an upgrade), `settings` and
   `connection` (inside that transaction: write through it, do not commit or close it). A hook must
   be safe to run twice, because a failure re-runs it on the next start.

`post_init` must be a plain function: an `async def` hook is refused when the addon is loaded (it would be called without being awaited). Versions are compared by number (`1.10` is newer than `1.9`, `1.0` equals `1.0.0`).

Startup stops, naming the addon and the file or hook, when: the database becomes unreachable during the install pass (`AddonInstallError`, field `database`); a migration file fails (that file is
rolled back, the earlier ones stay, the next start resumes there); `post_init` raises, is missing
or does not import (the version is not recorded); or the database holds a NEWER version than the
manifest (a downgrade: nothing runs for any addon, deploy the installed version or restore a
backup). Two processes starting together do each step once (a session advisory lock; the second
waits and finds it done). With no addon enabled nothing but the empty tables exists.

Removing an addon from `AGENT_CORE_ADDONS` leaves its rows and every table its migrations made; if
it returns it continues where it stopped. Uninstalling is deliberately manual (drop its tables, then
`DELETE FROM addon_migrations WHERE addon = 'x'; DELETE FROM addons WHERE name = 'x';`).
`python -m agent_core addons list` is read-only: for each installed or enabled addon it prints the
recorded and manifest versions and what the next start does (current, installs, upgrades, refuses,
or kept because not enabled). Exit code 0, or 2 when the database cannot be read.

### Profile inheritance and slots (A5, D76)

Any profile file (Core's or an addon's) may build on another:

```yaml
id: shop_specialist
inherit: shop_assistant      # a Core profile or an addon template; at most 4 levels
max_iterations: 3            # scalars replace
toolsets: [refunds]          # lists append (parent first, no repeats); mappings merge
replace: [skill_namespaces]  # swap these top-level fields wholesale (never id, approval_rules)
slots: {before_tools: [house_style]}   # add default fragments to an inherited slot
```

`id`, `public_alias` and `peers.topology` are never inherited. A profile that wants addons to extend
it declares `slots: {before_tools: []}` and puts a line `{{slot:before_tools}}` in its persona.
Startup stops, naming the profile (and the addon), on: an unknown parent, a cycle, more than 4
parents, an unknown slot or profile in an insert, a slot with no marker (or a marker with no slot),
`replace` of a field the patch does not carry. With none of this used nothing changes. Editing a
parent gives every child a new profile version.

A child that sets its own `persona` must keep a `{{slot:name}}` line for every inherited slot that
holds content (defaults or addon inserts), or startup stops naming the slot and who filled it (D77);
an empty inherited slot may be left out. An empty slot removes only its own marker line. A
`{{slot:x}}` line in a profile that neither inherits, declares slots nor receives an insert is plain
text. In a profile that does, a marker must be alone on its line: `see {{slot:x}} here` stops
startup naming the slot (D79).

