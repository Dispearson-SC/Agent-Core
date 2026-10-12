"""The peer's turn is a durable workflow that answers the asker (C1, D29), on a REAL DBOS.

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driving/peers/worker.py (`dispatch_next_ask`), adapters/driving/workflow/
         turn_workflow.py (`run_peer_turn_workflow`, `DurablePeerTurnStarter`)

WHAT IS BEING DEFENDED

    1. A CALLEE THAT SUSPENDS FOR A HUMAN STILL ANSWERS. Before, the worker ran the callee's turn
       itself; a turn that suspended (the shipped `billing_specialist` needs an approval) recorded
       nothing and nobody ever answered the asker. Here the callee suspends on an approval, the
       approval is delivered to the callee's turn id, and the asker's turn resumes with the
       callee's answer.

    2. A CRASH AFTER THE CLAIM LOSES NOTHING AND ANSWERS ONCE. The engine is destroyed while the
       callee waits, relaunched, and the worker claims the same row again. The deterministic
       workflow id (`peer-<correlation id>`) makes the second start a no-op: the callee's turn
       ran once, its approval was published once, and the row holds exactly one answer.

The wiring is the production one (`dbos_config`, the real `PeerSeat`, the real worker function);
only the model-facing seats (`StartTurn`, `ResumeTurn`), the human gateway and the mailbox
storage are in-memory doubles, so the test measures the workflow and not Postgres.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.peers.mailbox import PeerAsk, wrap_peer_answer
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.adapters.driving.peers.worker import AskDisposition, dispatch_next_ask
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId, PeerAnswerRefusedError, PeerPolicy, peer_ask_id
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionRef,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)
from tests.fakes import fanout, peer_envelope

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DATABASE = "agent_core_peer_turn_workflow_test"
_DEADLINE = 60.0
_POLL = 0.05

_QUESTION = "freeze the account?"
_ASK_CALL = ToolCallId("call_Ask-1")
_APPROVAL_CALL = ToolCallId("call_Approve-1")
_ASKING_TURN = TurnId("asker-turn-1")


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _conninfo(database: str = _DATABASE) -> str:
    head, _, _ = _ADMIN_CONNINFO.rpartition("/")
    return f"{head}/{database}"


def _drop() -> None:
    with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=5, autocommit=True) as admin:
        for database in (f"{_DATABASE}_dbos_sys", _DATABASE):
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


class _Dbos:
    """The production DBOS configuration, launched; `drop=False` INHERITS what a killed
    process left, exactly as the next process to start would."""

    def __init__(self, *, drop: bool = True) -> None:
        self._drop = drop

    async def __aenter__(self) -> None:
        from dbos import DBOS, DBOSConfig

        if self._drop:
            _drop()
        settings = composition.Settings(app_conninfo=_conninfo())
        DBOS(config=cast("DBOSConfig", dict(composition.dbos_config(settings))))
        DBOS.launch()

    async def __aexit__(self, *_: object) -> None:
        from dbos import DBOS

        DBOS.destroy()


# --- doubles ---------------------------------------------------------------------------------


@dataclass
class _Mailbox:
    """An in-memory mailbox with the real id derivation and the real first-answer-wins rule."""

    rows: dict[str, PeerAsk] = field(default_factory=dict)
    queued: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    answer_calls: list[tuple[str, str]] = field(default_factory=list)
    cancelled: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        if correlation_id in self.answers or correlation_id in self.cancelled:
            return False
        self.cancelled[correlation_id] = reason
        if correlation_id in self.queued:
            self.queued.remove(correlation_id)
        return True

    async def redeliveries(self, correlation_id: str) -> int:
        return 0

    async def is_cancelled(self, correlation_id: str) -> bool:
        return correlation_id in self.cancelled

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        ids = sorted(
            corr
            for corr, ask in self.rows.items()
            if str(ask.turn_id) == str(turn_id)
            and corr not in self.answers
            and corr not in self.cancelled
        )
        for corr in ids:
            await self.cancel(corr, reason)
        return tuple(ids)

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        asker: AgentId | None = None,
        request_key: str | None = None,
        continue_session: str | None = None,
        root_turn_id: str | None = None,
        background_caller: object | None = None,
    ) -> str:
        assert request_key is not None
        corr = peer_ask_id(str(turn_id), request_key, str(target), question)
        if corr not in self.rows:
            self.rows[corr] = PeerAsk(
                correlation_id=corr,
                target=target,
                question=question,
                from_session=from_session,
                turn_id=turn_id,
                hop=hop,
                asker=asker,
            )
            self.queued.append(corr)
        return corr

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        for corr in list(self.queued):
            if self.rows[corr].target == target:
                self.queued.remove(corr)
                return self.rows[corr]
        return None

    def requeue(self, corr: str) -> None:
        """What a redelivery would do to a row whose worker died after the claim."""
        self.queued.append(corr)

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        self.answer_calls.append((correlation_id, answer))
        if correlation_id in self.cancelled:
            raise PeerAnswerRefusedError(correlation_id, "the ask was cancelled")
        if correlation_id not in self.answers and failed:
            self.failed.add(correlation_id)
        self.answers.setdefault(correlation_id, answer)

    async def answer_failed(self, correlation_id: str) -> bool:
        return correlation_id in self.failed

    async def read_answer(self, correlation_id: str) -> str | None:
        raw = self.answers.get(correlation_id)
        return None if raw is None else wrap_peer_answer(raw)


class _Start:
    """`StartTurn`: the asker suspends on `ask_peer`, the callee on a human approval."""

    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.requests.append(request)
        if request.profile_id == fanout.ASKER:
            pending = PendingRequest(
                kind=PendingKind.DELEGATION,
                tool_call_id=_ASK_CALL,
                tool_name="ask_peer",
                arguments={"target": fanout.PEER, "question": _QUESTION},
                reason="asking",
            )
        else:
            pending = PendingRequest(
                kind=PendingKind.APPROVAL,
                tool_call_id=_APPROVAL_CALL,
                tool_name="freeze_account",
                arguments={},
                reason="a human must agree",
            )
        return TurnOutcome(turn_id=turn_id, pending=(pending,))

    def callee_starts(self) -> int:
        return sum(1 for request in self.requests if request.profile_id == fanout.PEER)


class _Resume:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(
        self,
        turn_id: TurnId,
        session: SessionRef,
        profile_id: str,
        resolutions: tuple[Any, ...],
        *,
        caller: CallerIdentity,
    ) -> TurnOutcome:
        self.calls.append((profile_id, resolutions))
        text = "the account is frozen" if profile_id == fanout.PEER else "all done"
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))


class _Gateway:
    def __init__(self) -> None:
        self.published: list[tuple[TurnId, tuple[PendingRequest, ...]]] = []

    async def publish(
        self,
        turn_id: TurnId,
        session: SessionRef,
        requests: tuple[PendingRequest, ...],
        owner: object | None = None,
    ) -> None:
        self.published.append((turn_id, requests))


class _Channel:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append(message)


@dataclass
class _World:
    mailbox: _Mailbox = field(default_factory=_Mailbox)
    start: _Start = field(default_factory=_Start)
    resume: _Resume = field(default_factory=_Resume)
    gateway: _Gateway = field(default_factory=_Gateway)
    channel: _Channel = field(default_factory=_Channel)


def _bind(monkeypatch: pytest.MonkeyPatch) -> _World:
    world = _World()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=cast("Any", world.start),
            channels=ChannelRegistry((("http", world.channel),)),  # no "peer" channel: on purpose
            human_gateway=cast("Any", world.gateway),
            resume_turn=cast("Any", world.resume),
            peers=turn_workflow.PeerSeat(
                mailbox=cast("Any", world.mailbox), profiles=fanout.profiles()
            ),
        ),
    )
    return world


async def _until(predicate: Any, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + _DEADLINE
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_POLL)
    raise AssertionError(f"timed out waiting for {what}")


async def _dispatch(world: _World) -> AskDisposition | None:
    report = await dispatch_next_ask(
        world.mailbox,
        turn_workflow.DurablePeerTurnStarter(),
        target=AgentId(fanout.PEER),
        wake=turn_workflow.signal_peer_answer,
    )
    return None if report is None else report.disposition


def _only_correlation_id(world: _World) -> str:
    (corr,) = world.mailbox.rows
    return corr


def _callee_turn(world: _World) -> TurnId:
    return TurnId(turn_workflow.peer_workflow_id(_only_correlation_id(world)))


async def _asker_result(deadline: float = _DEADLINE) -> TurnOutcome:
    from dbos import DBOS

    handle = await DBOS.retrieve_workflow_async(str(_ASKING_TURN))
    return cast("TurnOutcome", await asyncio.wait_for(handle.get_result(), deadline))


# --- the tests -------------------------------------------------------------------------------


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_callee_that_suspends_for_a_human_answers_the_asker_after_the_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _bind(monkeypatch)

    async def drive() -> None:
        async with _Dbos():
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")

            assert await _dispatch(world) is AskDisposition.STARTED
            callee = _callee_turn(world)
            await _until(lambda: world.gateway.published, "the callee's approval request")
            assert world.mailbox.answer_calls == [], "the callee has not finished: no answer yet"
            assert world.gateway.published[0][0] == callee, "the approval belongs to the callee"

            await turn_workflow.signal_decision(callee, _APPROVAL_CALL, True, None)
            outcome = await _asker_result()
            assert outcome.result is not None and outcome.result.text == "all done"

    asyncio.run(drive())

    ((corr, answer),) = world.mailbox.answer_calls
    assert answer == "the account is frozen"
    assert corr == next(iter(world.mailbox.rows))

    callee_requests = [r for r in world.start.requests if r.profile_id == fanout.PEER]
    (callee_request,) = callee_requests
    assert (
        callee_request.caller.subject_id == "peer-agent" and callee_request.caller.channel == "peer"
    )
    assert callee_request.caller.roles == frozenset({"peer"})
    assert callee_request.hop == 1, "the hop count travelled onto the callee's turn"
    assert callee_request.session.session_id.startswith("peer~s~")
    assert callee_request.input.text == _QUESTION

    asker_resume = [calls for profile, calls in world.resume.calls if profile == fanout.ASKER]
    ((resolution,),) = asker_resume
    assert resolution.tool_call_id == _ASK_CALL
    assert peer_envelope.contents(resolution.payload) == wrap_peer_answer("the account is frozen")
    assert len(world.channel.sent) == 1, "only the asker's turn is delivered to a channel"


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_crash_after_the_claim_and_a_second_claim_of_the_row_yield_exactly_one_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _bind(monkeypatch)

    async def first_process() -> None:
        async with _Dbos():
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")
            assert await _dispatch(world) is AskDisposition.STARTED
            await _until(lambda: world.gateway.published, "the callee's approval request")
        # leaving the block destroys the engine: the worker and everything it ran are gone

    async def second_process() -> None:
        async with _Dbos(drop=False):
            callee = _callee_turn(world)
            # a redelivery hands the SAME row to a worker again
            world.mailbox.requeue(_only_correlation_id(world))
            assert await _dispatch(world) is AskDisposition.STARTED
            await asyncio.sleep(1.5)  # let the queue and recovery settle: nothing may re-run

            await turn_workflow.signal_decision(callee, _APPROVAL_CALL, True, None)
            outcome = await _asker_result()
            assert outcome.result is not None and outcome.result.text == "all done"

            world.mailbox.requeue(_only_correlation_id(world))
            assert await _dispatch(world) is AskDisposition.STARTED  # after it is all over
            await asyncio.sleep(1.5)

    asyncio.run(first_process())
    asyncio.run(second_process())

    assert world.start.callee_starts() == 1, (
        "the callee's turn ran once across restart and re-claim"
    )
    assert len(world.gateway.published) == 1, "the callee's human was asked once"
    assert world.mailbox.answer_calls == [
        (next(iter(world.mailbox.rows)), "the account is frozen")
    ], "exactly one answer on the row"
    asker_resumes = [profile for profile, _ in world.resume.calls if profile == fanout.ASKER]
    assert asker_resumes == [fanout.ASKER], "the asker resumed exactly once"


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_durable_starter_knows_whether_a_peer_workflow_was_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1d: the worker's 'did my failed start commit?' question, against real DBOS."""
    world = _bind(monkeypatch)
    starter = turn_workflow.DurablePeerTurnStarter()
    seen: dict[str, bool] = {}

    async def drive() -> None:
        async with _Dbos():
            seen["before"] = await starter.exists("corr-that-was-never-started")
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")
            assert await _dispatch(world) is AskDisposition.STARTED
            (corr,) = world.mailbox.rows
            seen["started"] = await starter.exists(corr)

    asyncio.run(drive())

    assert seen == {"before": False, "started": True}


