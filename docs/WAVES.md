# Waves — how implementation is scheduled

This file is the execution plan. `docs/TASKS.md` says *what* to build and owns the task
anchors; this file says *in what order and how many at once*. When the two disagree about
ordering, this file is the one that is wrong and must be regenerated — `TASKS.md` owns the
anchors.

Scheduling exists because implementation runs as parallel waves of subagents, per the
amendment to the standing sequencing preference in `docs/DECISIONS.md`.

## The two rules that make a wave safe

1. **Dependencies satisfied.** Every anchor a task depends on landed in an earlier wave.
   Dependencies are derived from real constraints, never from table order: layer direction
   (`domain/` needs nothing, `ports/` needs the domain types it names, `application/` needs
   domain plus ports, an adapter needs its port frozen, `composition.py` needs every adapter
   it wires), a fake needing its protocol frozen, and any explicit gate language in the task.

2. **Writes are disjoint.** No two tasks in one wave write the same file. This is the rule
   that actually prevents corruption, and it is why the schedule tracks a `writes` set per
   task. Two anchors pointing at the same module go in different waves even when neither
   depends on the other.

   This rule can only be checked against a **file**. Six anchors once named a package, and
   against a directory the question has no answer — `docs/TASKS.md` now requires a module
   path, and that requirement exists for this rule. The same reasoning retired
   `migrations.py` as a shared write: seven upcoming anchors create a table, so their ids
   are pre-allocated in `docs/TASKS.md` and each defines its `Migration` in its own module.
   Otherwise all seven would collide, in every wave they appear in, over a list.

A wave is a barrier: every task in it finishes before the next wave starts. Inside a wave
there is no barrier and no ordering.

3. **The orchestrator flips the status, not the agent.** `docs/TASKS.md` is the index of
   truth, and after waves 8 and 9 it was lying: twenty-three anchors had landed and every
   row still read `TODO`. A wave agent cannot fix that itself — every agent in a wave would
   be writing the same file, which is the one thing rule 2 forbids. So reconciling the
   index is the orchestrator's job at the barrier, and it happens before the next wave is
   scheduled: a schedule derived from a stale index re-runs work that is already done.

4. **A task that widens a port owns every implementation of it, in the same wave.** Three
   waves in a row ended with a barrier cleaning up a port that had moved under its adapters:
   `AuditSink` grew `record_rejected_decision` and eighteen mypy errors appeared in eleven
   files; `ModelGateway.classify_error` grew an `attempt` parameter and broke four stubs;
   `MediaStore.signed_url` changed its return type and left eight errors across four.
   Each time the widening was CORRECT, and each time the wave still ended red.

   The cause is not the agents. Every one of them reported the collateral accurately and
   declined to touch files outside its writes set, which is rule 2 working exactly as
   intended. **The cause is the writes set, and the writes set is the orchestrator's.** A
   port and its implementations are one work unit; splitting them across a barrier
   guarantees a red wave.

   So before scheduling any task that touches `ports/`, grep for the Protocol's name and put
   **every** implementation, every fake in `Core/tests/fakes/`, and every local stub inside a
   test module into that one task's writes set. If that makes the set large, the set is
   large - it is one change. If it then collides with another task, those two cannot share a
   wave, which is rule 2 doing its job rather than an inconvenience.

   A rename is the same thing: `ask_peer_result_for` became `ask_peer_result` and left a test
   red for exactly this reason.

5. **Never `git checkout --` a file in a wave.** An agent proving mutation teeth restored its
   one-line change with `git checkout -- runner.py` and reverted **765 lines of uncommitted
   work**, because `HEAD` in this repository is ten waves behind the working tree. It
   reconstructed the file from the session transcript and verified it — 1,416 lines, every
   anchor present, 31 green tests — but that recovery was luck wearing diligence's clothes.

   A mutation is restored by **writing the original bytes back**: keep a copy before you
   mutate, restore from the copy, and confirm with `git diff --stat` that `src/` shows only
   what you meant. `git checkout --` does not restore a mutation, it restores `HEAD`, and
   those are the same thing only in a repository that is committed.

   The orchestrator's half of this rule is the real one: **commit at every barrier.** The
   blast radius of that command was ten waves wide because nobody had reduced it. A green
   suite is not a save point.

## Test-first is enforced per task, not per wave

Every wave agent follows the same contract:

1. Write the failing test first. Put it where the suite already lives — `Core/tests/unit/`
   for anything needing no database, network or model; `Core/tests/integration/` otherwise.
2. Run it and confirm it fails **for the intended reason**. A collection error or an import
   error is not a red test.
3. Implement the minimum that makes it pass.
4. Run the test again, then `ruff` and `mypy` over what was touched.

**A test that passes before the implementation exists is a broken test.** The task stops
there and reports it rather than continuing — a green-on-empty test is worse than no test,
because it certifies nothing while looking like coverage.

This matters more here than in most projects. `CLAUDE.md` lists five areas where a green
suite proves nothing — DBOS step determinism, the compaction strategy, the policy engine,
untrusted-content wrapping, and tool call/return pairing. Tasks touching those are marked
`fine` below and get the stronger model, and they still need the manual check that table
describes.

