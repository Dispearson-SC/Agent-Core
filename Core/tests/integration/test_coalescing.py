"""The partitioned turn queue and the coalescing enqueue in front of it.

Phase:   F2 (durability)
Tasks:   docs/TASKS.md#t-f2-03, docs/TASKS.md#t-f2-05
Covers:  adapters/driving/workflow/turn_workflow.py - the queue and its enqueue helper
         adapters/driving/http/routes.py            - the coalescing enqueue (D19 1-3)

WHAT IS BEING DEFENDED (D19)
    A session is a conversation. Two turns of the SAME conversation running at once read
    and write the same history, so the second one starts from a transcript the first has
    not finished writing - the classic interleaving that produces an answer to a message
    the user has already superseded. Serialising per session is the fix.

    The cost of getting the fix wrong in the other direction is just as bad and much less
    obvious: a queue that serialises EVERYTHING makes every tenant wait behind every other
    tenant's slowest turn, and it looks perfectly healthy under a single-session test.

    On top of that, people split one thought across sends - "hola" / "queria preguntar
    algo" / "sobre mi pedido 123". Three turns would answer the first two pointlessly and
    pay for three model calls to produce one useful answer. So messages arriving inside a
    window join the turn already waiting, and one turn runs.

WHY THIS IS AN INTEGRATION TEST AND NOT A UNIT TEST
    Neither guarantee is code in this repository. Serialisation is
    `partition_concurrency=1` on `dbos.Queue` plus a `queue_partition_key` per enqueue;
    coalescing is `delay_seconds` plus `deduplication_id` with
    `duplication_policy="return-existing"` (docs/FIELD-NOTES.md, dbos 2.31.1). A test with
    a fake queue would assert that the fake honours the options it was handed, which is a
    test of the fake. Only a real DBOS over a real Postgres can say whether the options
    were spelled right, whether they SURVIVE to the enqueue, and whether a delayed,
    deduplicated, partitioned row is ever dequeued at all.

    It skips cleanly when no Postgres is reachable, the same way the other integration
    tests in this directory do.

WHY THE KEY CARRIES THE TENANT
    `SessionId` is unique inside a tenant, not globally. Keying the partition on the
    session id alone would make two tenants that happen to pick the same session id
    serialise against each other - cross-tenant interference that no single-tenant test
    can see. The third pair below is here to pin that.

WHY NOTHING HERE MEASURES WALL-CLOCK OVERLAP ANY MORE - READ THIS BEFORE "FIXING" IT
    The first version of the serialisation test proved concurrency by sleeping a fixed
    half-second inside each turn and asserting that two spans overlapped. It passed 5/5
    alone and failed twice inside full-suite runs under sixteen-way agent load, and the
    reason is a property of the queue, not of the code under test: `dbos.Queue` polls
    once a second, so two enqueues that straddle a poll tick are dequeued a full second
    apart. A half-second hold then cannot overlap, and the test reports "these sessions
    do not run in parallel" when what actually happened is "the machine was busy".

    Widening the hold or the tolerance only moves the load at which it fails, and a guard
    that is re-run until green is worse than no guard. So the timing was removed instead:

      - Parallelism is proved by a RENDEZVOUS. Every turn blocks until three DISTINCT
        partitions are in flight at the same moment. If they cannot be concurrent, that
        condition is never reached and the test fails - it can never be reached by luck,
        and being slow does not make it fail.
      - Serialisation is proved by an OCCUPANCY COUNT taken as each turn enters and
        leaves. A second turn of a partition entering while the first is still inside is
        recorded exactly, whatever the clock says.

    Both properties are now causal. The only remaining deadline is the one that turns a
    hang into a readable failure.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from agent_core.adapters.driving.channels.registry import (
    ChannelRegistry,
    OutboundMessage,
)
from agent_core.adapters.driving.http import routes
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# DBOS keeps its own system tables; it gets its own database so a failed run here can be
# dropped without touching the schema the repository tests use.
_DBOS_DATABASE = "agent_core_queue_test"

# How long a turn may wait for the other partitions before the rendezvous is declared
# impossible. This is NOT a tolerance the assertions depend on: reaching the rendezvous
# is the pass condition and a slow machine only reaches it later. It exists so a queue
# that never dequeues fails with a sentence instead of hanging the suite.
_RENDEZVOUS_DEADLINE_SECONDS = 30.0

# How often a waiting turn looks at the rendezvous. A yield, not a delay: the value
# affects only how quickly a released turn notices, never whether the test passes.
_POLL_SECONDS = 0.01


class _NowhereChannel:
    """A registered channel that does nothing.

    `_step_deliver` (docs/TASKS.md#t-f3-07) routes every finished turn through the
    registry and raises `UnknownChannelError` on a miss, which is correct - an answer
    with nowhere to go must be loud. These tests are about the queue in front of that,
    so the channel exists and drops what it is given, rather than the workflow failing
    for a reason no assertion here is about.
    """

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        return None


def _wired(
    start_turn: object, pending_inputs: object | None = None
) -> turn_workflow.TurnWorkflowDependencies:
    """The workflow's collaborators. The buffer is wired WITH the starter, never apart.

    `t-f2-10`'s drain reads the same table `t-f2-05`'s coalescing enqueue writes. A
    process that wired one and not the other would append every coalesced sentence and
    never read one back - the user's second and third messages would sit in a table
    forever and the reply would answer a third of what they said, with nothing raising.
    """
    if pending_inputs is None:
        return turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", start_turn),
            channels=ChannelRegistry((("http", _NowhereChannel()),)),
        )
    return turn_workflow.TurnWorkflowDependencies(
        start_turn=cast("Any", start_turn),
        channels=ChannelRegistry((("http", _NowhereChannel()),)),
        pending_inputs=cast("Any", pending_inputs),
    )


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo() -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{_DBOS_DATABASE}"


def _drop_databases() -> None:
    """Start every test in this file from an empty DBOS system table.

    NOT tidiness, and not a retry dressed up as a fixture. `agent-core-turns` is
    partitioned with `partition_concurrency=1`, and DBOS's dequeue treats a PENDING
    workflow on a partition as that partition's occupant whatever application version
    wrote it - the mutual-exclusion probe in `_sys_db.py` is "unscoped by design". Local
    recovery is the opposite: it asks for pending workflows of the CURRENT
    `application_version` only (`_dbos.py`). So one run killed mid-turn - a session limit,
    a Ctrl-C, a crashed worker - leaves a row that will never be recovered and never
    releases the slot, and every later turn for that session is accepted, enqueued, and
    sits at ENQUEUED forever.

    Nothing raises. The symptom is silence, and inside this file it surfaced as
    `DBOSNonExistentWorkflowError` from `_await_turn_behind` - a failure that points at
    the coalescing code and is not in it. `test_delivery.py` reproduces the blocking
    causally and refuses any launch helper in this directory that skips this call.

    The window cancellation in `_drive_coalescing` is a separate job and both are needed:
    that one keeps a DELAYED window from firing into a later test's fakes, this one clears
    what a killed run left behind, and neither covers the other.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _SessionOccupancy:
    """Stands in for `StartTurn`, and records who was inside which partition, when.

    A real `StartTurn` needs a model, a store and a policy, and none of them change the
    answer to "were these two turns in the same conversation at the same time?". What
    matters is that the workflow's own `_step_start` is what this replaces, so the
    interval measured is the interval the queue slot was actually occupied.

    Entering and leaving are recorded under a lock rather than timed, because DBOS may
    run two dequeued workflows on different threads and a lost increment would read as
    "no overlap" - a false green on the one property this file exists to prove.
    """

    def __init__(self, *, partitions_expected: int) -> None:
        self._partitions_expected = partitions_expected
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        self._released = False
        self.ran: list[tuple[str, int]] = []
        self.overlapped: list[str] = []
        self.timed_out = False

    def _enter(self, partition: str) -> None:
        with self._lock:
            depth = self._in_flight.get(partition, 0) + 1
            self._in_flight[partition] = depth
            if depth > 1:
                self.overlapped.append(partition)
            occupied = sum(1 for count in self._in_flight.values() if count > 0)
            if occupied >= self._partitions_expected:
                self._released = True

    def _leave(self, partition: str, message: int) -> None:
        with self._lock:
            self._in_flight[partition] -= 1
            self.ran.append((partition, message))

    def _may_go(self) -> bool:
        with self._lock:
            return self._released

    def _give_up(self) -> None:
        # Latched, and it releases everybody: one turn waiting out the deadline is a
        # readable failure, six of them in series is a hung suite.
        with self._lock:
            self.timed_out = True
            self._released = True

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        partition = turn_workflow.session_partition_key(request.session)
        message = request.input.text.count(".")
        self._enter(partition)
        try:
            deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
            while not self._may_go():
                if time.monotonic() >= deadline:
                    self._give_up()
                    break
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            self._leave(partition, message)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


@dataclass
class _RecordingStartTurn:
    """What ran, in a test that cares only about how many turns there were."""

    started: list[TurnRequest] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        with self._lock:
            self.started.append(request)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


@dataclass
class _RecordingBuffer:
    """The pending-input buffer, as far as the coalescing enqueue can see it.

    A message that joined a turn already waiting out its window is not in that turn's
    `TurnRequest` - the enqueue discarded these arguments and handed back the existing
    handle. If nobody buffers it, the user's second and third sentences are simply gone,
    and the reply answers a third of what they said. `t-f2-10` drains this back in.
    """

    appended: list[tuple[SessionRef, UserInput]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def append(self, session: SessionRef, message: UserInput) -> None:
        with self._lock:
            self.appended.append((session, message))

    async def drain(self, session: SessionRef) -> tuple[UserInput, ...]:
        """Hand back this session's buffered messages IN ARRIVAL ORDER and empty it.

        Read-and-remove in one critical section, exactly as the real
        `PgPendingInputBuffer.drain` does it in one `DELETE ... RETURNING`: a read
        followed by a separate removal can hand the same sentence to two turns.
        """
        with self._lock:
            mine = [
                message for buffered, message in self.appended if buffered == session
            ]
            self.appended = [
                pair for pair in self.appended if pair[0] != session
            ]
        return tuple(mine)


def _request(tenant: str, session: str, message: int) -> TurnRequest:
    # The message index travels in the text rather than in a field the domain does not
    # have; the fakes read it back out. Dots, so the value survives verbatim.
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId(tenant)),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="http",
            tenant_id=TenantId(tenant),
            roles=frozenset({"operator"}),
        ),
        profile_id="p-1",
        input=UserInput(text="." * message),
    )


