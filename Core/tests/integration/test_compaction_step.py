"""The compaction step inside the turn workflow - one pass per session, never a retry.

Phase:   F5 - Context compaction
Tasks:   docs/TASKS.md#t-f5-08 (R5 in turn_workflow.py's own docstring)
Covers:  adapters/driving/workflow/turn_workflow.py - `_step_compact` and the body's call

SILENT-BUG AREA (CLAUDE.md). Everything wrong here shows up on the BILL, not in a test,
so every assertion below COUNTS summariser calls rather than inspecting a return value.
A workflow that compacted twice still returns a perfectly well-formed `TurnOutcome`.

THE TWO PROPERTIES, AND WHY EACH NEEDS THE SHAPE IT HAS

  1. AT MOST ONE PASS PER SESSION PER TURN, AND THE LOCK IS DBOS'S, NOT OURS.
     `agent-core-turns` is partitioned with `partition_concurrency=1`, so two messages of
     one conversation cannot run at once. That is the durable lock `ports/context_engine.py`
     says we get for free by making compaction a step - "Hermes runs compression over a
     deep copy, publishes only on an admitted commit fence, holds a durable lock so there
     is one pass per session ... several hundred lines of machinery".

     It is only observable through a REAL queue. Two turns enqueued together for one
     session: serialised, the first pass frees tokens and the second turn's trigger no
     longer fires, so the summariser is paid for once. Overlapping, both passes would read
     the pre-compaction size, both would reach the summariser, and both would rewrite the
     prompt prefix - which re-bills the whole prompt at full price on the next request
     (domain/compaction.py's opening warning). So the fake engine below records its own
     concurrency: a test that only counted calls could not tell "serialised" from "the
     second one happened to be cheap".

     A second session runs in the same block as the control. Without it, "compress was
     called once" is equally well explained by a fake that is one-shot globally, or by a
     queue that stopped dequeuing after the first turn.

  2. A PASS THAT FREED NOTHING IS NEVER RETRIED.
     `application/compact_context.py` already refuses: it returns the fruitless result
     without a checkpoint and without asking again. The failure this file guards is the
     step turning that refusal back into a retry by being RE-ENTERED - a loop around the
     call, or a second call site. That is a property of the workflow BODY, so it is
     asserted over the source as well as behaviourally: an infinite loop does not fail an
     assertion, it hangs the suite, and the AST says which line is responsible.

WHY THE TURNS GO THROUGH `enqueue_turn` AND NOT THROUGH THE BODY
    test_delivery.py's barrier assertion spells it out: past the queue there is no
    partition, and the partition is the entire mechanism under test here. Driving the body
    directly would make property 1 unfalsifiable - there would be nothing to serialise.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.application.compact_context import CompactContext
from agent_core.domain.compaction import (
    CompactionCheckpoint,
    CompactionPolicy,
    CompactionResult,
    ContextState,
    Rung,
)
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Usage,
    UserInput,
)
from tests.fakes.ports import FakeConversationStore

_PROFILE_ID = "delivery_optimizer"

PROFILE = AgentProfile(id=_PROFILE_ID, persona="You optimise deliveries.", model="m")

_WORKFLOW_SOURCE = Path(str(turn_workflow.__file__))

# The window the composition root would state for this deployment. A real number rather
# than zero on purpose: `ports/context_engine.py` resolves an unknown window TOWARDS
# compaction, so a seat that forgot to carry the window would make the trigger fire on
# every turn - which is the expensive failure this whole phase is about.
_CONTEXT_WINDOW = 100_000

# Over the default `trigger_fraction` of 0.75, and comfortably under it after a pass.
_TOKENS_BEFORE = 90_000
_TOKENS_AFTER = 30_000

# How long one compaction pass is held open. Long enough that two overlapping passes would
# be caught in the act by `peak_in_flight`, short enough to be invisible in the run time.
_PASS_SECONDS = 0.4

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# Its own DBOS system database, for the reason test_delivery.py gives: a failed run here
# can be destroyed without touching state another file owns.
_DBOS_DATABASE = "agent_core_compaction_test"

_RUN_DEADLINE_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingChannel:
    """A `Channel` by shape alone (t-f3-13). Delivery has to succeed or the body never
    reaches the compaction call at all."""

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _FinishesImmediately:
    """Stands in for `StartTurn`: one finished turn carrying the usage the step reads.

    The usage is the honest input to the trigger - `input_tokens` is the whole prompt the
    provider billed for and `output_tokens` is what was appended to it, so their sum is
    the size of the conversation as it now stands. That is the same arithmetic
    `LadderContextEngine.update_from_response` settled under docs/TASKS.md#t-f5-11, and
    for the same reason: a running total would cross the trigger once and stay across it.
    """

    def __init__(self, result: TurnResult) -> None:
        self._result = result
        self.calls = 0

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.calls += 1
        return TurnOutcome(turn_id=turn_id, result=self._result)


class _LedgerEngine:
    """A `ContextEngine` that models the one thing the durable lock has to prevent.

    It keeps a per-session token count. `should_compress` fires while that count is over
    the profile's trigger fraction of the window it was HANDED - so a step that forgot to
    carry the window makes this fire forever, which is exactly the production failure. A
    pass drops the count to `after`, so a later turn on the same session sees a
    conversation that no longer needs compacting.

    `peak_in_flight` is what separates "serialised" from "lucky". Counting calls alone
    cannot: two overlapping passes that both freed tokens leave the same count behind as
    one pass did.

    It is counted PER SESSION, and that is not bookkeeping. Two different conversations
    compacting at the same time is the wanted behaviour - `turn_queue` bounds a partition
    and deliberately leaves global concurrency unbounded, so a thousand sessions proceed
    at once while each stays single-file. A process-wide counter would call that a
    violation and would be asserting the opposite of the design.
    """

    def __init__(self, *, before: int, after: int) -> None:
        self._before = before
        self._after = after
        self._tokens: dict[SessionRef, int] = {}
        self.compress_calls: list[SessionRef] = []
        self.states: list[ContextState] = []
        self.in_flight: dict[SessionRef, int] = {}
        self.peak_in_flight: dict[SessionRef, int] = {}

    def _size(self, session: SessionRef) -> int:
        return self._tokens.setdefault(session, self._before)

    def on_session_start(self, session: SessionRef) -> None:
        return None

    def update_from_response(self, session: SessionRef, usage: object) -> None:
        return None

    def on_session_end(self, session: SessionRef) -> None:
        return None

    def should_compress(self, state: ContextState, policy: CompactionPolicy) -> bool:
        self.states.append(state)
        return self._size(state.session) > int(policy.trigger_fraction * state.context_window)

    async def compress(
        self, session: SessionRef, history: object, policy: CompactionPolicy
    ) -> CompactionResult:
        before = self._size(session)
        self.compress_calls.append(session)
        self.in_flight[session] = self.in_flight.get(session, 0) + 1
        self.peak_in_flight[session] = max(
            self.peak_in_flight.get(session, 0), self.in_flight[session]
        )
        try:
            await asyncio.sleep(_PASS_SECONDS)
        finally:
            self.in_flight[session] -= 1
        self._tokens[session] = self._after
        return CompactionResult(
            compacted_history=["compacted"],
            checkpoint=CompactionCheckpoint(
                checkpoint_id=f"ck-{len(self.compress_calls)}",
                session=session,
                summary="The courier was rerouted twice.",
                covers_through_message=20,
                tokens_before=before,
                tokens_after=self._after,
                rungs_applied=(Rung.L3_SUMMARISE_MIDDLE,),
            ),
            rungs_applied=(Rung.L3_SUMMARISE_MIDDLE,),
            tokens_before=before,
            tokens_after=self._after,
        )


def _request(session: str) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId(session), tenant_id=TenantId("t-f5")),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="telegram",
            tenant_id=TenantId("t-f5"),
            roles=frozenset({"operator"}),
        ),
        profile_id=_PROFILE_ID,
        input=UserInput(text="where is my parcel"),
    )


# `context_window_used` stays None deliberately: adapters/driven/agent_pydantic/runner.py
# records that no provider LiteLLM fronts reports it, so None is the COMMON case and the
# port's mandatory fallback is the path that actually runs in production.
_USAGE = Usage(input_tokens=88_000, output_tokens=2_000, context_window_used=None)


def _finished_result() -> TurnResult:
    return TurnResult(text="it is out for delivery", usage=_USAGE)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_turn: _FinishesImmediately,
    channels: ChannelRegistry,
    engine: _LedgerEngine,
    store: FakeConversationStore,
) -> None:
    """Wire the workflow's collaborators for one test, and unwire them afterwards.

    `monkeypatch` rather than `bind_dependencies`, for the reason test_delivery.py gives:
    the binding is process-wide by design - a DBOS workflow is a module-level object - and
    that is exactly what makes it leak into the next test in the session.

    THE PRECONDITION IS AN ASSERTION ON PURPOSE. `CompactionSeat` is part of this adapter's
    PUBLIC surface, because `composition.py` is the only thing that knows which model a
    deployment runs and therefore how big its context window is. A module that does not
    export it cannot be wired for compaction at all, and every count below would then be
    measuring a turn that never compacts.
    """
    assert "CompactionSeat" in turn_workflow.__all__, (
        "turn_workflow exports no CompactionSeat, so there is no way for composition.py to "
        "hand the workflow a CompactContext, the loaded profiles and the deployment's "
        "context window - and R5's step has nothing to wrap. docs/TASKS.md#t-f5-08"
    )
    seat = turn_workflow.CompactionSeat(
        compact=CompactContext(context=cast("Any", engine), store=cast("Any", store)),
        profiles={_PROFILE_ID: PROFILE},
        context_window=_CONTEXT_WINDOW,
    )
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", start_turn),
            channels=channels,
            compaction=seat,
        ),
    )


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _dbos_conninfo(database: str = _DBOS_DATABASE) -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


def _drop_databases() -> None:
    """Destroy this file's DBOS state before launching.

    test_delivery.py's barrier asserts that every module which launches DBOS does this,
    and reproduces the reason: one workflow left PENDING by a killed run occupies its
    session's only partition slot forever, so every later turn for that session is
    enqueued and never dequeued - with nothing raising. On a file whose whole subject is
    per-session serialisation that would read as the property under test passing.
    """
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DBOS_DATABASE}_dbos_sys", _DBOS_DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _Dbos:
    """A launched DBOS for the length of one block, destroyed however the block ends."""

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


# ---------------------------------------------------------------------------
# Property 1 - one pass per session, and the lock is the queue's
# ---------------------------------------------------------------------------


@pytest.mark.phase("F5")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_two_turns_racing_one_session_pay_the_summariser_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f5-08 - "DBOS's durable lock gives one pass per session".

    Two turns for ONE session are enqueued together, plus one turn for a second session as
    the control. Three facts come out of the single run, and separately each has an
    innocent explanation:

      1. the shared session's summariser was paid for exactly ONCE, because the partition
         serialised the two turns and the second one's trigger saw an already-compacted
         conversation;
      2. no two passes were ever in flight at the same time, which is what makes (1) the
         durable lock rather than a coincidence of timing;
      3. the OTHER session compacted too, which is what makes (1) a per-session lock
         rather than a fake that is one-shot, or a queue that stopped dequeuing.
    """
    engine = _LedgerEngine(before=_TOKENS_BEFORE, after=_TOKENS_AFTER)
    store = FakeConversationStore()
    channel = _RecordingChannel()
    start_turn = _FinishesImmediately(_finished_result())
    _bind(
        monkeypatch,
        start_turn=start_turn,
        channels=ChannelRegistry((("telegram", channel),)),
        engine=engine,
        store=store,
    )

    shared = _request("s-shared")
    other = _request("s-other")

    async def drive() -> None:
        async with _Dbos("agent-core-compaction-test"):
            handles = [
                await turn_workflow.enqueue_turn(shared),
                await turn_workflow.enqueue_turn(shared),
                await turn_workflow.enqueue_turn(other),
            ]
            await asyncio.wait_for(
                asyncio.gather(*(handle.get_result() for handle in handles)),
                timeout=_RUN_DEADLINE_SECONDS,
            )

    asyncio.run(drive())

    assert start_turn.calls == 3, (
        f"{start_turn.calls} turns ran, not 3. Nothing below is measuring compaction."
    )
    shared_passes = [s for s in engine.compress_calls if s == shared.session]
    assert len(shared_passes) == 1, (
        f"the summariser was paid {len(shared_passes)} times for one session in one pass "
        "of the queue. Every extra pass also rewrites the prompt prefix, so the provider's "
        "cache is invalidated and the NEXT request re-bills the whole prompt at full price "
        "- the most expensive failure this system has, and it fails no other test. "
        "docs/TASKS.md#t-f5-08, domain/compaction.py's opening warning."
    )
    overlapped = {s: peak for s, peak in engine.peak_in_flight.items() if peak > 1}
    assert overlapped == {}, (
        f"{overlapped} had more than one compaction pass in flight at once. The lock that "
        "makes one pass per session is the queue's partition_concurrency=1, and a step "
        "that runs outside it has no lock at all - two passes then read the same "
        "pre-compaction history and both pay. Note that passes on DIFFERENT sessions "
        "overlapping is correct and wanted: turn_queue bounds a partition and leaves "
        "global concurrency unbounded on purpose."
    )
    assert [s for s in engine.compress_calls if s == other.session], (
        "the second session never compacted, so the assertion above is equally well "
        "explained by a fake that is one-shot globally or by a queue that stopped "
        "dequeuing. The lock is per SESSION."
    )
    assert len(channel.sent) == 3, (
        f"{len(channel.sent)} answers were delivered. Compaction runs AFTER delivery so a "
        "pass never delays the reply a human is waiting for; a turn that lost its answer "
        "to the compaction step has the ordering backwards."
    )