## Which model runs a task

| Class | Model | What qualifies |
|---|---|---|
| `fine` | Opus | Freezing a port Protocol; domain invariants; security boundaries (the no-knowledge-write rule, `AdminIdentity` separation, peer answers as untrusted content); anything in the silent-bug table. |
| `general` | Sonnet | Implementation against a contract that is already frozen, wiring, and adapters with no judgement left in them. |

The split is about how expensive the mistake is, not how much code it is. A wrong Protocol
signature is cheap to type and expensive to discover four waves later; a repository method
is the opposite.

## Wave 0 — contract settlement

Runs alone, before anything is implemented, because freezing a contradictory contract
propagates that contradiction into every wave below it. Wave 0 resolved:

- the sync/async inversion, where the stubs declared sync `def` against D13's async rule;
- the `ToolPolicy` mismatch, where `start_turn.py` called `filter_toolset(caller, names)`
  against a port declaring `filter_toolset(rules, tool_names)`, and where `decide` carried
  no caller while `RuleSet.applicable` required roles and channel;
- whether F1 may depend on domain types and ports owned by F5 through F9.

## The F0 and F1 schedule

| Wave | Anchors | Why it is safe to run at once |
|---|---|---|
| 1 | `t-f1-01` `t-f1-02` `t-f1-03` `t-f1-04` `t-f1-16` | Pure `domain/` plus the DDL. Five files, no external imports, no cross-dependency. |
| 2 | `t-f1-05` `t-f1-06` `t-f1-07` `t-f1-08` `t-f1-09` `t-f1-10` `t-f1-17` | One Protocol per file over wave-1 domain types. `t-f1-17` writes `domain/profile.py`, which no port file writes. |
| 3 | `t-f0-01` `t-f0-04` `t-f1-13` `t-f1-14` `t-f1-15` | Ports are frozen, so fakes and adapters build against a fixed contract. Three repositories, three separate files, one already-migrated schema. |
| 4 | `t-f1-11` `t-f1-12` `t-f1-18` | Use case, runner adapter and the version-persistence edit — three disjoint files downstream of frozen ports and the fakes. |
| 5 | `t-f0-02` `t-f1-19` | Composition wires adapters that now all exist; the versioning test only reads wave-3 and wave-4 behaviour and writes a new module. |
| 6 | `t-f0-03` | Alone. The HTTP route is the only consumer of the container and is F0's end-to-end proof. |

### Wave 7 — the guard tests

Not in the original schedule, and that was the schedule's mistake. `docs/TASKS.md` carries
no anchor for the tests that were shipped skipped with a phase marker on them, so the wave
plan derived from it inherited the blind spot. Three tests marked F1 were still skipped
after wave 6:

| Test | What it guards |
|---|---|
| `test_contract.py::test_domain_and_application_import_nothing_external` | the layer rule itself |
| `test_policy.py::test_deny_beats_needs_approval_beats_allow` | effect precedence, order-independent |
| `test_policy_repository.py` precedence set | the *real* reducer, and that the audit trail names the rule that actually won |

The contract test failed on its first real run and found a genuine layer violation. Two
lessons, both now folded into the rules above:

- **Enable a guard test the moment its precondition holds**, not at the end of the phase.
  This one said "enable once domain and ports are populated" and would have caught the
  violation as it landed instead of after the whole phase was written.
- **Sweep for phase-marked skips before declaring a phase done.** `pytest -rs` grouped by
  reason is the check; a phase is not finished while a test carries its marker.

**And the sweep belongs at the front, not the end.** Both lessons above are end-of-phase
checks, and the blind spot recurred anyway: a dependency pass over the anchors still open
found nine more items carrying a phase marker and no anchor — `_step_compact`, two routes
listed in `routes.py`'s header, and four more skipped tests in `test_durability.py`, two of
which *are* the F3 and F7 done-when criteria. `docs/TASKS.md` now carries an anchor for
each. A phase whose acceptance criterion is an unowned skipped test cannot be finished,
only declared finished — and no wave can be scheduled over work this file's source of truth
does not contain. Run the sweep before scheduling a phase, and read docstrings and module
headers as well as skip reasons: an `R`-rule and a route in a header are the same claim as
a marker.

### The serial spine

These can never share a wave, and each line says what breaks if they do.

- `t-f1-02` → `t-f1-06` → `t-f1-15` → `t-f1-12` — policy semantics, then the frozen
  protocol, then the store, then the enforcement point. Every link changes what the next
  one is allowed to assert.
- `t-f1-04` → `t-f1-17` → `t-f1-18` → `t-f1-19` — profile shape, version, persistence of
  it, then the audit-reproduction test. `t-f1-04` and `t-f1-17` also both write
  `domain/profile.py`.
- `t-f1-13` → `t-f1-18` — same file, `conversation_repository.py`.
- `t-f1-16` → `t-f1-13`, `t-f1-14`, `t-f1-15` — no repository is testable against a schema
  that does not exist.