# Two messages on one session, two messages on a second session of the same tenant, and
# two messages on a session of ANOTHER tenant that reuses the first session id.
#
# The order is a literal tuple and never a set: CLAUDE.md non-negotiable #7. Dispatch
# order decides which turn of a partition runs first, and deriving it from an unordered
# container would make this test assert something different on every run.
_DISPATCH: tuple[tuple[str, str, int], ...] = (
    ("t-1", "s-1", 1),
    ("t-1", "s-1", 2),
    ("t-1", "s-2", 1),
    ("t-1", "s-2", 2),
    ("t-2", "s-1", 1),
    ("t-2", "s-1", 2),
)

_PARTITIONS_EXPECTED = 3


class _Dbos:
    """A launched DBOS for the length of one test, destroyed however the test ends."""

    def __init__(self, name: str) -> None:
        self._name = name

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        _drop_databases()
        config: DBOSConfig = {"name": self._name, "database_url": _dbos_conninfo()}
        DBOS(config=config)
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


async def _drive_partitions() -> None:
    async with _Dbos("agent-core-queue-test"):
        handles = [
            await turn_workflow.enqueue_turn(_request(tenant, session, message))
            for tenant, session, message in _DISPATCH
        ]
        for handle in handles:
            await handle.get_result()


@pytest.mark.phase("F2")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_one_turn_at_a_time_per_session_while_sessions_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-03, both halves of it, neither of them timed.

    Same partition key: never two turns inside it at once, whatever else is in flight.
    Different partition key: genuinely concurrent, including across tenants that reused a
    session id - proved by three of them meeting, not by three spans that happened to
    overlap on an unloaded machine.
    """
    enqueue = getattr(turn_workflow, "enqueue_turn", None)
    assert enqueue is not None, (
        "turn_workflow exposes no enqueue_turn: there is no partitioned queue, so every "
        "turn of a session runs the moment it arrives and two messages interleave over "
        "one conversation. docs/TASKS.md#t-f2-03"
    )

    held = _SessionOccupancy(partitions_expected=_PARTITIONS_EXPECTED)
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(held))

    asyncio.run(_drive_partitions())

    assert len(held.ran) == len(_DISPATCH), (
        f"{len(held.ran)} of {len(_DISPATCH)} enqueued turns ran. A turn that never "
        "leaves the queue is not serialisation, it is a deadlock."
    )

    assert sorted({partition for partition, _ in held.ran}) == [
        "t-1/s-1",
        "t-1/s-2",
        "t-2/s-1",
    ], (
        f"the six turns fell into {sorted({p for p, _ in held.ran})}. Expected one "
        "partition per (tenant, session): a key that collapses two tenants onto one "
        "partition makes them queue behind each other."
    )

    assert held.overlapped == [], (
        f"two turns of partition(s) {sorted(set(held.overlapped))} were inside the "
        "session at the same time. One session runs one turn at a time or the second "
        "turn reads a history the first has not finished writing. docs/TASKS.md#t-f2-03"
    )

    assert not held.timed_out, (
        f"no moment existed at which {_PARTITIONS_EXPECTED} different partitions were "
        f"running together, after waiting {_RENDEZVOUS_DEADLINE_SECONDS:.0f}s for one. "
        "They are different conversations - two sessions of one tenant and a third that "
        "reuses a session id across tenants - and serialising them makes every tenant "
        "wait behind every other tenant's slowest turn, which nothing in a "
        "single-session test would show."
    )


# ---------------------------------------------------------------------------
# t-f2-05 / t-f2-11 / t-f2-10 - the coalescing window, and the drain that empties it
#
# THESE WERE xfail(strict) AND THE MARKERS ARE GONE BECAUSE THE WINDOW LANDED
#     D19's table and docs/FIELD-NOTES.md both read as though coalescing were one more
#     option on the enqueue `t-f2-03` already writes. On dbos 2.31.1 the two mechanisms
#     are MUTUALLY EXCLUSIVE on one queue, and every route around it is closed:
#
#       - `Queue._validate_enqueue` (_queue.py:771) raises "Deduplication is not
#         supported for partitioned queues" when `queue_partition_key` and
#         `deduplication_id` are both set. That is the exact combination D19 prescribes.
#       - The same file refuses the other direction: a partitioned queue REQUIRES a
#         partition key, so the turn cannot simply be enqueued without one.
#       - `Debouncer` refuses it explicitly too - "partitioned queues do not support the
#         deduplication a debounce requires" (_debouncer.py, _reject_conflicting_options).
#       - And a plain `deduplication_id` is released only when the workflow COMPLETES
#         (_sys_db.py, update_workflow_outcome: "As the workflow is complete, remove its
#         deduplication ID"). Only `is_debounced` rows have it cleared when the delay
#         expires. So even a hand-built two-queue relay would make a message arriving
#         MID-TURN join the running turn and vanish, instead of becoming the follow-up
#         turn D19 wants and `t-f2-09` asserts.
#
#     What is left is the shape `Debouncer` itself uses: a separate NON-partitioned
#     window queue carrying a short-lived window workflow, which on expiry enqueues the
#     real turn through `enqueue_turn` onto the partitioned queue. That workflow lives in
#     `adapters/driving/workflow/turn_workflow.py`, not in `routes.py` - so `t-f2-05`
#     named one file and needed an activity that lives in another. Same defect class as
#     `t-f1-04` and the `t-f2-04` / `t-f2-10` split, and it got the same fix: the anchor
#     was split, and `t-f2-11` is the half that landed with these markers removed.
#
#     WHY THE WINDOW WORKFLOW MUST NOT AWAIT THE TURN, which is the whole trick
#         The last bullet above is what makes the two-queue relay work rather than what
#         breaks it. A `deduplication_id` is released when its workflow COMPLETES, so a
#         window workflow that awaited the turn would hold the key for the length of the
#         turn - and D19's mid-turn message would then coalesce onto a window whose turn
#         has already drained the buffer, which is the vanishing this file's third test
#         exists to forbid. The window workflow enqueues and returns, so the key is free
#         again in milliseconds and a mid-turn message opens a NEW window; the partition
#         then serialises the follow-up turn behind the running one, which is exactly
#         D19's "collect" mode.
#
# WHICH MESSAGE TRAVELS IN THE TURN, ANSWERED ONCE FOR BOTH HALVES
#     `duplication_policy="return-existing"` keeps the FIRST message's arguments and
#     discards the later ones, so `request.input` is the first sentence and the buffer
#     holds every sentence that joined after it. `_step_drain_pending` therefore EXTENDS
#     `request.input`; it never replaces it. The two halves have to agree on this or the
#     drain either repeats a sentence or loses one, and no assertion here could say which.
# ---------------------------------------------------------------------------


# Wide enough that three enqueues in a row are unambiguously inside it even on a loaded
# machine. Nothing asserted below depends on the value: the window is a PRECONDITION of
# the scenario ("these messages arrived close together"), never the measurement. Making
# it generous removes the machine's speed from the question entirely.
_WIDE_WINDOW_SECONDS = 60.0

# Used only where a turn must actually be observed to RUN. Short, and awaited causally
# through the workflow handle rather than slept past.
_NARROW_WINDOW_SECONDS = 0.5


def _fresh_tenant() -> str:
    """A tenant nobody else has used.

    The deduplication id is derived from (tenant, session) and is held by a workflow for
    as long as it is delayed. A fixed tenant would make this test attach to a leftover
    turn from a previous run of itself, and it would then pass for the wrong reason.
    """
    return f"t-{uuid4().hex[:12]}"


async def _drive_coalescing(
    starter: routes.TurnStarter, requests: tuple[TurnRequest, ...]
) -> list[str]:
    """Open windows, read back which ones they were, and CANCEL them before leaving.

    The cancellation is not tidiness, it is the difference between a suite that passes
    and one that passes the first time. This helper's callers use a deliberately wide
    window and never let it close, so every window it opens is still DELAYED in the
    shared system database when DBOS is destroyed. The NEXT launch - the next test, or
    the next run of this file minutes later - recovers those rows, fires them when their
    delay finally expires, and runs turns against whichever fake is bound at that moment.
    That is how a later test came to report "4 turns ran for one message".

    WHAT A STARTER HANDS BACK IS THE TURN'S ID, NOT THE WINDOW'S - docs/TASKS.md#t-f0-06
        It used to be one value, and that was the defect the anchor removed: `POST /turns`
        answered with the window workflow's id while every audit row, `GET /turns`, the
        correlation table and `DecideApproval`'s wake-up used another. `TurnHandle.turn_id`
        is now unambiguously the TURN's, so the window has to be derived from it -
        `window_id_for_turn` is the pure, invertible derivation the workflow itself uses.
        Passing the turn id to DBOS as a workflow id instead is what made this file fail
        with `DBOSNonExistentWorkflowError` against a turn that had not been enqueued yet.
    """
    from dbos import DBOS

    async with _Dbos("agent-core-coalescing-test"):
        ids = [str((await starter(request)).turn_id) for request in requests]
        for turn_id in dict.fromkeys(ids):
            await DBOS.cancel_workflow_async(
                turn_workflow.window_id_for_turn(TurnId(turn_id))
            )
        return ids


async def _await_turn_behind(turn_id: str) -> None:
    """Wait for the turn `turn_id` names, causally and never by clock.

    Two hops, because the window workflow deliberately does NOT await the turn (see the
    block comment above): the window has to close and enqueue first, and only the turn's
    own handle completes when the turn has actually run. The window is reached through
    `window_id_for_turn` for the reason `_drive_coalescing` gives - the two ids are two
    workflows, and the turn's does not address a workflow until the window enqueues it.
    """
    from dbos import DBOS, WorkflowHandleAsync

    window_id = turn_workflow.window_id_for_turn(TurnId(turn_id))
    window: WorkflowHandleAsync[str] = await DBOS.retrieve_workflow_async(window_id)
    enqueued = str(await window.get_result())
    assert enqueued == turn_id, (
        f"the window at {window_id} enqueued turn {enqueued}, not {turn_id}. The window id "
        "and the turn id are derived from each other (docs/TASKS.md#t-f0-06); if they can "
        "disagree then the id a caller was answered with is filed under nothing."
    )
    await (await DBOS.retrieve_workflow_async(enqueued)).get_result()


async def _drive_one_coalesced_turn(
    starter: routes.TurnStarter, request: TurnRequest
) -> None:
    async with _Dbos("agent-core-coalescing-test"):
        handle = await starter(request)
        await _await_turn_behind(str(handle.turn_id))


def _coalescing_starter(**kwargs: Any) -> routes.TurnStarter:
    factory = getattr(routes, "coalescing_turn_starter", None)
    assert factory is not None, (
        "routes exposes no coalescing_turn_starter: every message a user sends starts "
        "its own turn, so three sentences split across three sends are answered three "
        "times and paid for three times. D19 step 2-3, docs/TASKS.md#t-f2-05"
    )
    return cast("routes.TurnStarter", factory(**kwargs))


@pytest.mark.phase("F2")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_three_messages_on_one_session_coalesce_into_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-05, and its other half: coalescing that is not too eager.

    Three messages on one session inside the window are ONE enqueued turn - the same
    durable id comes back three times, so there is one workflow to run and one model
    call to pay for. Two sessions are never merged, however close together they arrive:
    the deduplication id carries the tenant and the session, so a shared window is not a
    shared conversation.
    """
    started = _RecordingStartTurn()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(started))
    buffer = _RecordingBuffer()
    starter = _coalescing_starter(buffer=buffer, window_seconds=_WIDE_WINDOW_SECONDS)

    tenant = _fresh_tenant()
    # The fifth request reuses session id "s-1" under a DIFFERENT tenant. `SessionId` is
    # unique inside a tenant only, so a deduplication id keyed on the session alone would
    # merge two strangers' conversations into one turn - a disclosure, not a latency win,
    # and one no single-tenant test can see.
    other_tenant = _fresh_tenant()
    requests = (
        _request(tenant, "s-1", 1),
        _request(tenant, "s-1", 2),
        _request(tenant, "s-1", 3),
        _request(tenant, "s-2", 1),
        _request(other_tenant, "s-1", 1),
    )

    ids = asyncio.run(_drive_coalescing(starter, requests))

    first, second, third, other_session, other_tenant_same_session = ids

    assert first == second == third, (
        f"three messages on one session inside the window produced {len(set(ids[:3]))} "
        f"turns ({sorted(set(ids[:3]))}). D19: the second and third must join the turn "
        "already waiting, or one thought split across three sends is answered three "
        "times and billed three times. docs/TASKS.md#t-f2-05"
    )

    assert other_session != first, (
        "a message on a DIFFERENT session joined the first session's turn. The "
        "deduplication id must carry (tenant, session): coalescing across conversations "
        "answers one user with another user's context, which is a disclosure, not a "
        "latency optimisation."
    )

    assert other_tenant_same_session not in {first, other_session}, (
        "a message from ANOTHER TENANT that happens to reuse session id 's-1' joined "
        "this tenant's turn. `SessionId` is unique inside a tenant only, so the "
        "deduplication id must carry the tenant as well - otherwise one customer's "
        "sentence is answered inside another customer's conversation."
    )

    # Deliberately not "the second and third": which of the three messages ends up
    # inside the surviving turn's request is the implementation's business - a
    # `return-existing` collision keeps the FIRST message's arguments, a debounce bounce
    # keeps the LAST. What is not negotiable either way is that the other two are kept
    # somewhere, distinct and unrepeated. A message that joined a waiting turn is not in
    # that turn's request, so a coalescing enqueue that does not buffer it has silently
    # deleted what the user said. D19 step 1, drained back by docs/TASKS.md#t-f2-10.
    coalesced = sorted(message.text for _, message in buffer.appended)
    assert len(coalesced) == 2 and len(set(coalesced)) == 2, (
        f"the buffer holds {coalesced}. Three messages arrived and one of them travels "
        "in the enqueued turn; the other two must be buffered exactly once each - "
        "buffering none loses two thirds of what the user said, and buffering all three "
        "makes the drain answer one sentence twice."
    )
    assert set(coalesced) < {".", "..", "..."}, (
        f"the buffer holds {coalesced}, which is not a subset of what was sent."
    )

    assert {session.session_id for session, _ in buffer.appended} == {SessionId("s-1")}, (
        "a message was buffered against a session it did not belong to; the drain would "
        "then hand one conversation's sentence to another."
    )


