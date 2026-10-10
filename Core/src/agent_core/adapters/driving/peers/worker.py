"""Driving adapter: the peer worker - the consumer `PgAgentMailbox` never had.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-43
Status:  IMPLEMENTED - claim, run the turn AS THE TARGET AGENT, answer
Tests:   Core/tests/integration/test_peer_worker.py
Consumes: adapters/driven/peers/mailbox.py (the answering half of the queue)

WHY THIS IS A DRIVING ADAPTER, AND WHOSE SIBLING IT IS
    It claims work that arrived from OUTSIDE this process and calls a use case with it.
    That is the same shape as `adapters/driving/scheduler/cron.py`, and this file is
    deliberately written in that style rather than a second one: a cron firing and a peer's
    question are both "somebody who is not a human in a chat window wants a turn run".

    CLAUDE.md non-negotiable #1 is why there is no `@DBOS.workflow()` anywhere below. The
    durable machinery belongs to `adapters/driving/workflow/`; this file builds a
    `TurnRequest` and hands it to a runner seat, exactly as `cron.py` hands one to
    `TurnStarter`. That seat is a Protocol so this module stays importable, and testable,
    with neither DBOS nor Postgres running.

F9 BUILT THE ASK AND THE TRANSPORT. THIS IS THE PART NOTHING RAN.
    `PgAgentMailbox.claim_next` is a single `UPDATE ... FOR UPDATE SKIP LOCKED` with a
    concurrent-claim test behind it - eight workers claiming at once - and until this file
    existed it had no caller at all. An ask was enqueued durably, correlated, hop-counted
    and then waited forever, because nothing ran the answering agent's turn.

THE TURN RUNS AS THE TARGET AGENT. THIS IS THE SECURITY CLAIM OF THE WHOLE FILE.
    `profile_id` is `ask.target`: B's persona, B's toolsets, B's approval rules, B's
    `max_cost_usd`. Running it under the ASKING agent's identity would make delegation a
    privilege escalation dressed as a question - A asks B, B holds tools A was never
    granted, and if the turn ran as A then A has just used them. It would also be
    invisible: the audit row would name A, the policy verdicts would be A's, and every
    line of the trail would look correct.

    The two-sided allowlist (`adapters/driven/peers/hop_limit.py`) exists to prevent
    exactly that, and it decides WHICH peer may be asked. It does not decide WHO the turn
    then runs as; this file does, and it is the only place that can.

    The caller is a `CallerIdentity` and never an `AdminIdentity` - CLAUDE.md
    non-negotiable #9, and the same reading `cron.py` gives: an answering agent is a caller
    with no human behind it, not an administrator. There is no parameter below an
    `AdminIdentity` could arrive through, which is the structural half of that rule.

    `PEER_SUBJECT_ID` is a constant for `cron.py`'s reason: a peer turn never borrows a
    human's `subject_id`, so a deployment writes policy rows for the `peer` channel once
    and every ask arriving from another agent is judged by them. It is deliberately NOT the
    asking agent's id - see WHAT THIS FILE CANNOT ATTRIBUTE below.

    The TENANT is the asking session's, never a default and never widened. A question
    crossing a tenant boundary would let one deployment's customer data answer another's,
    and the queue row is the only thing that knows which tenant the conversation belongs
    to.

A FRESH SESSION PER ANSWERED ASK
    `cron.py`'s reason, and it is sharper here: inheriting the ASKING conversation would
    file B's turn inside A's customer's history, and reusing one long-lived peer session
    would let one customer's history answer another customer's question - the agent would
    read the previous ask as context and might answer from it. Nothing below ever reads an
    existing `SessionId`, so there is no path by which either could happen.

THE HOP TRAVELS WITH THE ASK
    `PeerAsk.hop` is the count carried on the row, and `hop_limit.authorise_hop` needs it
    to catch A -> B -> A: to B, an ask from A is the first ask B has seen, so only a number
    carried on the message tells "A started a conversation" apart from "A is asking back
    the agent that just asked it". A worker that dropped the count would reset the cycle at
    every hop, and every hop is a full turn with model calls on both sides - so the limit
    would never fire and the first thing anybody noticed would be the bill.

    So `hop` travels TWICE, and neither copy is redundant (docs/TASKS.md#t-f11-47):
    `peer_turn_request` puts it on the `TurnRequest`, which is what the gate reads back out
    three layers away in `turn_workflow._step_ask_peers`; and it stays on the runner seat,
    which is what makes a binding that starts a DIFFERENT kind of turn - a durable
    workflow, an HTTP call to another process - state the depth explicitly rather than
    inherit whatever the request it was handed happened to carry. `DirectTurnRunner`
    reconciles the two by applying the seat's count to the request it forwards, so the
    number the caller passed is the number the gate sees.

WHAT HAPPENS TO A CLAIM WHOSE TURN SUSPENDS - READ THIS BEFORE CHANGING ANYTHING
    B may itself suspend: to ask its own human (the shipped `billing_specialist` needs an
    approval before it freezes an account) or to ask a third agent. That is the ordinary
    case, not a failure, and it is precisely why `ask` and `answer` are separated by a
    durable queue instead of by a function call.

    When the turn comes back suspended this worker records NOTHING and returns. The row
    stays `delivered` with a null `answer`, the asking turn stays suspended on its
    correlation id, and whoever resumes B's turn is what eventually calls
    `AgentMailbox.answer()` (docs/TASKS.md#t-f11-44). Two things this must never do
    instead: answer with the suspension notice, which would resume A's turn with a sentence
    no peer ever said; or hold the claim open and wait, which would pin a worker for as
    long as B's human takes and lose the ask entirely on the next deploy.

WHAT HAPPENS WHEN THE WORKER DIES MID-TURN
    The claim is already `delivered`. `claim_next` is exactly-once and says in its own
    docstring that recovering a crashed claim is deliberately not built, because a
    redelivery policy is a decision about duplicate model spend. So the ask is answered by
    nobody: A waits until its `PeerPolicy.reply_timeout_seconds` expires. That is a real
    gap and it is named here rather than hidden - the remedy is a redelivery anchor that
    owns the retry story, not a `try/except` in this file that re-queues an ask whose model
    calls may already have been paid for.

WHAT THIS FILE CANNOT ATTRIBUTE, NAMED RATHER THAN GUESSED
    `PeerAsk` carries the target, the question, the asking session, the turn and the hop -
    and NOT the asking agent's `AgentId`. So this worker cannot name the peer that asked,
    which means two things it would otherwise do are impossible here: run
    `authorise_hop`'s callee-side check (`callee_policy.may_ask(caller)`) a second time on
    arrival, and write per-asker policy rows. Inventing an id from the session would be a
    guess wearing an identity's clothes. The asking side's gate still applies; this is a
    defence in depth that is missing, not the gate.

WHAT THIS FILE DOES NOT DECIDE
    Whether the question was allowed to become a hop - that is `hop_limit.authorise_hop`,
    on the asking side, before the row was ever written. Whether the answer may be believed
    - a peer's reply is untrusted content whatever this worker did with it (CLAUDE.md
    non-negotiable #10), and `mailbox.read_answer` wraps it on the way back out. This
    worker writes the peer's bytes VERBATIM for that reason: wrapping on write would
    double-wrap, and a model shown a nested boundary cannot tell which one is real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import uuid4

from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.domain.peers import AgentId
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    UserInput,
)

__all__ = [
    "PEER_CHANNEL",
    "PEER_ROLE",
    "PEER_SUBJECT_ID",
    "AskDisposition",
    "DirectTurnRunner",
    "PeerAnswerSignal",
    "PeerQueue",
    "PeerTurnReport",
    "PeerTurnRunner",
    "TurnRunner",
    "answer_next_ask",
    "answerable_targets",
    "build_peer_identity",
    "new_peer_session",
    "new_turn_id",
    "peer_turn_request",
    "run_peer_worker",
]

# WHO an agent-to-agent turn is FROM. Constants, never parameters: a call site that could
# pass a subject would be a call site that could run a peer's turn under a human's
# permissions, and CLAUDE.md non-negotiable #9 cuts both ways - a chat client is never
# widened into an administrator, and an agent never borrows a person's identity either.
PEER_CHANNEL: Final[str] = "peer"
PEER_SUBJECT_ID: Final[str] = "peer-agent"
PEER_ROLE: Final[str] = "peer"


# THE RETURN LEG, AS A SEAT RATHER THAN AS AN IMPORT. t-f11-43 records the answer;
# `docs/TASKS.md#t-f11-44` owns WAKING the turn that is waiting on it, and it lives in
# `adapters/driving/workflow/turn_workflow.py` because `DBOS.send()` is the only thing that
# can reach a suspended workflow - and `dbos` may not be imported outside that package.
#
# `(turn_id, correlation_id)`: the id of the ASKING turn, which is the only handle that
# finds the suspended workflow, and the correlation id it redeems through `read_answer`.
# Both travel on the claimed row (`PeerAsk`) precisely so the answer can find its way back.
PeerAnswerSignal = Callable[[TurnId, str], Awaitable[None]]


class AskDisposition(StrEnum):
    """What became of one claimed ask.

    ANSWERED  - B finished and its answer is on the row A is waiting on.
    SUSPENDED - B is waiting on a human or another agent. Nothing was recorded; see WHAT
                HAPPENS TO A CLAIM WHOSE TURN SUSPENDS in the module docstring.

    There is deliberately no FAILED member. An exception from the turn is not a disposition
    this file gets to convert into a quiet outcome - it propagates, because an ask that was
    claimed and then silently discarded is the failure mode the durable queue exists to
    prevent, and a member here would be the place someone later wrote `pass`.
    """

    ANSWERED = "answered"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class PeerTurnReport:
    """One claimed ask and what became of it. Returned rather than logged.

    The worker loop below is what turns these into operator-visible lines; a function that
    printed would be untestable and would put the output format in the middle of the claim
    path.
    """

    correlation_id: str
    target: AgentId
    disposition: AskDisposition


@runtime_checkable
class PeerQueue(Protocol):
    """The ANSWERING side of the mailbox, which is deliberately not on the port.

    `ports/agent_mailbox.py` describes the asking side; `claim_next` lives on the adapter
    because the wire protocol decides it - D2's A2A adapter receives an HTTP task instead
    of polling a queue (`mailbox.py`, `claim_next`'s own docstring). So this worker
    declares what it consumes, the way `console.py` narrows `StartTurn` to `TurnRunner`.

    `runtime_checkable` for one caller: `main.py` holds `Container.mailbox` typed on the
    PORT and has to establish that the bound adapter can also be claimed from. Checking it
    at startup names the missing half; discovering it at the first claim would take the
    worker down a minute into a deployment with no ask to show for it.
    """

    async def claim_next(self, target: AgentId) -> PeerAsk | None: ...

    async def answer(self, correlation_id: str, answer: str) -> None: ...


class PeerTurnRunner(Protocol):
    """What this worker needs in order to run B's turn, and nothing else.

    Narrowed for `console.TurnRunner`'s reason: a worker holding the whole use case could
    reach its store or its audit sink and answer a peer from the side rather than from a
    turn.

    `hop` is a keyword parameter here as well as a field on `TurnRequest`, because the
    count is a property of the TURN rather than of the request a caller assembles - see
    THE HOP TRAVELS WITH THE ASK above. An implementation must put it where the turn it
    starts will read it back; `DirectTurnRunner` does that by rebuilding the request.
    """

    async def execute(
        self, turn_id: TurnId, request: TurnRequest, *, hop: int
    ) -> TurnOutcome: ...


class TurnRunner(Protocol):
    """`StartTurn` as this module consumes it - the same shape `console.py` narrows to."""

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome: ...


@dataclass(frozen=True, slots=True)
class DirectTurnRunner:
    """Runs the peer's turn through `StartTurn` directly, at the depth it was handed.

    IT IS DIRECT FOR THE CONSOLE'S REASON, AND CARRIES THE CONSOLE'S CAVEAT
        `StartTurn.execute` returns the outcome, which is what this worker needs in order
        to answer; `enqueue_turn` returns a handle and the answer would never come back
        here. So a turn run through this binding is NOT durable: kill the worker mid-turn
        and there is no workflow to recover it. That is the same trade `run_console` makes
        and says so in its banner, and it is why `PeerTurnRunner` is a seat rather than an
        import - a binding that starts the durable workflow and awaits its result drops in
        here without this file changing.

    THE HOP IS APPLIED TO THE REQUEST, NOT DISCARDED (docs/TASKS.md#t-f11-47)
        This binding used to accept the count and drop it, because `TurnRequest` had no
        seat for it and the honest move was to say so rather than pretend. It has one now,
        so the count is written onto the request this runner forwards and the gate reads
        it back in `turn_workflow._step_ask_peers`. Without that line an `ask_peer` B makes
        while answering A is counted as a FIRST hop rather than a second, and the cycle
        A -> B -> A never trips `max_hops` - a limit that resets at every hop is not a
        limit, and every hop is a full turn with model calls on both sides.

        `replace` rather than trusting the request as built: the seat's `hop` is the
        authority here. `answer_next_ask` passes `ask.hop` and `peer_turn_request` has
        already put the same number on the request, so this is a no-op on the worker path
        - and it stops a caller that assembled the request itself from silently running a
        peer's turn at depth zero.
    """

    start_turn: TurnRunner

    async def execute(
        self, turn_id: TurnId, request: TurnRequest, *, hop: int
    ) -> TurnOutcome:
        return await self.start_turn.execute(turn_id, replace(request, hop=hop))


def new_turn_id() -> TurnId:
    """A fresh turn id.

    NOT the non-determinism CLAUDE.md #2 forbids. That rule is about a DBOS workflow BODY,
    where a fresh id on replay forks the conversation; nothing here is a workflow body -
    which is also what `DirectTurnRunner` warns about. Injectable so a test can read back
    which id a peer turn was filed under.
    """
    return TurnId(str(uuid4()))


def new_peer_session(tenant_id: TenantId, origin: SessionRef | None = None) -> SessionRef:
    """A brand new session for one answered ask, in the ASKING side's tenant.

    Never a session id supplied by a caller and never one reused between asks - see A FRESH
    SESSION PER ANSWERED ASK above. The `peer-` prefix is for whoever reads `:sessions`
    later: a conversation with no human in it is worth being able to tell apart at a
    glance.

    `origin` is the session that asked, taken from the claimed row by CODE (never from the
    question text, which the asking model wrote). When given, its id travels inside the new
    one as `peer~<origin id>~<uuid>`, so a vertical's tools can act on behalf of the asking
    conversation (Glazed: the case) without the model naming it. The history is still
    fresh - only the NAME carries the origin. Origin ids must not contain `~`.
    """
    if origin is None:
        return SessionRef(session_id=SessionId(f"peer-{uuid4()}"), tenant_id=tenant_id)
    return SessionRef(
        session_id=SessionId(f"peer~{origin.session_id}~{uuid4()}"), tenant_id=tenant_id
    )


def build_peer_identity(tenant_id: TenantId) -> CallerIdentity:
    """The identity every answered ask carries.

    A `CallerIdentity`, never an `AdminIdentity` (CLAUDE.md #9). The tenant comes from the
    asking session's row rather than from a default, so an ask can never be answered in a
    tenant it did not come from.
    """
    return CallerIdentity(
        subject_id=PEER_SUBJECT_ID,
        channel=PEER_CHANNEL,
        tenant_id=tenant_id,
        roles=frozenset({PEER_ROLE}),
    )


def peer_turn_request(ask: PeerAsk, *, session: SessionRef) -> TurnRequest:
    """The claimed ask as a turn request - AS THE TARGET AGENT, AT THE DEPTH IT ARRIVED AT.

    `profile_id=ask.target` is the line the module docstring is about. The question becomes
    the user input verbatim: whatever redaction `PeerPolicy.visibility` called for happened
    on the asking side, where the conversation being redacted actually is
    (`ports/agent_mailbox.py`, step 3), and re-deciding it here with no conversation in
    hand would be decoration.

    `hop=ask.hop` IS THE LINE docs/TASKS.md#t-f11-47 EXISTS FOR. This is the one place in
    the system that holds the queue row and the request it becomes at the same time, so
    it is the place the count crosses from the message onto the turn. A request built
    without it starts the answering agent at depth zero, `authorise_hop` is handed a 0 at
    every hop of A -> B -> A, and `max_hops` never trips however carefully the gate is
    called - see THE HOP TRAVELS WITH THE ASK above.
    """
    return TurnRequest(
        session=session,
        caller=build_peer_identity(session.tenant_id),
        profile_id=ask.target,
        input=UserInput(text=ask.question),
        hop=ask.hop,
    )


async def answer_next_ask(
    queue: PeerQueue,
    runner: PeerTurnRunner,
    *,
    target: AgentId,
    wake: PeerAnswerSignal | None = None,
    mint_turn_id: Callable[[], TurnId] = new_turn_id,
    mint_session: Callable[[TenantId], SessionRef] | None = None,
) -> PeerTurnReport | None:
    """Claim one ask for `target`, run it, answer it. `None` when the queue is empty.

    ONE ask, not a drain: the caller decides how many, and a function that emptied the
    queue would starve every other target behind whichever one happened to be busy.

    An empty queue runs NO turn. That is worth stating because the opposite - starting a
    turn on a claim that came back `None` - would bill for model calls on a question
    nobody asked.

    A SUSPENDED OUTCOME IS NOT AN ANSWER, and the module docstring says what the row looks
    like afterwards. The discriminator is `result is None` rather than `is_suspended`
    because `TurnOutcome`'s I2 invariant makes them the same question, and this way the
    type checker can see that nothing below reads a result that is not there. A suspended
    turn wakes nobody either: there is nothing to deliver, and A must keep waiting.

    `wake` IS OPTIONAL AND THAT IS NOT A CONVENIENCE. Unbound, this worker records the
    answer and the asking turn stays suspended until its `reply_timeout_seconds` - a real
    hole, named here rather than hidden, and closed by binding the seat
    (docs/TASKS.md#t-f11-44 owns the function that fills it). It is optional because the
    console path has no workflow to wake at all, and a required seat would have to be
    satisfied there by a no-op, which is the same hole with a stub in front of it.
    """
    ask = await queue.claim_next(target)
    if ask is None:
        return None

    session = (
        new_peer_session(ask.from_session.tenant_id, origin=ask.from_session)
        if mint_session is None
        else mint_session(ask.from_session.tenant_id)
    )
    outcome = await runner.execute(
        mint_turn_id(), peer_turn_request(ask, session=session), hop=ask.hop
    )

    result = outcome.result
    if result is None:
        return PeerTurnReport(
            correlation_id=ask.correlation_id,
            target=ask.target,
            disposition=AskDisposition.SUSPENDED,
        )

    # VERBATIM. The wrapping is `read_answer`'s - see WHAT THIS FILE DOES NOT DECIDE.
    # `result.media` is not carried: the queue row holds text, and an answer that silently
    # lost half of itself would be worse than one that never claimed to carry media.
    await queue.answer(ask.correlation_id, result.text)

    # RECORD, THEN WAKE - in that order, and the order is the whole of it. Waking first is
    # a race with one loser and no error: the workflow resumes, redeems its correlation id
    # through `read_answer`, finds nothing there yet, and continues with an answer nobody
    # gave. `answer` is idempotent, so a worker that dies between these two lines leaves a
    # recorded answer and an unwoken turn - recoverable - rather than the reverse.
    if wake is not None:
        await wake(ask.turn_id, ask.correlation_id)

    return PeerTurnReport(
        correlation_id=ask.correlation_id,
        target=ask.target,
        disposition=AskDisposition.ANSWERED,
    )


def answerable_targets(profiles: Mapping[str, AgentProfile]) -> tuple[AgentId, ...]:
    """Which loaded profiles this worker should drain a queue for, in a fixed order.

    `peers.enabled` is the feature switch (`domain/peers.py`), and it fails closed: a
    profile that never opted in is not polled, so a deployment does not acquire an
    answering agent by accident. Sorted for the reason `run_peer_worker` polls in order.
    """
    return tuple(
        sorted(
            AgentId(profile_id)
            for profile_id, profile in profiles.items()
            if profile.peers.enabled
        )
    )


async def _sleep(seconds: float) -> None:
    """The idle wait, as a named seam a test replaces. See `run_peer_worker`."""
    await asyncio.sleep(seconds)


def _forever() -> bool:
    return True


async def run_peer_worker(
    queue: PeerQueue,
    runner: PeerTurnRunner,
    *,
    targets: Iterable[AgentId],
    wake: PeerAnswerSignal | None = None,
    idle_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = _sleep,
    keep_going: Callable[[], bool] = _forever,
    write_line: Callable[[str], None] = print,
) -> None:
    """Drain the peer queues until told to stop. One ask per target per pass.

    THE ORDER IS SORTED AND IT IS NOT A TIDY-UP. CLAUDE.md non-negotiable #7 is about a
    workflow body, and this is not one - but the same argument survives the move: a worker
    is restarted, two of them drain one queue, and iteration order decides which target is
    served first. Sorted, a starved target is a fact somebody can reproduce rather than a
    property of whichever dict was built first.

    ONE ASK PER TARGET PER PASS, for `answer_next_ask`'s reason: draining one target to
    empty holds every other target behind it.

    THE SLEEP IS ONLY FOR AN IDLE PASS. A pass that answered something goes straight round
    again - a busy queue is drained at the speed of the turns, never at the speed of the
    poll interval. Polling at all is this side's business: `mailbox.py` forbids a sleep or
    a poll loop in the ADAPTER, where a wait would silently lose a turn on the next deploy,
    and a worker process is exactly where the waiting was supposed to move to.

    `sleep` and `keep_going` are seams so this loop can be asserted without real time
    passing. Their defaults are the production behaviour: `asyncio.sleep`, and forever.
    """
    ordered = tuple(sorted(targets))
    while keep_going():
        handled = False
        for target in ordered:
            report = await answer_next_ask(queue, runner, target=target, wake=wake)
            if report is None:
                continue
            handled = True
            write_line(
                f"[peer] {report.target} {report.correlation_id} {report.disposition.value}"
            )
        if not handled:
            await sleep(idle_seconds)