- `t-f0-01` → `t-f1-11`, `t-f1-12` — one fakes file, and test-first makes every use-case
  and adapter test depend on it. It is the widest bottleneck in F1.
- every adapter → `t-f0-02` → `t-f0-03` — composition imports each concrete adapter, and
  the route exists only to call the container.
- `t-f1-02` and `t-f1-15` both write `Core/tests/unit/test_policy.py`. Different waves, or
  split the module.

### The F0 and F1 ordering inversion

`t-f0-01` fakes `AgentRunner` and `ToolProvider`, and `t-f0-04` implements `ModelGateway`,
but those protocols are frozen at `t-f1-05`, `t-f1-07` and `t-f1-10`. F0's own note says to
build the fakes before the adapters, and a fake needs its protocol frozen — with F0 strictly
before F1, both cannot hold. The schedule above resolves it by dependency order rather than
by phase label: the F0 anchors land in waves 3 through 6. F0's done-when criterion is
unchanged and is still proved by `t-f0-03`.

## Phase shape beyond F1

Per-anchor scheduling is worked out only for F0 and F1. Beyond that this is the overlap
shape, to be expanded into a real wave table when the phase is next.

| Phase | Overlaps the previous phase? | Why |
|---|---|---|
| F2 durability | No | The workflow wraps `StartTurn` in steps; step determinism cannot be judged before the use case is final. F2 is mostly serial internally — its anchors collide on `turn_workflow.py`. |
| F3 deferred human | Partly | The port and the channel adapters are new files needing nothing from F2; the durable-wait anchors are serial after it. |
| F4 first vertical | Partly | The tools package is independent. The contract test must land last, and after the `domain/profile.py` edit it is meant to police. |
| F5 compaction | Mostly | `t-f5-01`..`t-f5-06` depend on nothing in F4: domain types, a port, a use case, the ladder engine and two tests over *simulated* conversations. Only `t-f5-09`, the bill measurement, needs real F4 traffic — a measurement is not a dependency of the code it measures. `t-f5-07` waits on `runner.py`, `t-f5-08` on `turn_workflow.py`. |
| F6 skills and MCP | Mostly | `t-f6-01`..`t-f6-03`, `t-f6-05` and `t-f6-06` are a new port, a new adapter and its tests, and overlap freely. Only `t-f6-04` is serial, and only because it writes `runner.py`. |
| F7 multimodal | Mostly | The domain, port and store lane is new files. `t-f7-05` moved to a shared module and no longer collides with F4's tools package; `t-f7-07` and `t-f7-08` are serial on `runner.py` and `routes.py`. |
| F8 knowledge | Mostly | Its own domain, ports and adapters, except the one anchor that writes `runner.py`. |
| F9 peers | Mostly | Overlaps F8 except the anchor writing `application/start_turn.py`. Depends on F3's suspension, not on F8. |
| F10 transcripts | Mostly | Overlaps F8 and F9, except the anchor writing `conversation_repository.py`. Needs F1 and F2 rows to exist, nothing later. |

**Three rows above were wrong, and the header at the top of this file says why that is this
file's fault.** F5, F6 and F7 were marked as phases that could not overlap. Checked against
the anchors, the collisions were never phase-wide — they were four files. Marking a whole
phase serial because one of its anchors writes a shared module costs every other anchor in
it a wave it did not need, and the reason is invisible: "No" says nothing about which file.
The rows now name the colliding anchors, and the collisions are listed below as spines.

### The serial spines beyond F1

Same shape as F1's: each line is a file, and no two anchors on a line share a wave.

- `adapters/driven/agent_pydantic/runner.py` — `t-f1-12` → `t-f3-10` → `t-f5-07` →
  `t-f6-04` → `t-f7-07` → `t-f8-05`. Six anchors across five phases on one file; it is the
  widest bottleneck after F1, and the reason F5, F6, F7 and F8 each keep exactly one serial
  anchor.
- `adapters/driving/workflow/turn_workflow.py` — `t-f2-01` → `t-f2-03` → `t-f2-10` →
  `t-f3-07` → `t-f5-08`. This is why F2 is mostly serial internally.
- `adapters/driving/http/routes.py` — `t-f0-03` → `t-f2-05` → `t-f3-11` → `t-f7-08`.
- `Core/tests/integration/test_durability.py` — `t-f2-02`, `t-f2-07`, `t-f2-08`, `t-f2-09`,
  `t-f3-12`, `t-f7-09`. One module, six anchors; split it per phase or take one per wave.
- `adapters/driving/channels/registry.py` — `t-f3-13` first, then its registrants
  `t-f3-08` and `t-f3-09`, then `t-f3-06` wires them in `composition.py`. The Protocol has
  to exist before anything can register against it; `docs/TASKS.md` explains why it lives
  in the adapter layer and not in `ports/`.
- `domain/policy.py` — `t-f1-02` → `t-d2-06`, and `t-d2-06` is conditional. Read its
  condition in `docs/TASKS.md` **before** scheduling D2: whether it exists at all decides
  whether `t-d2-03` is one anchor or two, and the policy engine is a silent-bug area.
