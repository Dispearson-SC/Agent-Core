"""The hop count travels with the ask, so a cycle A -> B -> A actually trips `max_hops`.

Phase:   F12 - agent-to-agent orchestration, property 5
Tasks:   docs/TASKS.md#t-f11-47
Covers:  domain/turn.py (`TurnRequest.hop`),
         adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`),
         adapters/driving/peers/worker.py (`peer_turn_request`, `DirectTurnRunner`)

WHAT WAS ACTUALLY BROKEN, AND WHY EVERY OTHER HALF OF THE GATE WAS FINE

    Both `enabled` switches and BOTH allowlists were enforced exactly as written: an
    agent could not ask a peer neither side named. `max_hops` is the one half that
    depends on a NUMBER, and the number was pinned at zero because `TurnRequest` had no
    seat for it - so the turn a worker starts to ANSWER a peer began life looking like a
    turn a human started, and `authorise_hop` was handed a 0 at every hop of the cycle.

    A limit that resets at every hop is not a limit. The failure it allows is not an
    unauthorised call - both sides legitimately allow each other - it is an UNBOUNDED
    authorised one, and every hop is a complete turn with model calls on both sides. It
    surfaces as a bill and a hung conversation rather than as a breach, which is exactly
    why nothing red ever pointed at it.

WHY THE CYCLE IS DRIVEN THROUGH THE REAL WORKFLOW AND THE REAL WORKER FUNCTION

    The property spans two files that never call each other: the worker builds the
    answering turn's `TurnRequest` from the claimed row, and the workflow reads the count
    back out of that request when the answering agent asks somebody in turn. A test that
    constructed the request by hand would assert the workflow's half and let the worker
    keep dropping the count - which is the state this anchor found. So the request below
    is built by `worker.peer_turn_request`, the production function, from a `PeerAsk`
    exactly as `claim_next` hands one over.

    And the ask is driven through `enqueue_turn` rather than through `_step_ask_peers`
    directly, for `test_peer_round_trip.py`'s reason: the gate runs inside a DBOS step,
    and calling the step as a function would replace the mechanism whose replay behaviour
    is the point.

THE SECOND TEST IS NOT A DUPLICATE - IT IS WHAT STOPS THE CHEAP FIX

    Refusing every ask would make the first assertion pass. A turn a human started is at
    depth zero and must still be allowed to ask once, carrying 1 so the NEXT hop can be
    counted; `test_a_human_started_turn_is_at_depth_zero_and_its_ask_travels_at_one`
    pins that, and it is also the behavioural statement of `TurnRequest.hop`'s default.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any, cast

import psycopg
import pytest

from agent_core.adapters.driven.peers.mailbox import PeerAsk, wrap_peer_answer
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.peers import worker
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)

# The two shipped profiles that are a real peer pair (Core/profiles/). Each names the
# other, so nothing below is refused by an allowlist - the DEPTH is the only thing under
# test, and a refusal for any other reason would be measuring the wrong half.
_ASKER = "support_triage"
_PEER = "billing_specialist"

_TENANT = TenantId("t-f11")

# The provider's id for the deferred call B makes while answering A. Ugly on purpose, for
# the reason test_peer_round_trip.py gives: a strip, a fold or a fresh uuid changes it.
_TOOL_CALL_ID = ToolCallId("call_Hop-2_ZQ")

_A_QUESTION = "Was order 55021 charged twice in March?"
_B_QUESTION = "What did the customer say the charge was for?"

# A's turn, as the queue row records it. `hop=1` is what `authorise_hop` handed back when
# A (at depth zero) was allowed to ask B, and it is what `mailbox.ask` persisted - see
# THE COUNT HAS TO TRAVEL in adapters/driven/peers/hop_limit.py.
_HOP_A_ASKED_B_AT = 1

_ASKING_TURN_ID = TurnId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_ASKING_SESSION = SessionRef(session_id=SessionId("s-a-side"), tenant_id=_TENANT)

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

# Its own DBOS system database: a failed run here is destroyed without touching state
# another file owns.
_DBOS_DATABASE = "agent_core_peer_hop_depth_test"

_RUN_DEADLINE_SECONDS = 60.0
_POLL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingChannel:
    """A `Channel` by shape alone (t-f3-13). Delivery has to succeed or the body never
    finishes the turn the assertions read."""

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _RecordingMailbox:
    """An `AgentMailbox` that records the hop every ask left with.

    It is deliberately NOT a gate: `ports/agent_mailbox.py` says so, and if this double
    refused anything the assertions below would measure the double instead of
    `hop_limit.authorise_hop` at its one production call site.
    """

    def __init__(self) -> None:
        self.asks: list[dict[str, Any]] = []
        self._answers: dict[str, str] = {}
        self._next = 0

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        return policy.peers

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        # t-f11-50. The port's own default, restated rather than chosen: `None` means the
        # asker is UNKNOWN, never "anyone may ask". A double that invented an identity
        # here would let the answering side's `callee_policy.may_ask(caller)` pass in a
        # test and refuse in production. Recorded like every other argument, because what
        # left is what this double exists to report.
        asker: AgentId | None = None,
    ) -> str:
        self._next += 1
        correlation_id = f"corr-{self._next}"
        self.asks.append(
            {
                "policy": policy,
                "target": target,
                "question": question,
                "from_session": from_session,
                "turn_id": turn_id,
                "hop": hop,
                "asker": asker,
                "correlation_id": correlation_id,
            }
        )
        return correlation_id

    async def answer(self, correlation_id: str, answer: str) -> None:
        self._answers.setdefault(correlation_id, answer)

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self._answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class _SuspendsOnAPeerAsk:
    """Stands in for `StartTurn`: one turn suspended on a single DELEGATION request.

    The arguments are the model's OWN tool-call arguments, which is what
    `adapters/driven/agent_pydantic/runner.py::_pending_requests` puts there.
    """

    def __init__(self, *, target: str, question: str) -> None:
        self.seen: list[TurnRequest] = []
        self._target = target
        self._question = question

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.seen.append(request)
        return TurnOutcome(
            turn_id=turn_id,
            pending=(
                PendingRequest(
                    kind=PendingKind.DELEGATION,
                    tool_call_id=_TOOL_CALL_ID,
                    tool_name="ask_peer",
                    arguments={"target": self._target, "question": self._question},
                    reason=f"Asked {self._target}: {self._question}",
                ),
            ),
        )


class _RecordingResume:
    """Stands in for `ResumeTurn`, recording the resolutions the workflow built."""

    def __init__(self) -> None:
        self.resolutions: list[Any] = []

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[Any, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        self.resolutions.extend(resolutions)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="Understood."))


class _RecordingRunner:
    """A `TurnRunner` for `DirectTurnRunner` to forward to - it keeps what it was handed."""

    def __init__(self) -> None:
        self.seen: list[TurnRequest] = []

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.seen.append(request)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="done"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _profiles() -> dict[str, AgentProfile]:
    """Both sides name each other, and both cap the depth at one hop.

    `max_hops=1` means: a turn a human started may ask one peer, and that peer may not
    ask anybody back. The stricter of the two sides owns the limit
    (adapters/driven/peers/hop_limit.py), and here they agree.
    """
    return {
        _ASKER: AgentProfile(
            id=_ASKER,
            persona="You are first-line support.",
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(AgentRef(agent_id=AgentId(_PEER), display_name="Billing"),),
                max_hops=1,
            ),
        ),
        _PEER: AgentProfile(
            id=_PEER,
            persona="You explain charges.",
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(AgentRef(agent_id=AgentId(_ASKER), display_name="Support"),),
                max_hops=1,
            ),
        ),
    }


def _claimed_ask() -> PeerAsk:
    """The row the worker claims: A's question to B, carrying the count it travelled with."""
    return PeerAsk(
        correlation_id="corr-a-asked-b",
        target=AgentId(_PEER),
        question=_A_QUESTION,
        from_session=_ASKING_SESSION,
        turn_id=_ASKING_TURN_ID,
        hop=_HOP_A_ASKED_B_AT,
    )