@pytest.mark.phase("F5")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_step_hands_the_trigger_the_finished_turn_s_own_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `ContextState` the step builds, pinned - it is the whole input to the trigger.

    `window_used` is the provider's fraction and is OFTEN None (domain/turn.py); the
    fallback divides an estimate by the window, so both numbers have to be real. The
    estimate is `input_tokens + output_tokens` - the whole prompt this response was billed
    for plus what was appended to it, which is the size of the conversation as it now
    stands. docs/TASKS.md#t-f5-11 settled that arithmetic and named the alternative: a
    running total crosses the trigger once and stays across it, so the ladder runs on
    every turn and re-bills the whole prompt every turn.
    """
    engine = _LedgerEngine(before=_TOKENS_BEFORE, after=_TOKENS_AFTER)
    start_turn = _FinishesImmediately(_finished_result())
    _bind(
        monkeypatch,
        start_turn=start_turn,
        channels=ChannelRegistry((("telegram", _RecordingChannel()),)),
        engine=engine,
        store=FakeConversationStore(),
    )

    request = _request("s-usage")

    async def drive() -> None:
        async with _Dbos("agent-core-compaction-test"):
            handle = await turn_workflow.enqueue_turn(request)
            await asyncio.wait_for(handle.get_result(), timeout=_RUN_DEADLINE_SECONDS)

    asyncio.run(drive())

    assert len(engine.states) == 1, (
        f"the trigger was consulted {len(engine.states)} times for one turn. One turn is "
        "one pass; asking again inside the same turn is the loop t-f5-03 refuses."
    )
    state = engine.states[0]
    usage = _USAGE
    assert state.session == request.session, (
        f"the trigger was asked about {state.session!r} while the turn belongs to "
        f"{request.session!r}. A pass filed against another conversation compacts "
        "somebody else's history."
    )
    assert state.window_used == usage.context_window_used, (
        f"window_used was {state.window_used!r} and the provider reported "
        f"{usage.context_window_used!r}. Inventing a fraction here would be a second "
        "trigger, and ports/context_engine.py owns the None fallback precisely so there "
        "is only one."
    )
    assert state.estimated_tokens == usage.input_tokens + usage.output_tokens, (
        f"estimated_tokens was {state.estimated_tokens}, not the "
        f"{usage.input_tokens + usage.output_tokens} this turn was billed for. The "
        "fallback divides this by the window, so a wrong number here is a trigger that "
        "fires on every turn or never. docs/TASKS.md#t-f5-11"
    )
    assert state.context_window == _CONTEXT_WINDOW, (
        f"context_window was {state.context_window}, not the {_CONTEXT_WINDOW} the seat "
        "carries. ports/context_engine.py resolves an unknown window TOWARDS compaction, "
        "so a step that drops it compacts every single turn and nothing ever says so."
    )


# ---------------------------------------------------------------------------
# Property 2 - a pass that freed nothing is never retried
# ---------------------------------------------------------------------------


@pytest.mark.phase("F5")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_pass_that_freed_nothing_is_not_retried_and_writes_no_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step must not turn t-f5-03's refusal back into a retry by being re-entered.

    The engine here frees nothing and leaves the session exactly as big as it was, so its
    trigger keeps firing. A step that looped "until the history fits" would call the
    summariser forever - an infinite loop that ALSO rewrites the prompt prefix on every
    pass, which is the most expensive failure mode this system has.

    The checkpoint half is the other symptom of the same defect. `CompactContext` refuses
    to persist a summary that bought nothing, because a chain of checkpoints covering
    messages the previous one already covered stops being the record of what the agent
    used to know - and that chain is the only such record there is.
    """
    engine = _LedgerEngine(before=_TOKENS_BEFORE, after=_TOKENS_BEFORE)
    store = FakeConversationStore()
    start_turn = _FinishesImmediately(_finished_result())
    _bind(
        monkeypatch,
        start_turn=start_turn,
        channels=ChannelRegistry((("telegram", _RecordingChannel()),)),
        engine=engine,
        store=store,
    )

    request = _request("s-stuck")

    async def drive() -> str:
        async with _Dbos("agent-core-compaction-test"):
            handle = await turn_workflow.enqueue_turn(request)
            await asyncio.wait_for(handle.get_result(), timeout=_RUN_DEADLINE_SECONDS)
            return (await handle.get_status()).status

    status = asyncio.run(drive())

    assert len(engine.compress_calls) == 1, (
        f"the summariser was called {len(engine.compress_calls)} times on a ladder that "
        "freed nothing. A fruitless pass is returned, never repeated: repeating it is an "
        "infinite loop that invalidates the provider's prompt cache every time round. "
        "application/compact_context.py step 3, docs/TASKS.md#t-f5-08."
    )
    assert store.checkpoints == [], (
        f"a checkpoint was persisted for a pass that bought nothing ({store.checkpoints!r}). "
        "The supersedes chain is the only record of what the agent used to know, and a "
        "summary covering already-covered messages makes it unreadable."
    )
    assert status == "SUCCESS", (
        f"the turn ended {status!r}. An exhausted ladder is not a failed turn - the answer "
        "was produced and delivered, and compaction is housekeeping that runs after it."
    )


