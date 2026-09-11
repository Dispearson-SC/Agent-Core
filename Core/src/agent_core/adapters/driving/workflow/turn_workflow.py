"""Driving adapter: the DBOS workflow that orchestrates one turn.

Phase:   F0 (the id a caller polls) / F2 (durability) / F3 (the human wait, outbound
         delivery) / F5 (compaction step)
Tasks:   docs/TASKS.md#t-f2-01, docs/TASKS.md#t-f2-03, docs/TASKS.md#t-f2-10,
         docs/TASKS.md#t-f2-11, docs/TASKS.md#t-f3-07, docs/TASKS.md#t-f3-17,
         docs/TASKS.md#t-f0-06, docs/TASKS.md#t-f3-11
Status:  IMPLEMENTED (t-f2-01, t-f2-03, t-f2-10, t-f2-11, t-f3-07) - the body, its steps,
         the per-session queue, the coalescing window in front of it, the pending-input
         drain, outbound delivery, and the seams later anchors fill.
         t-f0-06 IMPLEMENTED - the domain `TurnId` and the turn workflow's durable id are
         now ONE value; see ONE ID, NOT TWO below.
         t-f3-11 IMPLEMENTED on this side - `signal_decision` is the send paired with the
         body's `DBOS.recv_async`, and `HumanAnswer` is the shape it puts on the wire.
         t-f7-11 IMPLEMENTED - the wire admits what F7 has to put on it. `HumanPayload`
         widens `HumanAnswer.note` from `str | None` to include a `MediaRef`, and
         `signal_evidence` is the evidence door onto the same durable topic; before it,
         F7's own acceptance test could only reach this wire through a `cast`.
         t-f3-17 IMPLEMENTED - `_step_publish` asks through `HumanGateway`.
         t-f3-16 IMPLEMENTED on this side - `_step_resume` turns a `HumanAnswer` into the
         `tuple[ToolResolution, ...]` `ResumeTurn` takes and supplies `request.caller` as
         the identity the resumed continuation is policed and audited as. `composition.py`
         binds the `resume_turn` seat (t-f3-20); a container built without it still
         refuses loudly rather than silently skipping the resume.
         t-f5-08 IMPLEMENTED - `_step_compact` is R5's step. It wraps `CompactContext` and
         runs AFTER delivery, so a pass never delays the answer a human is waiting for.
         `composition.py` does not bind the `compaction` seat yet; an unbound seat is the
         one silence this file allows, and COMPACTION SILENCE below says why.

ONE ID, NOT TWO - docs/TASKS.md#t-f0-06, AND WHICH END OWNS IT
    The domain `TurnId` IS this workflow's durable id. `_step_new_turn_id` reads it back
    off the running workflow rather than minting a fresh `uuid4`, and `enqueue_turn` pins
    it with `SetWorkflowID` so an edge that has to answer a caller BEFORE the turn runs
    can know it in advance.

    THE EDGE OWNS IT, AND IT HAS TO. `POST /turns` answers 202 with an id and promises the
    answer will be there later; `GET /turns/{turn_id}`, every audit row, the
    `human_requests` correlation and `DecideApproval`'s wake-up all have to name that same
    turn. An id minted inside the workflow cannot be handed to a caller who has already
    been answered, so the only end that CAN own it is the one that answers first.

    That does not weaken CLAUDE.md non-negotiable #2, it strengthens it. The old
    `uuid4()`-in-a-step was replay-safe only because a step result is recorded; a durable
    workflow id is the KEY that record is stored under, so it cannot differ between the
    run that crashed and the run that recovers it. What #2 forbids is a value that changes
    on replay, and this one is the one value that provably cannot.

    It is still read inside a step, for one reason worth stating: the id is then written
    into the operation log next to everything filed under it, so the audit trail can be
    joined to the run without consulting DBOS's own tables.

    The consequence that makes docs/TASKS.md#t-f3-11 possible at all:
    `DBOS.send_async(destination_id=str(turn_id))` addresses the workflow that is waiting.
    While the two ids differed, that send went to a workflow id nothing had ever created -
    and it raised nothing anybody saw. The decision was recorded, the caller was told it
    was accepted, and the turn slept until it expired.

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

COMPACTION SILENCE - THE ONE NO-OP DEFAULT IN THIS FILE, AND WHY IT IS NOT THE OTHERS
    `human_gateway` and `resume_turn` default to `None` and RAISE when reached, because
    there is no correct silent behaviour for "a question reached nobody" or "a decision was
    dropped". `compaction` defaults to `None` and returns instead, and the difference is
    what an unwired seat costs: a turn that did not compact is a CORRECT turn. It answers
    the human, it is audited, and nothing it produced is wrong. It is only more expensive
    later, and a deployment is entitled to run without compaction at all.

    That is a real risk and it is named rather than hidden: docs/STATE.md records that half
    the ladder was inert for ten waves because an OPTIONAL seat had no adapter and nothing
    warned. The defence here is not a raise at the end of a delivered turn - it is that the
    seat is ONE value (`CompactionSeat`) with no default on any of its three fields, so a
    composition root cannot bind compaction and forget the window it is a fraction of.

WHY THERE ARE TWO QUEUES AND TWO WORKFLOWS - READ THIS BEFORE MERGING THEM
    D19 asks for one enqueue that is partitioned per session AND deduplicated over a
    window. On the installed dbos 2.31.1 those two are mutually exclusive:
    `Queue._validate_enqueue` raises "Deduplication is not supported for partitioned
    queues", `Debouncer` refuses the identical pair, and a partitioned queue cannot be
    enqueued onto without a key. docs/FIELD-NOTES.md carries the evidence and the
    RETRACTED note that this replaces.

    So the window is a SECOND, non-partitioned queue carrying a second, deliberately tiny
    workflow: `run_turn_window` waits out its delay, enqueues the real turn onto the
    partitioned queue, and finishes. `t-f2-11` is that pair; `t-f2-05` is the routes.py
    half that enqueues onto it.

    THE WINDOW WORKFLOW MUST NOT AWAIT THE TURN. A deduplication id is released when its
    workflow COMPLETES, so a window that awaited the turn would hold the key for the
    length of the turn - and D19's mid-turn message would then coalesce onto a window
    whose turn has already drained the buffer. That message never becomes a turn and
    nothing raises: it simply sits in a table unanswered. Returning immediately frees the
    key in milliseconds, so a mid-turn message opens a NEW window and the partition
    serialises the follow-up turn behind the running one - D19's "collect" mode, which is
    the wanted behaviour and needs no extra code.

WHICH MESSAGE TRAVELS IN THE TURN, ANSWERED ONCE FOR BOTH HALVES OF THE COALESCING
    `duplication_policy="return-existing"` keeps the FIRST message's arguments and
    discards every later one, so the enqueued turn's `request.input` is the first
    sentence and the buffer holds each sentence that joined after it. `_step_drain_pending`
    therefore EXTENDS `request.input` with the buffered messages, in arrival order; it
    never REPLACES it. The routes.py half obeys the same answer by buffering exactly the
    messages that coalesced, and only those. If the two halves disagreed the drain would
    either repeat one sentence or lose one, and no test could say which.

    `_step_publish` is WIRED (docs/TASKS.md#t-f3-17): `HumanGateway` landed at t-f3-04 and
    the step asks through it. `_step_resume` is wired too (docs/TASKS.md#t-f3-16), and it
    reads the `HumanAnswer` that `POST /decisions/{corr_id}` puts on the durable topic. It
    refuses anything else rather than coercing it: a silent fallback on either step would
    resume a turn without asking anybody.

NOT EVERY SUSPENSION IS A QUESTION FOR A HUMAN
    Three mechanisms suspend a turn through one primitive: an approval, an evidence
    request, and `ask_peer` (docs/TASKS.md#t-f9-04). Only the first two are answered by a
    person; a peer ask is answered by another AGENT's turn, arriving on this same
    `RESUME_TOPIC`. So the body publishes `_for_a_human(...)` and not `outcome.pending`,
    and it enters the durable wait either way. Publishing all of it sent somebody a
    question nobody could answer and then waited three days on them for it - which looks,
    from their side, exactly like the system asking something incomprehensible and then
    going quiet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import uuid4

from dbos import DBOS, Queue, SetEnqueueOptions, SetWorkflowID, WorkflowHandleAsync

from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.application.compact_context import CompactContext
from agent_core.application.resume_turn import ResolvedTool, ResumeTurn
from agent_core.application.start_turn import StartTurn
from agent_core.domain.compaction import ContextState
from agent_core.domain.media import MediaRef
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    PendingRequest,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    Usage,
    UserInput,
)
from agent_core.ports.human_gateway import HumanGateway

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "MAX_HUMAN_ROUNDS",
    "RESUME_TOPIC",
    "THREE_DAYS",
    "TURN_QUEUE_NAME",
    "WINDOW_QUEUE_NAME",
    "CompactionSeat",
    "HumanAnswer",
    "HumanPayload",
    "NotInsideAWorkflowError",
    "PendingInputs",
    "TurnWorkflowDependencies",
    "TurnWorkflowNotWiredError",
    "UnusableResumeAnswerError",
    "WindowEnqueue",
    "bind_dependencies",
    "enqueue_turn",
    "enqueue_turn_window",
    "run_turn_window",
    "run_turn_workflow",
    "session_partition_key",
    "session_window_key",
    "signal_decision",
    "signal_evidence",
    "turn_id_of_window",
    "turn_queue",
    "window_id_for_turn",
    "window_queue",
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

# The coalescing window (t-f2-11). A SECOND queue, and deliberately NOT partitioned: this
# is the queue the deduplication id lives on, and dbos 2.31.1 refuses deduplication on a
# partitioned queue. Nothing is serialised here on purpose - the window workflow only
# hands work to `turn_queue`, and it is `turn_queue` that decides what may run at once.
WINDOW_QUEUE_NAME: Final[str] = "agent-core-turn-window"

window_queue: Final[Queue] = Queue(WINDOW_QUEUE_NAME)

# D19's "delay_seconds ~ 1.5". A fixed window, not a sliding one: `return-existing` hands
# back the waiting workflow without extending its delay, so the reply is at most this late
# however many messages arrive. A sliding window would let a fast typist postpone their
# own answer indefinitely.
DEFAULT_WINDOW_SECONDS: Final[float] = 1.5

# A coalescing window's durable id is its turn's id behind this prefix, and that is the
# whole mechanism by which a caller learns the turn id before the turn exists
# (docs/TASKS.md#t-f0-06). See `window_id_for_turn` for why a DERIVATION and not a second
# minted value.
_WINDOW_ID_PREFIX: Final[str] = "window-"

_TOO_MANY_ROUNDS: Final[str] = (
    "Abandoned after too many human round-trips: a tool kept asking for input instead of "
    "finishing."
)
_NO_ANSWER: Final[str] = (
    "Abandoned: nobody answered the request this turn was waiting on before it expired."
)


class PendingInputs(Protocol):
    """Reads back the messages that joined a turn while its window was open.

    NOT a sixteenth port, for the same reason `ChannelRegistry` is not one (D23, D15):
    `application/` never learns that coalescing exists. A turn is started with the input
    it is started with, and folding the buffered sentences into that input is an
    orchestration concern of this adapter. Declaring it structurally here keeps the
    "adding a vertical is one profile file plus one tools package" contract untouched.

    `PgPendingInputBuffer` (docs/TASKS.md#t-f2-04) is the implementation. Only `drain` is
    named here: this file must not be able to APPEND. Appending is the arriving message's
    business and it happens in routes.py, and a workflow that could write to the buffer
    could write a sentence the user never sent.
    """

    async def drain(self, session: SessionRef) -> tuple[UserInput, ...]: ...


class _NoPendingInputs:
    """The default: a process with no coalescing window has nothing buffered.

    Silent rather than loud, and that is safe only because of one invariant: the buffer is
    written by `routes.coalescing_turn_starter` and by nothing else, so a process that
    never built one never appended a row either, and an empty drain loses nothing.
    `composition.py` wires the starter and this field together or neither.
    """

    async def drain(self, session: SessionRef) -> tuple[UserInput, ...]:
        return ()


class TurnWorkflowNotWiredError(RuntimeError):
    """A step ran before `bind_dependencies` was called at startup.

    Loud rather than lazy-constructing anything: a workflow that builds its own use case
    would be a second composition root, and the two would drift.
    """


class NotInsideAWorkflowError(RuntimeError):
    """Something asked for the running workflow's identity and there was not one.

    Its own type rather than a bare `RuntimeError` because it says something specific: the
    call site is only reachable from inside a `@DBOS.workflow()` or a `@DBOS.step()` of
    one, so reaching it means a step or a helper was invoked directly - by a test, or by
    code that bypassed the queue. Silently minting an id instead would file the turn under
    an identifier that addresses no workflow, which is the exact defect
    docs/TASKS.md#t-f0-06 exists to remove.
    """


class UnusableResumeAnswerError(RuntimeError):
    """The durable wait woke on something that is not this turn's answer.

    Its own type rather than a `ValueError`, because the two shapes it covers mean the same
    operational thing and want one name in an incident: a payload of an unreadable type, and
    a `HumanAnswer` addressed to a different turn. Both say the workflow was woken by
    something it must not act on.

    LOUD, NEVER A FALLBACK. The alternative is resuming a turn on a decision nobody made
    about it - the approval-without-an-approver the correlation table exists to prevent -
    and it would not raise anywhere afterwards: the resolution would name a `tool_call_id`
    the model never issued, Pydantic AI would drop it in silence, and the agent would ask
    the same question until the turn expired.
    """


def _current_workflow_id() -> str:
    """The durable id of the workflow running right now. See ONE ID, NOT TWO up top.

    `DBOS.workflow_id` is an attribute of the durable execution, not a fresh value: it is
    the key the operation log is stored under, so it is identical on the run that crashed
    and on the run that recovers it.
    """
    workflow_id = DBOS.workflow_id
    if workflow_id is None:
        raise NotInsideAWorkflowError(
            "no workflow is executing, so there is no durable id to file this turn "
            "under. A turn is started through enqueue_turn (docs/TASKS.md#t-f2-03); "
            "calling a step directly bypasses the queue and the partition with it."
        )
    return workflow_id


@dataclass(frozen=True, slots=True)
class CompactionSeat:
    """Everything R5's step needs, bound as ONE value so it cannot be half-wired.

    docs/TASKS.md#t-f5-08. Three fields, none with a default, and that is the whole design:
    a composition root either states all three or binds no compaction at all.

    WHY THE WINDOW IS HERE AND NOT DERIVED
        The ladder's target is a FRACTION of the context window and the trigger divides by
        it, but nothing on `ContextEngine.compress` carries it - `LadderContextEngine` takes
        it as a construction parameter for exactly this reason, and its comment says who
        owns the number: "the composition root knows which model a deployment runs".

        It must not have a default here, and specifically not zero.
        `ports/context_engine.py` resolves an unknown window TOWARDS compaction - correctly,
        because the alternative is an agent that dies of overflow - so a seat that silently
        defaulted the window to zero would make the trigger fire on EVERY turn. That is the
        expensive mistake `domain/compaction.py` opens by naming: every pass rewrites the
        prompt prefix, the provider's cache is invalidated, and the next request re-bills
        the whole prompt at full price. It fails no test. It appears on the invoice.

    WHY THE PROFILES ARE HERE AND NOT RE-LOADED
        `CompactionPolicy` is per profile (`AgentProfile.compaction`), and `TurnRequest`
        carries a profile ID rather than a profile. `StartTurn` already holds the loaded
        mapping and already refused this turn if the id named nothing, so the same mapping
        is handed here rather than a second loader being built - two readers of one profile
        registry is how one of them comes to be looking at a stale copy.
    """

    compact: CompactContext
    profiles: Mapping[str, AgentProfile]
    context_window: int


@dataclass(frozen=True)
class TurnWorkflowDependencies:
    """What the steps resolve their work through.

    The workflow function is a module-level object because that is how DBOS registers it,
    so its collaborators are bound at startup rather than passed as arguments: workflow
    arguments are SERIALISED into the operation log and a use case is not serialisable.

    Later anchors add fields here (the human gateway for docs/TASKS.md#t-f3-04, the context
    engine for docs/TASKS.md#t-f5-08). Adding a field is the whole change; no step signature
    moves, because a step's arguments are also part of the durable log.
    """

    start_turn: StartTurn

    # t-f3-07. D23's shared lookup table, NOT a sixteenth port: the same registry
    # `ChannelHumanGateway` asks the mid-turn question on is the one a finished turn leaves
    # on. It is a `ChannelRegistry` and never a `Mapping`, because the registry is what
    # refuses a duplicate id at construction and raises on a miss at `get`.
    #
    # The default is an EMPTY registry rather than `None`, and that is deliberate: an
    # unwired process must fail delivery the same way a misspelled channel id does -
    # `UnknownChannelError`, naming the id and listing what is registered. Two spellings of
    # "the answer has nowhere to go" would mean two things to read in an incident, and only
    # one of them would be recognised.
    channels: ChannelRegistry = field(default_factory=lambda: ChannelRegistry(()))

    # t-f2-10. Read-only by construction - see `PendingInputs`. The default drains
    # nothing, which is correct for a process with no coalescing enqueue in front of it
    # and wrong for one that has: wire this wherever `coalescing_turn_starter` is wired.
    pending_inputs: PendingInputs = field(default_factory=_NoPendingInputs)

    # t-f3-17. The adapter is `ChannelHumanGateway` (t-f3-04) and `composition.py` already
    # builds one for `DecideApproval` - `bind_turn_workflow` passes it here so the turn
    # that ASKS and the route that ANSWERS share one correlation table. Two instances would
    # still work by accident, because the table is the state; one instance is what makes
    # that an invariant rather than a coincidence.
    #
    # `None` rather than a no-op default, and that is the opposite choice from `channels`
    # above on purpose. An empty registry has a correct loud failure of its own
    # (`UnknownChannelError`); there is no equivalent for "ask a human" - a silent gateway
    # would let a turn suspend on an approval that was never sent to anybody, wait three
    # days and expire, with nothing anywhere saying a question went missing.
    human_gateway: HumanGateway | None = None

    # t-f3-16. `None` rather than a no-op, for the same reason as `human_gateway` above:
    # there is no correct silent behaviour for "a human answered and nothing applied it".
    # A default that returned some outcome would let a turn swallow a real decision and
    # then complete as though it had been resumed, which is indistinguishable from success
    # everywhere downstream.
    resume_turn: ResumeTurn | None = None

    # t-f5-08. `None` means this deployment does not compact, and `_step_compact` returns
    # rather than raising - see COMPACTION SILENCE in the module docstring for why this is
    # the one no-op default here and `human_gateway` above is not. The seat is atomic, so
    # "bound but missing its window" is not a state that exists.
    compaction: CompactionSeat | None = None


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


def _answerable_by_a_human(pending: PendingRequest) -> bool:
    """Whether a PERSON can resolve this request, decided from the request's KIND.

    A suspended turn does not necessarily wait on a human. `ask_peer`
    (docs/TASKS.md#t-f9-04) is the third user of one suspension mechanism, and the thing
    that answers it is ANOTHER AGENT's turn - it arrives back on the same durable topic
    that an approval does. Publishing it puts a question in front of somebody who has no
    way to answer it: the person sees an ask, cannot act on it, and the turn spends its
    three days waiting on them while the peer's answer was never theirs to give.

    THE TOOL NAME IS NO LONGER CONSULTED, AND THAT WAS t-f9-08'S POINT
        This function used to match `ASK_PEER_TOOL`, because both kinds a peer ask could
        wear were defined in terms of a human and the honest discriminator did not exist.
        `PendingKind.DELEGATION` is that discriminator, and `domain/turn.py` answers the
        question once, in a written-out grid with no default - so a new kind leaves a hole
        there and raises, rather than being published by accident to whoever is nearest.
        Two readers spelling the same distinction for themselves is exactly how one of
        them gets forgotten.
    """
    return pending.kind.answerable_by_a_human


def _for_a_human(pending: tuple[PendingRequest, ...]) -> tuple[PendingRequest, ...]:
    """The subset of a suspension that a person can actually act on, order preserved.

    A FILTER, never a branch that skips the publish when a peer ask is present: one
    outcome can carry both - a peer being consulted AND an approval a person is standing by
    to give - and dropping the whole publish would swap a question nobody can answer for an
    approval nobody was asked for. Both are a turn stuck for three days; only one of them
    would look like a bug in this file.

    Deterministic by construction (CLAUDE.md non-negotiable #7): it is a pure predicate
    applied in the order it was handed, so it preserves whatever order
    `_in_deterministic_order` established rather than establishing a second one.
    """
    return tuple(request for request in pending if _answerable_by_a_human(request))


def _closed_without_an_answer(turn_id: TurnId, reason: str, at: datetime) -> TurnOutcome:
    """A terminal outcome for a turn no human finished. Pure: the clock is a parameter.

    The clock is READ in the step that calls this and passed in, never read here. That is
    the difference between a helper a step can use and a second place non-determinism can
    hide - and only the first one survives review of CLAUDE.md non-negotiable #2.
    """
    return TurnOutcome(turn_id=turn_id, result=TurnResult(text=reason, finished_at=at))


@DBOS.step()
async def _step_new_turn_id() -> TurnId:
    """The turn id: this workflow's own durable identity. docs/TASKS.md#t-f0-06.

    IT IS NOT MINTED HERE ANY MORE, AND THAT IS THE POINT OF THE ANCHOR
        It used to be `uuid4()`, which satisfied CLAUDE.md non-negotiable #2 - a step
        result is recorded, so replay read the same value back - and satisfied nothing
        else. The id a caller was handed by `POST /turns` was the workflow's; the id the
        audit rows, the correlation table and `DecideApproval`'s wake-up used was this
        one. Two identifiers wearing one type name, joined by nothing, and every lookup
        across them returned empty forever without raising.

        Reading the workflow id instead makes them one value by construction rather than
        by a join somebody has to remember to write. The module docstring's ONE ID, NOT
        TWO says which end owns it and why replay is strictly safer, not weaker: this is
        the key the operation log itself is stored under.

    STILL A STEP, deliberately. Nothing here is non-deterministic any more, so the step is
    not what makes it replay-safe - the durable id is. What the step buys is a RECORD: the
    turn id lands in the operation log beside the steps that filed rows under it, so the
    audit trail can be joined to the run from the application's own data.
    """
    return TurnId(_current_workflow_id())


@DBOS.step()
async def _step_drain_pending(request: TurnRequest) -> TurnRequest:
    """The body's FIRST step: fold every sentence that joined this window into the turn.

    docs/TASKS.md#t-f2-10, and the other half of docs/TASKS.md#t-f2-11.

    THE DRAINED BUFFER EXTENDS `request.input`. IT DOES NOT REPLACE IT.
        This is the one question the coalescing halves have to answer together, and the
        answer follows from `duplication_policy="return-existing"`: a colliding enqueue
        gets the waiting workflow's handle back and its OWN arguments are discarded, so
        the turn already carries the FIRST message. routes.py buffers exactly the
        messages that collided, so the buffer holds the rest, in arrival order.

        Replacing would drop the first sentence. Buffering every message and replacing
        would work too - it is what D19's prose describes - but it makes an empty drain
        produce a turn with no user input at all, and an empty drain is reachable (a
        crash between the DELETE committing and this step's result being recorded). This
        way the turn always has at least the message it was started with.

    A STEP, NOT A LINE IN THE BODY, and not only because it does I/O. `drain` is
    destructive: the rows are deleted as they are read (one `DELETE ... RETURNING`, see
    pending_input_repository.py). Run from the body it would run again on every replay
    and the second run would find the buffer empty, so the recovered turn would answer a
    third of what the user said. As a step, the combined request is recorded once and
    replay returns it verbatim.

    Empty is the NORMAL case - a session whose user sent one message buffers nothing -
    so it returns the request unchanged rather than treating it as an error.
    """
    buffered = await _require_dependencies().pending_inputs.drain(request.session)
    if not buffered:
        return request

    sentences = [request.input.text, *(message.text for message in buffered)]
    return replace(
        request,
        input=UserInput(
            text="\n".join(sentence for sentence in sentences if sentence),
            # Media is carried by the request only. The buffer table holds text (F7 has
            # not extended it), so joining `media` from it would silently invent an empty
            # tuple over whatever the turn arrived with.
            media=request.input.media,
        ),
    )


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
    """Ask the humans, through `HumanGateway`. docs/TASKS.md#t-f3-17.

    `pending` is ALREADY filtered by `_for_a_human` and sorted by `_in_deterministic_order`
    - both in the body, where the decision is replayable. This step asks; it does not
    choose who to ask.

    IDEMPOTENCE IS THE ADAPTER'S, AND IT IS REAL
        A step is re-executed after a crash, and re-publishing asks the same person the
        same question twice - which is how one action collects two conflicting approvals.
        `ChannelHumanGateway.publish` enforces it with a UNIQUE constraint and an
        `ON CONFLICT DO NOTHING` that also decides whether the channel send happens, so
        the retry is silent on the wire as well as in the table. Nothing is added here: an
        "already published?" check in the workflow would be a second, weaker copy that
        two concurrent step attempts could both pass.

    AN EMPTY TUPLE RETURNS BEFORE THE GATEWAY IS RESOLVED, AND THAT ORDER IS DELIBERATE.
        A turn suspended only on a peer ask has nothing for a person, and a deployment
        that never asks anybody is not misconfigured for not having wired a gateway. The
        unwired failure below therefore lands on the first turn that genuinely needs a
        human - late, but exactly when the missing wiring is the reason a person was not
        asked, which is the moment the message is worth reading.
    """
    if not pending:
        return
    gateway = _require_dependencies().human_gateway
    if gateway is None:
        raise TurnWorkflowNotWiredError(
            "the turn suspended on a human but no HumanGateway is bound, so the question "
            "would reach nobody and the turn would expire in silence. composition.py's "
            "bind_turn_workflow must pass human_gateway=container.human_gateway; "
            "docs/TASKS.md#t-f3-17."
        )
    await gateway.publish(turn_id, request.session, pending)


@DBOS.step()
async def _step_resume(turn_id: TurnId, request: TurnRequest, answer: object) -> TurnOutcome:
    """Feed the human's answer back into the suspended turn. docs/TASKS.md#t-f3-16.

    THE WIRE SHAPE. `HumanAnswer` is what `signal_decision` sends and what
    `DBOS.recv_async` hands this step (docs/TASKS.md#t-f3-11). `answer` stays `object`
    because the parameter is part of the durable step log and a peer's reply arrives on
    this same topic (docs/TASKS.md#t-f9-04); the narrowing belongs here, to the reader.

    WHICH CALLER THE RESUMED CONTINUATION IS POLICED AS, WHICH IS THE POINT OF THE ANCHOR
        `request.caller` - the identity the TURN belongs to, carried in the request this
        step already holds. A resume is not one tool call: the model may ask for more on
        the same continuation, and `ToolPolicy.load_rules` needs an identity before any of
        them can be checked, audited or compacted.

        IT IS NOT THE APPROVING HUMAN, and that is a rule rather than a convenience.
        CLAUDE.md non-negotiable #9: no code path widens one identity into another. The
        approver reviewed ONE call; treating them as the actor would run every later call
        on the continuation under the reviewer's permissions, so approving a refund would
        silently lend the agent everything else that operator may do. The permissions the
        model acts under do not change because somebody said yes once.

        `HumanAnswer` carries no identity for exactly this reason, and it should not grow
        one: who decided is already recorded by `DecideApproval` through
        `AuditSink.record_human_decision`, on the human's own request, before this step
        ever wakes.

    A SILENT FALLBACK IS THE ONE THING THIS MUST NOT DO. Returning a plausible outcome
    here resumes a turn as though a human had answered when nobody did - the exact
    approval-without-an-approver the correlation table exists to prevent. So a payload
    that is not this turn's answer raises rather than being coerced: `HumanAnswer` carries
    its own `turn_id` precisely so that mismatch is detectable, and a resolution built from
    a misrouted payload would name a `tool_call_id` the model never issued - which Pydantic
    AI drops silently while the agent asks the same question forever.

    IDEMPOTENCE IS THE USE CASE'S (R4 below). `ResumeTurn` keys on
    `(turn_id, tool_call_id)` and returns the stored outcome for an answered pair, so a
    replayed step re-runs no tool. Nothing is re-checked here: a second, weaker copy of
    that rule in the adapter is how the two come to disagree.
    """
    if not isinstance(answer, HumanAnswer):
        raise UnusableResumeAnswerError(
            f"the durable wait woke with a {type(answer).__name__} on {RESUME_TOPIC!r} and "
            "only a HumanAnswer (docs/TASKS.md#t-f3-11) can be turned into a resolution. A "
            "peer's reply arrives on this topic too (docs/TASKS.md#t-f9-04) and has no "
            "reader yet; guessing at the payload resumes the turn on something nobody sent."
        )
    if answer.turn_id != turn_id:
        raise UnusableResumeAnswerError(
            f"an answer for turn {answer.turn_id!r} was delivered to turn {turn_id!r}. "
            "Applying it would resume this turn on a decision given about another one."
        )

    resume = _require_dependencies().resume_turn
    if resume is None:
        raise TurnWorkflowNotWiredError(
            "the turn was answered but no ResumeTurn is bound, so the human's decision "
            "would be dropped and the turn would expire as though nobody had replied. "
            "composition.py's bind_turn_workflow must pass resume_turn=container.resume_turn."
        )

    return await resume.execute(
        turn_id,
        request.session,
        request.profile_id,
        (
            ResolvedTool(
                tool_call_id=answer.tool_call_id,
                approved=answer.approved,
                # What the human supplied IS the tool result the model receives: a reason
                # when they refused, so the agent can adapt instead of retrying blindly; a
                # `MediaRef` when they were asked for a file and sent one (t-f7-11), which
                # `ResumeTurn` recognises by TYPE and turns into bytes or a signed URL;
                # `None` on a bare approval, which is what tells the runner to execute the
                # deferred call itself rather than answer it (ports/agent_runner.py).
                #
                # Passed through unnarrowed on purpose. `HumanPayload` is the carrier's
                # whole vocabulary and `ResolvedTool.payload` is `object | None`, so a
                # branch here would be this adapter deciding what an evidence answer means
                # - a second copy of a rule that already lives in `ResumeTurn`, and the
                # day a third kind of answer lands, the copy that is not updated is the
                # one that silently drops it.
                payload=answer.note,
            ),
        ),
        caller=request.caller,
    )


@DBOS.step()
async def _step_deliver(request: TurnRequest, result: TurnResult) -> None:
    """The return trip: put a finished turn on the channel it arrived on. t-f3-07, D23.

    WHY THERE IS NO PORT HERE
        D23 Shape A. This is dispatch over `CallerIdentity.channel`, a value the domain
        already carries - not a new question the domain asks. The registry lives in
        `adapters/driving/channels/registry.py` and `application/` never learns a channel
        exists. A sixteenth port would be the D15 bar unmet twice over.

    WHY A MISS RAISES INSTEAD OF RETURNING
        `ChannelRegistry.get` raises `UnknownChannelError` when nothing is registered under
        the id, and this step deliberately does not catch it. The alternative - skip the
        send, return the outcome - produces a turn that completes, is audited as
        successful, and reaches nobody. That failure has no symptom inside the process and
        is reported days later as "the bot ignores me". A failed workflow, with the missing
        id and the registered ids in the message, is the cheap version of the same news.

    DELIVERY IS AT-LEAST-ONCE, AND THIS IS THE WINDOW
        DBOS records a step's result AFTER the function returns. So a crash between the
        channel accepting the message and that result being written leaves no record of the
        send, and recovery replays this step and sends the SAME answer again. The human
        sees the reply twice.

        That is the deliberate trade, not an oversight: the other side of it is a crash in
        the same window losing the answer permanently, and a duplicate reply is a
        readable annoyance while a missing one is an unanswered customer. It cannot be
        closed from inside this step either - a "mark sent" write would itself be a second
        step with its own identical window. Real once-only delivery needs a per-channel
        idempotency key carried to the platform's own dedup, which is `docs/GAPS.md` A4's
        open item ("per-channel delivery retry and idempotency semantics"), not this
        anchor's.

    DETERMINISM
        Nothing non-deterministic is produced here (CLAUDE.md #2): the result and the
        caller are arguments, so a replay of this step sends exactly the bytes the first
        attempt sent. One turn goes to exactly one channel, so there is no fan-out and no
        `asyncio.gather` - if a future version ever delivers to several places at once, the
        targets are sorted before dispatch, because a set or a completion order would make
        replay take another path (CLAUDE.md #7).
    """
    await _require_dependencies().channels.deliver(
        request.caller, OutboundMessage.from_turn_result(result)
    )


@DBOS.step()
async def _step_compact(request: TurnRequest, usage: Usage) -> bool:
    """R5: one compaction pass for this session, wrapping `CompactContext`. t-f5-08.

    SILENT-BUG AREA (CLAUDE.md). Nothing here fails a test when it is wrong; it shows up on
    the bill, months later, as a number nobody can attribute.

    THIS IS THE STEP, NOT THE POLICY. `CompactContext` owns whether a pass runs and what is
    committed; `domain/compaction.py` owns the ladder and the trigger's boundary;
    `LadderContextEngine` owns what a rung does. Re-deciding any of that here would be a
    second copy of a rule that only disagrees with the first one on the invoice.

    A STEP, AND THAT IS WHERE THE DURABLE LOCK COMES FROM
        `ports/context_engine.py` lists what Hermes hand-built across several hundred lines
        - a deep copy, a commit fence, a durable lock giving one pass per session, sessions
        still running concurrently - and says we get all of it by making compaction a step.
        This is that step, and the lock is `turn_queue`'s `partition_concurrency=1`: two
        messages of one conversation cannot run at once, so two passes cannot overlap and
        read the same pre-compaction history. Nothing is re-implemented here; the whole
        mechanism is which queue the turn was enqueued onto.

        The step also buys the replay half. A recovered turn reads this result out of the
        operation log instead of paying the summariser again - which matters more here than
        anywhere else in this file, because the thing not repeated is a model call.

    IT IS CALLED ONCE, FROM ONE PLACE, AND NEVER FROM A LOOP
        `CompactContext` step 3 refuses to retry a pass that freed nothing, and that
        refusal is only worth anything if the step cannot be re-entered. A loop around this
        call - "compact until it fits" - turns the refusal straight back into the infinite
        loop it exists to prevent, and that loop rewrites the prompt prefix on every turn
        of it. `tests/integration/test_compaction_step.py` asserts the call's POSITION over
        the source, because an infinite loop hangs a suite rather than failing it.

    NEVER INSIDE A DATABASE TRANSACTION (CLAUDE.md non-negotiable #3). `@DBOS.step()`, never
    `@DBOS.transaction()`: rungs L3 and L4 each reach a summariser model, and a summariser
    call is not a fast one. Holding a connection for its length is how a pool is exhausted.

    WHAT THE `ContextState` IS BUILT FROM, AND THE ONE FIELD THAT IS NOT REAL
        `window_used` is passed through verbatim, `None` included - `ports/context_engine.py`
        owns the fallback and a fraction invented here would be a second trigger.

        `estimated_tokens` is `input_tokens + output_tokens` of the turn that just
        finished: the whole prompt the provider billed for plus what was appended to it,
        which is the size of the conversation as it now stands. That is the arithmetic
        docs/TASKS.md#t-f5-11 settled in `LadderContextEngine.update_from_response`, and it
        names the alternative - a running total crosses the trigger once and then stays
        across it, so the ladder runs on every turn forever.

        `message_count` is 0 and is the honest answer available here: the workflow does not
        hold the history, `CompactContext` loads it. Nothing reads the field - not the
        trigger, not the ladder - so it is informational, and loading the history a second
        time to populate it would pay a database round trip for a number nobody asks.

    AN UNBOUND SEAT RETURNS, IT DOES NOT RAISE. See COMPACTION SILENCE in the module
    docstring. An unknown profile id DOES raise: `StartTurn` resolved the same id before
    this turn could finish, so reaching here with a miss means two profile registries have
    drifted apart, and compacting under a default profile's policy would apply another
    agent's trigger and another agent's ladder to this conversation.
    """
    seat = _require_dependencies().compaction
    if seat is None:
        return False

    profile = seat.profiles.get(request.profile_id)
    if profile is None:
        raise TurnWorkflowNotWiredError(
            f"the turn ran under profile {request.profile_id!r} and the compaction seat "
            "has no profile under that id, so there is no CompactionPolicy to compact by. "
            "StartTurn resolved this same id to start the turn: the two profile mappings "
            "have drifted, and composition.py must hand both seats one registry. "
            "docs/TASKS.md#t-f5-08"
        )

    result = await seat.compact.execute(
        request.session,
        profile,
        ContextState(
            session=request.session,
            window_used=usage.context_window_used,
            estimated_tokens=usage.input_tokens + usage.output_tokens,
            context_window=seat.context_window,
            message_count=0,
            passes_so_far=0,
        ),
    )
    return result is not None and result.made_progress


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
    # t-f2-10, and it is FIRST on purpose: a message that joined this turn's window is
    # part of THIS turn's input and has to be folded in before `StartTurn` persists the
    # request, or the audit row records a question the user did not ask.
    #
    # Rebinding `request` is deterministic: the combined value is a STEP result, so a
    # replay reads it back out of the operation log rather than draining again.
    request = await _step_drain_pending(request)

    turn_id = await _step_new_turn_id()
    outcome = await _step_start(turn_id, request)

    rounds = 0
    while outcome.is_suspended:
        rounds += 1
        if rounds > MAX_HUMAN_ROUNDS:
            return await _step_abandon(turn_id, _TOO_MANY_ROUNDS)

        # Sorted before dispatch, never in the order the runner happened to produce
        # (CLAUDE.md non-negotiable #7), and then narrowed to the requests a PERSON can
        # actually resolve. A peer ask is answered by another agent's turn on this same
        # durable topic, so publishing it asks somebody a question they cannot answer and
        # spends the three days below waiting on them for it.
        #
        # Filtered here rather than inside the step: which requests a human is asked is a
        # decision the body makes, so it is recorded in the step's arguments and replays
        # identically. Inside the step it would be re-derived on every retry, and the day
        # the rule changes a recovered turn would publish a different set than the one that
        # crashed.
        #
        # The publish is UNCONDITIONAL even when nothing survives the filter, so the body
        # takes one path whatever the outcome carries. `_step_publish` returns on an empty
        # tuple; a branch here would be a second decision to keep deterministic for no gain.
        await _step_publish(
            turn_id, request, _for_a_human(_in_deterministic_order(outcome.pending))
        )

        # The durable wait, entered whether or not anybody was asked: a peer ask suspends
        # the turn exactly as an approval does and its answer arrives on this same topic.
        # Explicitly THREE_DAYS - the 60-second default is the trap.
        answer = await DBOS.recv_async(RESUME_TOPIC, timeout_seconds=THREE_DAYS)
        if answer is None:
            return await _step_expire(turn_id)

        outcome = await _step_resume(turn_id, request, answer)

    # t-f3-07. The loop above exits only on a FINISHED outcome, and I2 makes `result`
    # non-None for exactly that shape - `TurnOutcome.__post_init__` refuses any other. The
    # local is what mypy narrows on; asserting instead would put a check in the workflow
    # body for an invariant the domain type already enforces at construction.
    #
    # The two early returns above - `_step_abandon` and `_step_expire` - deliberately do
    # NOT come through here. They are the turn giving up, not the turn answering, and
    # telling the human on the channel is a product decision about what an abandoned turn
    # says, which belongs with F3's message copy rather than with this anchor.
    result = outcome.result
    if result is not None:
        await _step_deliver(request, result)

        # t-f5-08, R5. AFTER delivery, so a compaction pass never delays the answer the
        # human is waiting for - the summariser call on rungs L3 and L4 is not a fast one.
        #
        # ONE call site, and deliberately not inside the loop above: `CompactContext`
        # refuses to retry a pass that freed nothing, and a second entry would turn that
        # refusal back into the infinite loop it exists to prevent. The trigger is asked
        # again on the NEXT turn, which is what "compact rarely and in large steps" means.
        #
        # The turn's own usage is passed rather than read: an argument is recorded in the
        # durable log, so a replay compacts against the same numbers the first attempt saw
        # (CLAUDE.md non-negotiable #2). The two abandoned paths above return before this
        # line on purpose - a turn nobody answered produced no usage worth triggering on.
        await _step_compact(request, result.usage)

    return outcome


# What a human can put on the durable wire in answer to a deferred call.
# docs/TASKS.md#t-f7-11.
#
# THREE MEMBERS BECAUSE THERE ARE THREE ANSWERS, NOT BECAUSE A UNION IS CONVENIENT
#   `str`      - a refusal's reason, or a note the model reads as the tool's result.
#   `MediaRef` - an upload. D9 made approval and evidence ONE flow, so the file a person
#                sent arrives on the same topic an approval does; `ResumeTurn` recognises
#                it BY TYPE (application/resume_turn.py `_resolve`) and turns it into
#                bytes or a signed URL. A reference and never the bytes: this value is
#                pickled into the system database, and putting a 48MB photo in a workflow
#                message stores the photo twice and reads it back on every replay.
#   `None`     - a bare approval, which is what tells the runner to execute the deferred
#                call itself rather than answer it (ports/agent_runner.py).
#
# It was `str | None`, and that was the defect docs/TASKS.md#t-f7-11 closed: F7's own
# acceptance test could only put an upload on this wire through a `cast`, so no
# type-checked production caller could - which is why `POST /evidence/{corr_id}` stored a
# file and woke nobody. A carrier that will not admit what the feature has to carry is a
# feature nobody can finish wiring.
HumanPayload = str | MediaRef | None


@dataclass(frozen=True, slots=True)
class HumanAnswer:
    """What a human's decision looks like on the durable wire. docs/TASKS.md#t-f3-11.

    A TYPE AND NOT A DICT, for the reason `_step_resume` refused to guess a shape: the
    payload is written by `POST /decisions/{corr_id}` in one process and read back inside
    a workflow that may be running in another, days later, after a redeploy. A dict whose
    keys drifted between the two sides would not raise - it would hand the runner a
    resolution for a `tool_call_id` the model never issued, Pydantic AI would drop it
    silently, and the agent would ask the same question forever.

    IT CARRIES ITS OWN `turn_id` even though `_step_resume` already has one. That is not
    redundancy, it is the check: `DBOS.recv_async` returns whatever was sent to this
    workflow, and a mismatch means an answer was routed into the wrong turn - the exact
    approval-without-an-approver the correlation table exists to prevent. A reader that
    cannot tell is a reader that will apply it.

    IT IS PICKLED, AND docs/TASKS.md#t-f3-18 IS THE WARNING THIS TYPE HAS TO HEED
        `UnknownChannelError` crossed this same boundary and arrived unusable, because
        `BaseException.__reduce__` yields only `args` and the second constructor argument
        was gone. A frozen `slots=True` dataclass has its own version of that trap - it
        pickles through `__getstate__`/`__setstate__` rather than a `__dict__` - so the
        property worth asserting about anything on this wire is that the VALUE comes back,
        field for field, and never that the far side received the right shape.
    """

    turn_id: TurnId
    tool_call_id: ToolCallId
    approved: bool
    note: HumanPayload = None


async def signal_decision(
    turn_id: TurnId, tool_call_id: ToolCallId, approved: bool, note: str | None
) -> None:
    """Wake the turn waiting on a human. `DecideApproval`'s `DecisionSignal` seat.

    docs/TASKS.md#t-f3-11. This is the SEND paired with `run_turn_workflow`'s
    `DBOS.recv_async`, and it lives here because `dbos` is banned outside this package
    (pyproject.toml's TID251, the mechanical form of CLAUDE.md non-negotiable #1) - so
    `composition.py` binds this function rather than importing DBOS to build it.

    `destination_id` IS THE DOMAIN TURN ID, and only docs/TASKS.md#t-f0-06 made that true.
    Before it, the id `DecideApproval` holds and the id the workflow runs under were two
    different values, so this send addressed a workflow nothing had ever created. DBOS
    does not refuse that: the message is stored for an id that will never receive it. The
    audit row lands, the caller is told the decision was accepted, and the turn sleeps
    until it expires three days later.

    IDEMPOTENCE IS DBOS'S, NOT OURS - docs/FIELD-NOTES.md
        Humans double-click and retry logic re-posts, and resolving one request twice must
        not resume the turn twice. `send` takes an `idempotency_key`, so the second send
        with the same key is a no-op decided by the same database the wait lives in. A
        cache here would be a third copy of that rule, in memory, in one process - a lie
        the moment there are two.

        The key is (turn_id, tool_call_id) and not the correlation id: the correlation is
        the HANDLE a human was given, and a turn may be re-published a new handle for the
        same pending call after a crash (`_step_publish` is retried). Keying on the handle
        would let the same call be resolved twice under two handles; keying on the call
        cannot.

    It never waits for the turn. There is no result to await here - which is what lets
    `POST /decisions/{corr_id}` answer immediately, as t-f0-03 requires of every route.
    """
    await DBOS.send_async(
        str(turn_id),
        HumanAnswer(
            turn_id=turn_id, tool_call_id=tool_call_id, approved=approved, note=note
        ),
        RESUME_TOPIC,
        idempotency_key=f"{turn_id}:{tool_call_id}",
    )


async def signal_evidence(
    turn_id: TurnId, tool_call_id: ToolCallId, media: MediaRef
) -> None:
    """Wake the turn waiting for a file with the file. docs/TASKS.md#t-f7-11.

    The send `POST /evidence/{corr_id}` is bound to, and the evidence twin of
    `signal_decision` above. It lives here for the same reason that one does: `dbos` is
    banned outside this package (pyproject.toml's TID251, the mechanical form of
    CLAUDE.md non-negotiable #1), so composition binds this function rather than importing
    DBOS to build the send itself.

    A SEPARATE DOOR, ONE DURABLE MECHANISM - D9. Approval and evidence are one flow and
    share `RESUME_TOPIC`, `HumanAnswer` and the whole suspension machinery. What differs
    is what a person supplied, and the two doors keep that honest at the type level:
    `signal_decision` takes a yes/no and a note and cannot be handed a file;
    `signal_evidence` takes the stored reference and cannot be handed a yes/no. One
    function with an optional everything would let a caller send an approval carrying a
    photo, which `_step_resume` would then pass on as the tool's result.

    `approved=True` IS NOT A DECISION ANYBODY MADE, and it must not be read as one. A
    person who was asked for a photo and sent one has answered the request; there was no
    yes/no to give. The approval half of `ResolvedTool` is what tells the runner whether
    to EXECUTE the deferred call, and an evidence call carries its own result - the
    payload below - so it is answered rather than executed. Who decided what, and when, is
    recorded by `DecideApproval` through `AuditSink.record_human_decision` on the approval
    path only; nothing here writes an approval into the audit trail.

    IDEMPOTENCE IS DBOS'S, AND THE KEY IS THE SAME ONE - docs/FIELD-NOTES.md
        `(turn_id, tool_call_id)`, exactly as `signal_decision` keys it, and deliberately
        not the correlation handle: a turn may be re-published a new handle for the same
        pending call after a crash (`_step_publish` is retried), so keying on the handle
        would let one deferred call be resolved twice under two handles. Sharing the key
        with the approval door is the point rather than a coincidence - one deferred call
        is resolved once, whichever door the answer came through.

        People re-send an upload when the first attempt looks slow, and a browser retry
        posts the same file again. The second send with the same key is discarded by the
        same database the wait lives in; a cache in this process would be a third copy of
        that rule and a lie the moment there are two processes.

    It never waits for the turn, so the route can answer immediately - the rule every
    route in this system obeys (t-f0-03).
    """
    await DBOS.send_async(
        str(turn_id),
        HumanAnswer(
            turn_id=turn_id, tool_call_id=tool_call_id, approved=True, note=media
        ),
        RESUME_TOPIC,
        idempotency_key=f"{turn_id}:{tool_call_id}",
    )


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


async def enqueue_turn(
    request: TurnRequest, *, turn_id: TurnId | None = None
) -> WorkflowHandleAsync[TurnOutcome]:
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

    `turn_id` PINS THE WORKFLOW'S DURABLE ID - docs/TASKS.md#t-f0-06
        Pass it when the id has already been given to somebody: `run_turn_window` does,
        because `POST /turns` answered a caller with it a window ago. `_step_new_turn_id`
        reads the same value back inside the body, so the id the caller polls, the id the
        audit rows carry and the id `DBOS.send_async` addresses are one value.

        Omitting it lets DBOS assign the id, and the body still reads that back - so a
        caller that started a turn WITHOUT a window (main.py's direct starter) also gets
        a handle whose `workflow_id` is the domain `TurnId`. There is no third case: the
        body never invents an id of its own.

        Re-enqueueing the same `turn_id` is not a second turn. DBOS returns the existing
        handle for a workflow id it already has, which is what makes the window workflow's
        replay after a crash idempotent.
    """
    with SetEnqueueOptions(queue_partition_key=session_partition_key(request.session)):
        if turn_id is None:
            return await turn_queue.enqueue_async(run_turn_workflow, request)
        with SetWorkflowID(str(turn_id)):
            return await turn_queue.enqueue_async(run_turn_workflow, request)


def session_window_key(session: SessionRef) -> str:
    """The coalescing window one conversation shares. Pure, so it is the same on replay.

    Deliberately the same string as the partition key, because it answers the same
    question - "which conversation is this?" - and two spellings of one conversation is
    how a session ends up coalescing on one key and serialising on another. It carries
    the tenant for the reason `session_partition_key` gives: `SessionId` is unique inside
    a tenant only, and a window keyed on the session alone would merge two strangers'
    messages into one turn, which is a disclosure rather than a latency optimisation.
    """
    return session_partition_key(session)


def window_id_for_turn(turn_id: TurnId) -> str:
    """The coalescing window that will start `turn_id`. Pure, and the inverse of below.

    WHY THE TWO IDS ARE DERIVED FROM EACH OTHER AND NOT MINTED SEPARATELY
        docs/TASKS.md#t-f0-06 needs `POST /turns` to answer with the turn's id, and at
        that moment the turn does not exist: it is a window workflow sitting DELAYED in
        the system table, and it will not enqueue the turn for another second and a half.
        So the id has to be known at the edge, before the turn exists.

        That is easy for the message that OPENED the window and impossible for the ones
        that joined it. `duplication_policy="return-existing"` hands a colliding enqueue
        the holder's handle and throws its own arguments away, so a second message's
        freshly minted id is discarded and it never learns the id of the turn it just
        joined - it would answer its caller with an id nothing will ever be filed under.

        A derivation closes that without a second query and without a race: the window id
        that comes back IS the first message's turn id behind a prefix, so every message
        that coalesced reads the same answer out of the reply the deduplication already
        gave it. Reading the surviving window's arguments back instead would be a second
        query against a moment that may already have moved.

    A prefix rather than a hash, because it is invertible and auditable: an operator
    holding either id can see the other one, and the pair in the DBOS tables reads as one
    conversation instead of two unrelated rows.
    """
    return f"{_WINDOW_ID_PREFIX}{turn_id}"


def turn_id_of_window(window_id: str) -> TurnId:
    """The turn a coalescing window starts, read back out of the window's own id.

    The exact inverse of `window_id_for_turn`, and the reason `run_turn_window` needs no
    `turn_id` argument: the window derives it from its OWN durable id, so the value can
    never disagree with the one the edge answered a caller with.
    """
    return TurnId(window_id.removeprefix(_WINDOW_ID_PREFIX))


@dataclass(frozen=True, slots=True)
class WindowEnqueue:
    """What the caller of `enqueue_turn_window` needs to know, and nothing else.

    `coalesced` is the whole reason this is not just a handle: the caller has to buffer
    the message it just sent IF AND ONLY IF that message joined a window already open.
    Buffering unconditionally would repeat the first sentence (it travels in the enqueued
    turn), and buffering nothing would delete every sentence after the first.

    `turn_id` is the id the turn behind this window will be filed under - the one a caller
    polls and every audit row carries (docs/TASKS.md#t-f0-06). It is the FIRST message's
    when this one coalesced, which is correct and is the useful answer: "your message
    joined the answer already being prepared".

    `window_id` is DBOS's own handle on the window workflow, kept distinct from `turn_id`
    on purpose. They are two workflows; naming them with one value is the defect this
    anchor removed, and re-merging them here would put it straight back.
    """

    window_id: str
    turn_id: TurnId
    coalesced: bool


@DBOS.workflow()
async def run_turn_window(request: TurnRequest) -> str:
    """One coalescing window. Waits out its delay, starts the turn, and gets out of the way.

    docs/TASKS.md#t-f2-11. Returns the enqueued turn's workflow id, so a caller holding
    the window handle can reach the turn behind it without guessing.

    THE DELAY IS NOT HERE. It is `delay_seconds` on the enqueue (see
    `enqueue_turn_window`), which leaves this workflow DELAYED in the system table until
    the window closes. A `DBOS.sleep_async` in this body would work too and would be
    strictly worse: the deduplication id is what makes a second message join this window,
    and it is held from the moment the row exists - so sleeping here would hold a
    scheduler slot for the whole window as well, for no gain.

    WHY THE CHILD ENQUEUE IS NOT WRAPPED IN A STEP
        R2 says every side effect is a step, and a child enqueue is the documented
        exception, exactly like `DBOS.recv_async` below: DBOS records the child workflow
        id against this workflow's function id, so a replay returns the SAME handle
        instead of enqueueing a second turn. Wrapping it in a step would also mean
        returning a handle from a step, and a handle is not serialisable.

    AND WHY IT DOES NOT AWAIT THE RESULT
        See the module docstring. Awaiting the turn would hold this window's
        deduplication id for the length of the turn, and D19's mid-turn message would
        then join a window whose turn has already drained the buffer - a message that
        never becomes a turn, with nothing raising.

    THE TURN'S ID IS NOT INVENTED HERE, IT IS READ OFF THIS WINDOW (t-f0-06)
        `turn_id_of_window(_current_workflow_id())` is a pure function of this workflow's
        durable id, so it is the same value on replay and the same value the edge already
        answered the caller with - including for the messages that COALESCED onto this
        window, whose own arguments were discarded by `return-existing`. Minting one here
        would hand the turn an id nobody outside this process has ever seen.
    """
    turn_id = turn_id_of_window(_current_workflow_id())
    handle = await enqueue_turn(request, turn_id=turn_id)
    return handle.workflow_id


async def enqueue_turn_window(
    request: TurnRequest, *, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> WindowEnqueue:
    """Open a coalescing window for this session, or join the one already open.

    docs/TASKS.md#t-f2-11, and the primitive docs/TASKS.md#t-f2-05 is built on.

    THREE OPTIONS, AND EACH ONE IS LATE-BINDING D19
        `delay_seconds` postpones the window workflow so later messages can join it.
        `deduplication_id` is the conversation, so only one window is open per session.
        `duplication_policy="return-existing"` makes a colliding enqueue hand back the
        waiting workflow instead of raising - the default policy is "reject", which would
        turn a user's second sentence into a 500.

        None of the three may be set on `turn_queue`: it is partitioned, and dbos 2.31.1
        refuses deduplication there. That is why this targets `window_queue`.

    HOW "DID I COALESCE?" IS ANSWERED WITHOUT A RACE
        The id the new workflow WOULD get is pinned first. `return-existing` hands back
        the holder's id instead, so an id that came back different from the pinned one
        is proof that this message joined an existing window - decided by the same
        transaction that did the deduplication, not by a second query that could observe
        a different moment.

    AND HOW THE TURN'S ID COMES BACK WITH IT - docs/TASKS.md#t-f0-06
        The window id is not a fresh value: it is `window_id_for_turn(turn_id)` for the
        turn this message would start. So the same reply that answers "did I coalesce?"
        also answers "which turn am I part of?" - `turn_id_of_window` reads it back out,
        and a message that joined an open window gets the FIRST message's turn id, which
        is the one everything downstream will be filed under. `POST /turns` can therefore
        answer 202 with an id that outlives the window, without a second query and without
        waiting for the turn to exist.

    `uuid4()` here is safe and is not a violation of CLAUDE.md non-negotiable #2: this is
    an ordinary async function called from an HTTP handler, not a workflow body. The id it
    mints is the turn's, and it survives only if this message opened the window.
    """
    candidate = window_id_for_turn(TurnId(str(uuid4())))
    with (
        SetWorkflowID(candidate),
        SetEnqueueOptions(
            deduplication_id=session_window_key(request.session),
            duplication_policy="return-existing",
            delay_seconds=window_seconds,
        ),
    ):
        handle = await window_queue.enqueue_async(run_turn_window, request)

    return WindowEnqueue(
        window_id=handle.workflow_id,
        turn_id=turn_id_of_window(handle.workflow_id),
        coalesced=handle.workflow_id != candidate,
    )