def _human_started_request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s-human"), tenant_id=_TENANT),
        caller=CallerIdentity(
            subject_id="u-1",
            channel="cli",
            tenant_id=_TENANT,
            roles=frozenset({"operator"}),
        ),
        profile_id=_ASKER,
        input=UserInput(text="was I charged twice"),
    )


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_turn: _SuspendsOnAPeerAsk,
    resume: _RecordingResume,
    mailbox: _RecordingMailbox,
    channels: ChannelRegistry,
) -> None:
    """Wire the workflow's collaborators for one test, and unwire them afterwards.

    `monkeypatch` rather than `bind_dependencies`, for test_delivery.py's reason: the
    binding is process-wide by design and that is what makes it leak into the next test.
    """
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", start_turn),
            channels=channels,
            resume_turn=cast("Any", resume),
            peers=turn_workflow.PeerSeat(
                mailbox=cast("Any", mailbox), profiles=_profiles()
            ),
        ),
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
    """Destroy this file's DBOS state before launching.

    One workflow left PENDING by a killed run occupies its session's only partition slot
    forever, so every later turn for that session is enqueued and never dequeued - with
    nothing raising.
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


async def _run_one_turn(
    request: TurnRequest, turn_id: TurnId, mailbox: _RecordingMailbox
) -> None:
    """Run one turn and stop at the first thing worth asserting on.

    THE RACE IS THE POINT, AND WITHOUT IT THIS FILE ASSERTS NOTHING
        The two outcomes are not symmetric. A refused ask has no answer coming, so the
        workflow resolves the deferred call and FINISHES. An ask that leaked past the
        gate leaves the turn parked on `DBOS.recv_async` for THREE DAYS waiting for a
        peer that no worker is running - so waiting for the result would surface the
        defect as a timeout in the harness rather than as the assertion that names it.

        So this waits for whichever comes first: the turn finishing, or a question
        reaching the queue. Both are decided facts, and the assertions read them.
    """
    async with _Dbos("agent-core-peer-hop-depth"):
        handle = await turn_workflow.enqueue_turn(request, turn_id=turn_id)
        finished = asyncio.ensure_future(handle.get_result())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RUN_DEADLINE_SECONDS
        while not finished.done() and not mailbox.asks and loop.time() < deadline:
            await asyncio.sleep(_POLL_SECONDS)

        if finished.done():
            await finished  # A workflow error is raised here, never swallowed.
            return
        finished.cancel()
        with suppress(asyncio.CancelledError):
            await finished
        if not mailbox.asks:
            raise AssertionError(
                f"the turn neither finished nor asked anybody in "
                f"{_RUN_DEADLINE_SECONDS:.0f}s, so nothing below is measuring the hop "
                "limit. Check that the peer seat is bound and the deferred call is a "
                "DELEGATION."
            )


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


@pytest.mark.phase("F11")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_turn_answering_a_peer_carries_the_hop_and_the_cycle_trips_max_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/TASKS.md#t-f11-47 - A -> B -> A must be refused, not counted from zero again.

    The turn below is the one a worker starts to ANSWER A, built by the production
    function from the claimed row. Its profile is B's, both profiles name each other, and
    `max_hops` is 1 on both sides - so the ONLY thing that can refuse B's ask back to A is
    the depth the answering turn inherited from the queue row.
    """
    mailbox = _RecordingMailbox()
    resume = _RecordingResume()
    channel = _RecordingChannel()
    _bind(
        monkeypatch,
        start_turn=_SuspendsOnAPeerAsk(target=_ASKER, question=_B_QUESTION),
        resume=resume,
        mailbox=mailbox,
        channels=ChannelRegistry(((worker.PEER_CHANNEL, channel),)),
    )

    ask = _claimed_ask()
    answering = worker.peer_turn_request(
        ask, session=worker.new_peer_session(ask.from_session.tenant_id)
    )

    asyncio.run(
        _run_one_turn(answering, TurnId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), mailbox)
    )

    assert mailbox.asks == [], (
        f"B asked A back and the question reached the queue carrying "
        f"hop={[a['hop'] for a in mailbox.asks]}. A's ask travelled at "
        f"hop={_HOP_A_ASKED_B_AT} and both profiles cap max_hops at 1, so the answering "
        "turn had already spent the only hop there was. The count is turn-level state "
        "and it has to travel with the ask: a cycle that starts counting from zero on "
        "the answering side is a limit that never fires, and every hop is a full turn "
        "with model calls on both sides (adapters/driven/peers/hop_limit.py, THE COUNT "
        "HAS TO TRAVEL). docs/TASKS.md#t-f11-47"
    )

    assert len(resume.resolutions) == 1, (
        f"the refused ask left {len(resume.resolutions)} resolutions, so the deferred "
        "call was never answered and the turn parked on an answer that is not coming."
    )
    resolution = resume.resolutions[0]
    assert resolution.tool_call_id == _TOOL_CALL_ID, (
        "a refusal is resolved under the PROVIDER's tool_call_id like any other result. "
        "CLAUDE.md non-negotiable #5."
    )
    assert "hop_limit_reached" in str(resolution.payload), (
        f"the model was told {resolution.payload!r}. The refusal reasons are distinct on "
        "purpose: a depth refusal is fixed by raising max_hops or by breaking the cycle, "
        "and an allowlist refusal is fixed by editing a profile - two different actions "
        "for two different people (adapters/driven/peers/hop_limit.py)."
    )