@pytest.mark.phase("F2")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_coalesced_turn_is_still_dequeued_once_its_window_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delay must postpone the turn, not strand it.

    `delay_seconds`, `deduplication_id` and `queue_partition_key` are three options set
    on one enqueue, and a combination that no executor ever dequeues is the worst
    outcome available here: nothing raises, nothing logs, and the user's message is
    simply never answered. Awaited through the handle, so the proof is the turn running
    and not a clock.
    """
    started = _RecordingStartTurn()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(started))
    starter = _coalescing_starter(
        buffer=_RecordingBuffer(), window_seconds=_NARROW_WINDOW_SECONDS
    )

    request = _request(_fresh_tenant(), "s-1", 1)

    asyncio.run(_drive_one_coalesced_turn(starter, request))

    assert len(started.started) == 1, (
        f"{len(started.started)} turns ran for one message. A delayed, deduplicated, "
        "partitioned enqueue must run exactly once when its window closes."
    )
    assert started.started[0].input.text == request.input.text, (
        "the turn ran with different input than the message that started it."
    )


# ---------------------------------------------------------------------------
# t-f2-10 - the drain, and the mid-turn follow-up the window must not swallow
# ---------------------------------------------------------------------------

# The window for the "three messages become one turn" run below. Three consecutive
# enqueues against a local Postgres take tens of milliseconds, so this is roughly two
# orders of magnitude of headroom - and it is a PRECONDITION of the scenario ("these
# messages arrived close together"), never the measurement. If the machine stalls for
# five seconds between two local INSERTs the first assertion says exactly that, rather
# than reporting a coalescing bug that is not there.
_COALESCED_RUN_WINDOW_SECONDS = 5.0


class _BlockingStartTurn:
    """Stands in for `StartTurn`, and holds the FIRST turn inside the session slot.

    The mid-turn test needs a moment that provably IS mid-turn. Sleeping for one and
    hoping is the mistake this file's header is about, so the turn signals that it is
    inside and then waits to be let out: the second message is sent between those two
    events, which makes "mid-turn" causal instead of timed.

    Cross-thread primitives, not asyncio ones: DBOS runs a dequeued workflow on its own
    executor, so the event the test sets is not on the loop the workflow is running on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started: list[TurnRequest] = []
        self.inside = threading.Event()
        self.release = threading.Event()
        self.timed_out = False

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        with self._lock:
            self.started.append(request)
            hold = len(self.started) == 1
        if hold:
            self.inside.set()
            deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
            while not self.release.is_set():
                if time.monotonic() >= deadline:
                    self.timed_out = True
                    break
                await asyncio.sleep(_POLL_SECONDS)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