# ---------------------------------------------------------------------------
# The same property, over the source: an infinite loop hangs, it does not fail
# ---------------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _named(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def _calls_to(node: ast.AST, leaf: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _dotted(child.func).rsplit(".", 1)[-1] == leaf
    ]


@pytest.mark.phase("F5")
@pytest.mark.silent
def test_the_body_compacts_once_and_never_inside_a_loop() -> None:
    """R5, asserted over the source, because the failure it guards HANGS rather than fails.

    A behavioural test cannot see this one: a step re-entered until the history fits never
    returns, so the suite stops rather than going red, and the summariser bill is spent
    before anybody reads the traceback. The position of the call is the property - one
    call site, and none of them inside a `for` or a `while`.

    The step decorator is asserted beside it. Compaction reaches a summariser MODEL
    (CLAUDE.md non-negotiable #3 - never inside a database transaction, and a summariser
    call is not a fast one), and a plain call in the workflow body would re-run on every
    replay: `run_turn_workflow`'s R2 says every side effect is a step, and this one is the
    side effect that costs money.
    """
    tree = ast.parse(_WORKFLOW_SOURCE.read_text(encoding="utf-8"))

    step = _named(tree, "_step_compact")
    assert step is not None, (
        f"{_WORKFLOW_SOURCE.name} declares no async _step_compact. R5 in its own docstring "
        "says compaction is a step wrapping CompactContext, and the seam comment at the end "
        "of run_turn_workflow marks the line it belongs on. docs/TASKS.md#t-f5-08"
    )
    assert any(_dotted(item) == "DBOS.step" for item in step.decorator_list), (
        "_step_compact is not decorated with @DBOS.step(). Outside a step the summariser "
        "call re-runs on every replay and is paid for again each time, and nothing records "
        "that the pass already happened. run_turn_workflow's R2."
    )

    body = _named(tree, "run_turn_workflow")
    assert body is not None, f"{_WORKFLOW_SOURCE.name} declares no run_turn_workflow."

    call_sites = _calls_to(body, "_step_compact")
    assert len(call_sites) == 1, (
        f"run_turn_workflow calls _step_compact {len(call_sites)} times. One turn is one "
        "pass: a second call site compacts twice, and every pass rewrites the prompt prefix "
        "so the next request re-bills the whole prompt at full price. "
        "docs/TASKS.md#t-f5-08"
    )

    looped = [
        loop
        for loop in ast.walk(body)
        if isinstance(loop, ast.For | ast.AsyncFor | ast.While) and _calls_to(loop, "_step_compact")
    ]
    assert looped == [], (
        f"_step_compact is called inside a loop (line {[loop.lineno for loop in looped]}). "
        "application/compact_context.py refuses to retry a pass that freed nothing, and a "
        "loop around the step turns that refusal straight back into the infinite loop it "
        "exists to prevent - one that re-bills the whole prompt every time round."
    )