@pytest.mark.phase("F11")
@pytest.mark.silent
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_human_started_turn_is_at_depth_zero_and_its_ask_travels_at_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is a statement, not a convenience: a turn nobody delegated is at zero.

    This is what stops the cheap fix. Refusing every ask would satisfy the cycle test
    above and break delegation entirely; a turn a human started must still be allowed its
    one hop, and the ask must leave carrying 1 so the answering side can count from
    there.
    """
    mailbox = _RecordingMailbox()
    channel = _RecordingChannel()
    _bind(
        monkeypatch,
        start_turn=_SuspendsOnAPeerAsk(target=_PEER, question=_A_QUESTION),
        resume=_RecordingResume(),
        mailbox=mailbox,
        channels=ChannelRegistry((("cli", channel),)),
    )

    asyncio.run(
        _run_one_turn(
            _human_started_request(),
            TurnId("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            mailbox,
        )
    )

    assert len(mailbox.asks) == 1, (
        f"a turn a human started made {len(mailbox.asks)} asks. It is at depth zero and "
        "max_hops is 1, so its one ask must be allowed - a hop limit that refuses the "
        "first hop has not been enforced, it has been broken."
    )
    assert mailbox.asks[0]["hop"] == 1, (
        f"the ask left carrying hop={mailbox.asks[0]['hop']}. An allowed decision hands "
        "back `next_hop`, and that is the number the answering side counts from; an ask "
        "that travelled at 0 would let the cycle run forever however carefully the gate "
        "was called."
    )


# ---------------------------------------------------------------------------
# The binding that used to drop the count
# ---------------------------------------------------------------------------


@pytest.mark.phase("F11")
def test_the_direct_runner_threads_the_hop_into_the_request_it_forwards() -> None:
    """`DirectTurnRunner` accepted `hop` and discarded it, because there was nowhere to put it.

    There is now. Every binding of `PeerTurnRunner` hands the count to whoever runs the
    turn, and a binding that dropped it would put the defect back in one place while the
    workflow read a zero out of the request - which is precisely the shape this anchor
    found. Asserted on the FORWARDED request rather than on the seat, because the seat
    was never the thing that was broken.
    """
    inner = _RecordingRunner()
    runner = worker.DirectTurnRunner(start_turn=inner)

    asyncio.run(
        runner.execute(
            TurnId("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            _human_started_request(),
            hop=_HOP_A_ASKED_B_AT,
        )
    )

    assert len(inner.seen) == 1, "the runner forwarded no turn at all"
    forwarded = getattr(inner.seen[0], "hop", None)
    assert forwarded == _HOP_A_ASKED_B_AT, (
        f"the forwarded request carries hop={forwarded!r}. `TurnRequest` is the shape "
        "every driving adapter builds, and the depth is turn-level state that has to "
        "reach `_step_ask_peers` - a binding that drops it hands the gate a zero at every "
        "hop of a cycle. docs/TASKS.md#t-f11-47"
    )