async def _status(workflow_id: str) -> str | None:
    from dbos import DBOS

    status = await DBOS.get_workflow_status_async(workflow_id)
    return None if status is None else status.status


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_cancelling_the_asking_turn_cancels_the_running_callee_and_no_answer_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 on real DBOS: the callee is RUNNING (suspended on its human). `cancel_turn` cancels
    the asker, marks its open ask cancelled and cancels `peer-<id>`; the late approval that
    would have finished the callee leaves no answer and wakes nobody."""
    world = _bind(monkeypatch)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")
            assert await _dispatch(world) is AskDisposition.STARTED
            callee = _callee_turn(world)
            await _until(lambda: world.gateway.published, "the callee's approval request")

            seen["cancelled"] = await turn_workflow.cancel_turn(_ASKING_TURN, "operator cancel")
            seen["asker"] = await _status(str(_ASKING_TURN))
            seen["callee"] = await _status(str(callee))

            # the callee's human approves after the fact: the workflow is gone
            await turn_workflow.signal_decision(callee, _APPROVAL_CALL, True, None)
            await asyncio.sleep(1.5)

    asyncio.run(drive())

    (corr,) = world.mailbox.rows
    assert seen["cancelled"] == (corr,)
    assert seen["asker"] == "CANCELLED" and seen["callee"] == "CANCELLED"
    assert world.mailbox.cancelled == {corr: "operator cancel"}
    assert world.mailbox.answer_calls == [], "no answer was even attempted after the cancel"
    assert world.mailbox.answers == {}
    assert [p for p, _ in world.resume.calls if p == fanout.PEER] == [], "the callee never resumed"


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_cancelling_a_turn_whose_ask_is_still_queued_stops_the_worker_from_running_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _bind(monkeypatch)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")

            seen["cancelled"] = await turn_workflow.cancel_turn(_ASKING_TURN, "operator cancel")
            seen["dispatch"] = await _dispatch(world)  # the worker finds nothing to claim

    asyncio.run(drive())

    (corr,) = world.mailbox.rows
    assert seen["cancelled"] == (corr,)
    assert seen["dispatch"] is None, "a cancelled ask is not claimed"
    assert world.start.callee_starts() == 0


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_reply_timeout_in_a_round_cancels_the_running_callee_through_the_recorded_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 on real DBOS, the in-workflow path: the asker's round times out, the recorded cancel
    step marks the row and cancels `peer-<id>` from inside a step, and the asker resumes with the
    timeout text and finishes."""
    world = _bind(monkeypatch)

    async def one_second(request: TurnRequest) -> int:
        return 1

    monkeypatch.setattr(turn_workflow, "_step_peer_reply_timeout", one_second)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with _Dbos():
            await turn_workflow.enqueue_turn(fanout.request(), turn_id=_ASKING_TURN)
            await _until(lambda: world.mailbox.queued, "the ask to reach the mailbox")
            assert await _dispatch(world) is AskDisposition.STARTED
            callee = _callee_turn(world)
            outcome = await _asker_result()
            assert outcome.result is not None and outcome.result.text == "all done"
            seen["callee"] = await _status(str(callee))

    asyncio.run(drive())

    (corr,) = world.mailbox.rows
    assert seen["callee"] == "CANCELLED"
    assert list(world.mailbox.cancelled) == [corr]
    ((asker_profile, (resolution,)),) = [
        call for call in world.resume.calls if call[0] == fanout.ASKER
    ]
    assert "did not answer" in str(resolution.payload)
    assert world.mailbox.answers == {}
