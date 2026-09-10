"""Driving adapter: the DBOS workflow that orchestrates one turn.

Phase:   F2 (durability) / F3 (the human wait) / F5 (compaction step)
Tasks:   docs/TASKS.md#t-f2-01, docs/TASKS.md#t-f2-03
Status:  IMPLEMENTED (t-f2-01, t-f2-03) - the body, its steps, the per-session queue, and
         the seams later anchors fill

THIS FILE IS THE REASON FOR NON-NEGOTIABLE #1
    A DBOS workflow is a DRIVING ADAPTER, exactly like a FastAPI router. It orchestrates
    steps; each step resolves a use case and calls it.

    NEVER put @DBOS.workflow() on a use case. That drags infrastructure into the
    application layer: tests then need Postgres, and the use case is married to an
    orchestration engine. docs/DECISIONS.md#d6.

WHAT DBOS BUYS, STATED SO NOBODY REIMPLEMENTS IT
    - Crash recovery: die after step 1, restart, step 1 is NOT re-run.
    - Durable waits: DBOS.recv() survives restarts, redeploys and three days of silence.
    - Per-session serialisation and isolation for the compaction pass.

    Hermes hand-built the equivalent - snapshot, commit fence, durable lock - across
    several hundred lines. Here it is decorators. Do not rebuild it.

THE ASYNC VARIANTS ARE THE ONES THIS FILE CALLS
    The system is async end to end (D13) and DBOS ships an async counterpart for every
    primitive the workflow needs - `recv_async`, `send_async`, `run_step_async`,
    `start_workflow_async`. Calling the sync form from async code works on one machine and
    deadlocks on another. Verified against dbos 2.31.1; see docs/FIELD-NOTES.md.

WHAT THIS FILE DELIBERATELY DOES NOT DO YET, AND WHO OWNS IT
    Each seam below is a comment at the exact line the step belongs on, so the next anchor
    edits one place rather than re-deriving where its step goes. This file is the widest
    serial spine in F2 (docs/WAVES.md) precisely because five anchors write it in turn.

      - `_step_drain_pending`, the body's FIRST step   docs/TASKS.md#t-f2-10
      - the coalescing enqueue (it lives in routes.py)  docs/TASKS.md#t-f2-05
      - `_step_deliver`, outbound delivery              docs/TASKS.md#t-f3-07
      - `_step_compact`, R5's step                      docs/TASKS.md#t-f5-08

    `_step_publish` and `_step_resume` exist here with their F2 SHAPE - the body has to
    decide when to call them - and refuse loudly, because both need collaborators F3 has
    not built (`HumanGateway`, docs/TASKS.md#t-f3-04; `ResumeTurn`, docs/TASKS.md#t-f3-02).
    A silent fallback for either would resume a turn without asking anybody.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from dbos import DBOS, Queue, SetEnqueueOptions, WorkflowHandleAsync

from agent_core.application.start_turn import StartTurn
from agent_core.domain.turn import (
    PendingRequest,
    SessionRef,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)

__all__ = [
    "MAX_HUMAN_ROUNDS",
    "RESUME_TOPIC",
    "THREE_DAYS",
    "TURN_QUEUE_NAME",
    "TurnWorkflowDependencies",
    "TurnWorkflowNotWiredError",
    "bind_dependencies",
    "enqueue_turn",
    "run_turn_workflow",
    "session_partition_key",
    "turn_queue",
]

# Three days. Long enough for a weekend plus a public holiday, short enough that an
# abandoned turn does not sit in the database forever.
#
# It is passed EXPLICITLY to every recv: `DBOS.recv` defaults to 60 seconds (verified on
# dbos 2.31.1, docs/FIELD-NOTES.md). Taking the default would turn the durable wait for a
# human into a one-minute wait, and the test that proves the wait works would still pass
# on a fast machine.
THREE_DAYS: Final[int] = 3 * 24 * 60 * 60

# A resumed turn may suspend AGAIN - an approval unlocks a tool whose result triggers an
# evidence request. That is legitimate, but it needs a bound: without one, a badly written
# tool that always defers turns the workflow into an infinite human-round-trip loop that
# pesters a real person forever. Bound it and abandon loudly.
MAX_HUMAN_ROUNDS: Final[int] = 5

# One topic for every human answer. A named topic rather than None so a future sender on
# another topic cannot be mistaken for an approval by a workflow waiting for one.
RESUME_TOPIC: Final[str] = "human-answer"

# ONE queue for every session, partitioned. Not one queue per session: queues are declared
# at import time in a process-wide registry, so a queue per session would mean creating a
# registry entry per conversation and there is no moment at which one could be removed.
#
# `partition_concurrency=1` is the whole of D19's serialisation, and it is a PARAMETER of
# dbos.Queue rather than something this repository builds (verified on dbos 2.31.1,
# docs/FIELD-NOTES.md). Nothing else is set: leaving global concurrency unbounded is what
# lets a thousand sessions proceed at once while each of them stays single-file. Bounding
# it here would make every tenant queue behind every other tenant's slowest turn, and no
# single-session test would ever show it.
TURN_QUEUE_NAME: Final[str] = "agent-core-turns"

turn_queue: Final[Queue] = Queue(TURN_QUEUE_NAME, partition_concurrency=1)

_TOO_MANY_ROUNDS: Final[str] = (
    "Abandoned after too many human round-trips: a tool kept asking for input instead of "
    "finishing."
)
_NO_ANSWER: Final[str] = (
    "Abandoned: nobody answered the request this turn was waiting on before it expired."
)


class TurnWorkflowNotWiredError(RuntimeError):
    """A step ran before `bind_dependencies` was called at startup.

    Loud rather than lazy-constructing anything: a workflow that builds its own use case
    would be a second composition root, and the two would drift.
    """


@dataclass(frozen=True)
class TurnWorkflowDependencies:
    """What the steps resolve their work through.

    The workflow function is a module-level object because that is how DBOS registers it,
    so its collaborators are bound at startup rather than passed as arguments: workflow
    arguments are SERIALISED into the operation log and a use case is not serialisable.

    Later anchors add fields here (the human gateway for docs/TASKS.md#t-f3-07, the context
    engine for docs/TASKS.md#t-f5-08). Adding a field is the whole change; no step signature
    moves, because a step's arguments are also part of the durable log.
    """

    start_turn: StartTurn


_dependencies: TurnWorkflowDependencies | None = None


def bind_dependencies(dependencies: TurnWorkflowDependencies) -> None:
    """Called once, by the composition root, before any workflow runs."""
    global _dependencies
    _dependencies = dependencies


def _require_dependencies() -> TurnWorkflowDependencies:
    if _dependencies is None:
        raise TurnWorkflowNotWiredError(
            "run_turn_workflow was started before bind_dependencies() was called. The "
            "composition root wires this adapter at startup; see composition.py."
        )
    return _dependencies


def _in_deterministic_order(pending: tuple[PendingRequest, ...]) -> tuple[PendingRequest, ...]:
    """The order the workflow BODY dispatches pending work in, whatever order it arrived in.

    CLAUDE.md non-negotiable #7. `pending` comes back from a runner that may have built it
    from a dict, a set, or whichever model response arrived first, and the body must not
    inherit that: a replay that dispatches the same requests in another order writes a
    different operation log and diverges from the run it is supposed to be recovering.

    Sorting on `tool_call_id` is safe because the id is unique per suspended call and is
    round-tripped verbatim (domain/turn.py, `PendingRequest`) - so this is a total order,
    not a best-effort one.
    """
    return tuple(sorted(pending, key=lambda request: request.tool_call_id))


def _closed_without_an_answer(turn_id: TurnId, reason: str, at: datetime) -> TurnOutcome:
    """A terminal outcome for a turn no human finished. Pure: the clock is a parameter.

    The clock is READ in the step that calls this and passed in, never read here. That is
    the difference between a helper a step can use and a second place non-determinism can
    hide - and only the first one survives review of CLAUDE.md non-negotiable #2.
    """
    return TurnOutcome(turn_id=turn_id, result=TurnResult(text=reason, finished_at=at))


@DBOS.step()
async def _step_new_turn_id() -> TurnId:
    """The turn id, minted INSIDE a step. CLAUDE.md non-negotiable #2.

    In the body this would be a NEW id after a crash: the recovered run would file its
    audit rows under an id the conversation has never seen, and the fork would look like
    two turns rather than one bug.
    """
    return TurnId(str(uuid4()))


@DBOS.step()
async def _step_start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
    """One await of the application layer. The use case knows nothing about DBOS.

    `await`, not a bare call: `StartTurn.execute` is a coroutine (D13), and returning the
    unawaited coroutine would hand the workflow an object it would happily record as the
    step's result while the turn never ran.
    """
    return await _require_dependencies().start_turn.execute(turn_id, request)


@DBOS.step()
async def _step_publish(
    turn_id: TurnId, request: TurnRequest, pending: tuple[PendingRequest, ...]
) -> None:
    """Ask the humans. F3 fills this in through `HumanGateway`; docs/TASKS.md#t-f3-04.

    MUST be idempotent per (turn_id, tool_call_id) once it does something: a step is
    retried after a crash, and re-publishing asks the same human the same question twice -
    which is how one action collects two conflicting approvals.
    """
    raise NotImplementedError(
        "F3 - HumanGateway has no adapter yet; docs/TASKS.md#t-f3-04. The workflow body "
        "that decides WHEN to publish is t-f2-01 and is implemented."
    )


@DBOS.step()
async def _step_resume(turn_id: TurnId, request: TurnRequest, answer: object) -> TurnOutcome:
    """Feed the human's answer back. F3 fills this in through `ResumeTurn`; t-f3-02.

    `answer` stays `object` on purpose. What a human answer looks like on the wire is
    decided by `POST /decisions/{corr_id}` (docs/TASKS.md#t-f3-11), and inventing a shape
    here would freeze a format two anchors before anything sends it.
    """
    raise NotImplementedError(
        "F3 - ResumeTurn is pseudo-code; docs/TASKS.md#t-f3-02. Resuming must be "
        "idempotent per (turn_id, tool_call_id) - see R4 below."
    )


@DBOS.step()
async def _step_abandon(turn_id: TurnId, reason: str) -> TurnOutcome:
    """End a turn that kept asking. A step, not a `raise`: the outcome is an ORDINARY
    result the caller reads, and a workflow that raises here has no result to record."""
    return _closed_without_an_answer(turn_id, reason, datetime.now(UTC))


@DBOS.step()
async def _step_expire(turn_id: TurnId) -> TurnOutcome:
    """End a turn nobody answered within `THREE_DAYS`."""
    return _closed_without_an_answer(turn_id, _NO_ANSWER, datetime.now(UTC))


@DBOS.workflow()
async def run_turn_workflow(request: TurnRequest) -> TurnOutcome:
    """One turn, durably.

    RULES THAT MAKE REPLAY CORRECT - each of these produces a SILENT bug when broken,
    visible only during crash recovery:

    R1. NON-DETERMINISM LIVES INSIDE STEPS.
        Clock, UUID, randomness - all generated inside @DBOS.step(). The workflow body
        must produce the same sequence of decisions on replay. A `uuid4()` in the body
        yields a new turn_id after a crash and forks the conversation.

    R2. NO SIDE EFFECT OUTSIDE A STEP.
        Every write, publish and model call is a step. A side effect in the body runs
        again on every replay. The only thing this body does that is not a step is
        `DBOS.recv_async`, and that is a DURABLE primitive: it is recorded in the same log
        the steps are, which is exactly why the wait survives a redeploy.

    R3. THE MODEL CALL IS NEVER INSIDE A DATABASE TRANSACTION.
        A forty-second step holding a connection ends in pool exhaustion under load.
        CLAUDE.md #3. `_step_start` is a @DBOS.step(), never a @DBOS.transaction():
        `StartTurn` awaits the store and the model in turn, and wrapping the pair in one
        transaction would hold a connection for the length of the model call.

    R4. STEPS ARE IDEMPOTENT WHERE THEY TOUCH THE WORLD.
        `_step_resume` keys on (turn_id, tool_call_id) and treats an already-resolved id
        as a no-op returning the stored outcome. Without it, a crash between the tool
        running and the outcome being persisted freezes the account twice on recovery.

    R5. COMPACTION IS A STEP, NOT A HOOK.
        _step_compact() wraps CompactContext. DBOS's durable lock gives one pass per
        session for free. docs/TASKS.md#t-f5-08.

    THE F2 ACCEPTANCE TEST, WHICH IS ALSO THE ONLY WAY THESE BUGS SURFACE:
        Start a multi-tool turn. Kill the process mid-flight. Restart. Assert the turn
        completes and the already-executed tool did NOT run twice. A green unit suite
        proves nothing about any of R1-R5. That test is docs/TASKS.md#t-f2-02.

        What tests/integration/test_durability.py proves for THIS anchor is the layer
        underneath it: the source places every producer of non-determinism inside a step,
        and the body driven twice - once executing, once replaying its step log - reaches
        the same decision sequence. Cheap, and it fails on a laptop instead of in recovery.
    """
    # SEAM t-f2-10: `_step_drain_pending(request)` belongs HERE, before the id exists,
    # because a message that arrived while the previous turn was running is part of THIS
    # turn's input and must be folded in before StartTurn persists the request.

    turn_id = await _step_new_turn_id()
    outcome = await _step_start(turn_id, request)

    rounds = 0
    while outcome.is_suspended:
        rounds += 1
        if rounds > MAX_HUMAN_ROUNDS:
            return await _step_abandon(turn_id, _TOO_MANY_ROUNDS)

        # Sorted before dispatch, never in the order the runner happened to produce.
        # CLAUDE.md non-negotiable #7.
        await _step_publish(turn_id, request, _in_deterministic_order(outcome.pending))

        # The durable wait. Explicitly THREE_DAYS - the 60-second default is the trap.
        answer = await DBOS.recv_async(RESUME_TOPIC, timeout_seconds=THREE_DAYS)
        if answer is None:
            return await _step_expire(turn_id)

        outcome = await _step_resume(turn_id, request, answer)

    # SEAM t-f3-07: `_step_deliver(request, outcome.result)` belongs HERE - a finished
    # result goes out on the session's channel through the registry, with no new port (D23).
    # SEAM t-f5-08: `_step_compact(request.session)` belongs HERE, after delivery, so a
    # compaction pass never delays the answer the human is waiting for.
    return outcome


def session_partition_key(session: SessionRef) -> str:
    """The queue partition one conversation runs in. Pure, so it is the same on replay.

    THE TENANT IS PART OF THE KEY, and that is not defensive padding. `SessionId` is
    unique WITHIN a tenant (domain/turn.py, `SessionRef`), so keying on the session id
    alone makes two tenants that happen to pick the same id serialise against each other -
    one tenant's slow turn stalling another tenant's conversation, invisible to every
    single-tenant test and reported as "the product is sometimes slow".

    The separator is a character neither identifier can contain without the pair becoming
    ambiguous; if either ever admits a "/", this function needs an escape, not a longer
    separator.
    """
    return f"{session.tenant_id}/{session.session_id}"


async def enqueue_turn(request: TurnRequest) -> WorkflowHandleAsync[TurnOutcome]:
    """Hand one turn to the queue and return its handle WITHOUT waiting for it.

    This is the only supported way to start a turn. Calling `run_turn_workflow` directly,
    or starting it with `DBOS.start_workflow_async`, bypasses the partition and lets two
    messages of one conversation run at once - which is exactly the interleaving D19
    exists to prevent, and it fails only under real concurrent load.

    The caller does not await the RESULT here: an HTTP handler that waits for the turn
    holds its connection for the length of a model call, and the durable handle is what a
    caller polls or resolves later (docs/TASKS.md#t-f2-05 owns the route).

    `queue_partition_key` travels per enqueue rather than per queue, which is why one
    queue serves every session; see docs/FIELD-NOTES.md.
    """
    with SetEnqueueOptions(queue_partition_key=session_partition_key(request.session)):
        return await turn_queue.enqueue_async(run_turn_workflow, request)