async def _wait_for(flag: threading.Event, what: str) -> None:
    """Block until `flag` is set. The deadline turns a hang into a sentence, nothing more."""
    deadline = time.monotonic() + _RENDEZVOUS_DEADLINE_SECONDS
    while not flag.is_set():
        assert time.monotonic() < deadline, (
            f"waited {_RENDEZVOUS_DEADLINE_SECONDS:.0f}s and {what} never happened."
        )
        await asyncio.sleep(_POLL_SECONDS)


async def _drive_three_coalesced_messages(
    starter: routes.TurnStarter, requests: tuple[TurnRequest, ...]
) -> list[str]:
    async with _Dbos("agent-core-coalescing-test"):
        ids = [str((await starter(request)).turn_id) for request in requests]
        await _await_turn_behind(ids[0])
        return ids


async def _drive_a_mid_turn_message(
    starter: routes.TurnStarter,
    held: _BlockingStartTurn,
    first: TurnRequest,
    second: TurnRequest,
) -> tuple[str, str]:
    async with _Dbos("agent-core-coalescing-test"):
        opened = str((await starter(first)).turn_id)

        # The window has to have CLOSED and the turn have started before the second
        # message is sent, or this would be testing coalescing again.
        await _wait_for(held.inside, "the first turn reached _step_start")

        follow_up = str((await starter(second)).turn_id)

        held.release.set()
        await _await_turn_behind(opened)
        await _await_turn_behind(follow_up)
        return opened, follow_up


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_three_coalesced_messages_run_one_turn_carrying_every_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-05 + #t-f2-11 + #t-f2-10, which only mean anything together.

    One turn and one model call for three sentences is the D19 saving. The saving is a
    LOSS unless the two sentences that did not travel in the turn's own request come
    back - so this asserts the count and the content in the same run:
    `_step_drain_pending` must extend `request.input` with the buffered sentences, in
    arrival order, each exactly once.
    """
    held = _BlockingStartTurn()
    buffer = _RecordingBuffer()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(held, buffer))
    starter = _coalescing_starter(
        buffer=buffer, window_seconds=_COALESCED_RUN_WINDOW_SECONDS
    )

    tenant = _fresh_tenant()
    requests = (
        _request(tenant, "s-1", 1),
        _request(tenant, "s-1", 2),
        _request(tenant, "s-1", 3),
    )

    ids = asyncio.run(_drive_three_coalesced_messages(starter, requests))

    assert len(set(ids)) == 1, (
        f"three messages sent inside a {_COALESCED_RUN_WINDOW_SECONDS:.0f}s window "
        f"opened {len(set(ids))} windows ({sorted(set(ids))}). Either coalescing is not "
        "happening, or this machine took longer than the window to run three local "
        "enqueues - the second is a stalled machine, not a defect in the code."
    )

    assert len(held.started) == 1, (
        f"{len(held.started)} turns ran for three coalesced messages. One turn is one "
        "model call; three is three, for one answer. D19"
    )

    ran = held.started[0].input.text
    assert ran.split("\n") == [".", "..", "..."], (
        f"the turn ran on {ran!r}. The first message travels in the enqueued request "
        "(return-existing keeps the FIRST arguments) and the other two are drained out "
        "of the buffer behind it, in arrival order and once each. A drain that REPLACED "
        "the request would lose the first sentence; one that repeated it would answer "
        "the same sentence twice. docs/TASKS.md#t-f2-10"
    )

    assert buffer.appended == [], (
        f"{len(buffer.appended)} messages are still buffered after the turn ran. A "
        "drain that does not empty the buffer hands the same sentence to the next turn "
        "as well."
    )


@pytest.mark.phase("F2")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_message_arriving_mid_turn_becomes_a_follow_up_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f2-09's property, proved here because t-f2-11 is what decides it.

    Coalescing is bounded by the window, not by the turn. A message that arrives while
    the turn is already running has missed the window, and it must open a NEW one: the
    partitioned queue then serialises the follow-up turn behind the running one, which
    is D19's "collect" mode.

    The failure this forbids is silent. If the window workflow held its deduplication id
    for the length of the turn - which is exactly what awaiting the turn inside it would
    do - the mid-turn message would coalesce onto a window whose turn has already
    drained the buffer. Nothing raises. The sentence simply sits in a table and the user
    is never answered.
    """
    held = _BlockingStartTurn()
    buffer = _RecordingBuffer()
    monkeypatch.setattr(turn_workflow, "_dependencies", _wired(held, buffer))
    starter = _coalescing_starter(buffer=buffer, window_seconds=_NARROW_WINDOW_SECONDS)

    tenant = _fresh_tenant()
    first = _request(tenant, "s-1", 1)
    second = _request(tenant, "s-1", 2)

    opened, follow_up = asyncio.run(
        _drive_a_mid_turn_message(starter, held, first, second)
    )

    assert not held.timed_out, (
        "the first turn was never released; it waited out the deadline instead, so "
        "nothing below is about the property this test is named for."
    )

    assert follow_up != opened, (
        "a message that arrived while the turn was RUNNING was handed the running "
        "turn's window back. Its window had already closed, so the sentence joins a "
        "turn that has already read the buffer and is never answered - the exact "
        "vanishing D19 calls out and docs/TASKS.md#t-f2-09 asserts."
    )

    assert [request.input.text for request in held.started] == [".", ".."], (
        f"the turns ran on {[r.input.text for r in held.started]}. Expected two turns, "
        "the first carrying the first message and the follow-up carrying the mid-turn "
        "one - one turn means the second message vanished, and a first turn carrying "
        "both means the window did not close when the turn started."
    )
