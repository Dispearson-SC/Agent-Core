"""The peer worker hands a claimed ask to a DURABLE starter instead of running the turn (C1).

Subject: adapters/driving/peers/worker.py (`dispatch_next_ask`, `PeerTurnStarter`,
`run_peer_worker`).

The worker used to run the callee turn itself and answer on return. A turn that suspended (its
own human, a third agent) answered nobody; a worker that died after the claim lost the row. Now
the worker claims, builds the request AS THE TARGET AGENT, and starts a workflow that owns the
rest. It never answers a started ask itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.adapters.driving.peers.worker import (
    PEER_CHANNEL,
    PEER_ROLE,
    PEER_SUBJECT_ID,
    AskDisposition,
    PeerTurnStarter,
    dispatch_next_ask,
    run_peer_worker,
)
from agent_core.domain.peers import PEER_ASK_FAILED_ANSWER, AgentId, peer_session_key
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId, TurnRequest

ORIGIN = SessionRef(session_id=SessionId("case-77"), tenant_id=TenantId("S030"))


def _ask(corr: str = "c1", hop: int = 1, token: str | None = "tok-c1") -> PeerAsk:
    return PeerAsk(
        correlation_id=corr,
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("asker-turn"),
        hop=hop,
        asker=AgentId("coordinator"),
        answer_token=token,
    )


@dataclass
class _Queue:
    asks: list[PeerAsk]
    answered: list[tuple[str, str]] = field(default_factory=list)
    tokens: list[str | None] = field(default_factory=list)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        return self.asks.pop(0) if self.asks else None

    async def answer(
        self, correlation_id: str, answer: str, *, answer_token: str | None = None
    ) -> None:
        self.answered.append((correlation_id, answer))
        self.tokens.append(answer_token)


@dataclass
class _Starter:
    started: list[tuple[str, TurnId, TurnRequest]] = field(default_factory=list)
    tokens: list[str | None] = field(default_factory=list)
    error: BaseException | None = None
    # C1d: the enqueue COMMITTED before the call failed (a lost ack, a shutdown).
    commits_before_failing: bool = False
    exists_error: Exception | None = None
    exists_calls: list[str] = field(default_factory=list)

    async def exists(self, correlation_id: str) -> bool:
        self.exists_calls.append(correlation_id)
        if self.exists_error is not None:
            raise self.exists_error
        return any(started[0] == correlation_id for started in self.started)

    async def start(
        self,
        correlation_id: str,
        asking_turn_id: TurnId,
        request: TurnRequest,
        *,
        answer_token: str | None = None,
    ) -> None:
        if self.error is not None:
            if self.commits_before_failing:
                self.started.append((correlation_id, asking_turn_id, request))
            raise self.error
        self.started.append((correlation_id, asking_turn_id, request))
        self.tokens.append(answer_token)


def test_a_claimed_ask_starts_the_callee_turn_as_the_target_and_is_not_answered_by_the_worker() -> (
    None
):
    queue, starter, woken = _Queue([_ask()]), _Starter(), []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    report = asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))

    assert report is not None and report.disposition is AskDisposition.STARTED
    ((corr, asking_turn, request),) = starter.started
    assert (corr, asking_turn) == ("c1", TurnId("asker-turn"))
    assert request.profile_id == "specialist", "the TARGET's profile, never the asker's"
    assert request.caller.subject_id == PEER_SUBJECT_ID
    assert request.caller.channel == PEER_CHANNEL
    assert request.caller.roles == frozenset({PEER_ROLE})
    assert request.hop == 1, "the hop count travels onto the callee's turn"
    assert request.input.text == "plan?"
    assert request.session.session_id == f"peer~case-77~{peer_session_key('c1')}"
    assert request.session.tenant_id == "S030"
    assert queue.answered == [] and woken == [], "the workflow answers, not the worker"


def test_an_empty_queue_starts_nothing() -> None:
    starter = _Starter()

    report = asyncio.run(dispatch_next_ask(_Queue([]), starter, target=AgentId("specialist")))

    assert report is None and starter.started == []


def test_claiming_the_same_row_again_starts_the_same_workflow_inputs() -> None:
    """The worker may die after the claim and a retry may claim again: same ids, same session."""
    starter = _Starter()

    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))
    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))

    first, second = starter.started
    assert first == second


def test_a_start_that_fails_closes_the_ask_with_fixed_text_and_raises() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("queue unavailable: secret dsn"))
    woken: list[tuple[TurnId, str]] = []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))

    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert woken == [(TurnId("asker-turn"), "c1")]


def test_the_worker_loop_uses_the_starter_when_it_is_given_one() -> None:
    queue, starter, lines = _Queue([_ask("a"), _ask("b")]), _Starter(), []
    passes = iter([True, True, False])

    asyncio.run(
        run_peer_worker(
            queue,
            starter,
            targets=[AgentId("specialist")],
            keep_going=lambda: next(passes),
            write_line=lines.append,
        )
    )

    assert [corr for corr, _, _ in starter.started] == ["a", "b"]
    assert lines == ["[peer] specialist a started", "[peer] specialist b started"]


def test_a_starter_is_recognised_by_shape() -> None:
    assert isinstance(_Starter(), PeerTurnStarter)


def test_the_answer_token_travels_from_the_claim_to_the_workflow_start() -> None:
    """C1c. The secret is on the claimed row and goes to the durable starter, which hands it
    to the workflow that is the only legitimate answerer."""
    starter = _Starter()

    asyncio.run(dispatch_next_ask(_Queue([_ask()]), starter, target=AgentId("specialist")))

    assert starter.tokens == ["tok-c1"]


def test_closing_a_failed_start_presents_the_token_too() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("down"))

    with pytest.raises(RuntimeError):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist")))

    assert queue.tokens == ["tok-c1"]


def _dispatch_failing(
    starter: _Starter, queue: _Queue, error_type: type[BaseException]
) -> list[tuple[TurnId, str]]:
    woken: list[tuple[TurnId, str]] = []

    async def wake(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((turn_id, correlation_id))

    with pytest.raises(error_type):
        asyncio.run(dispatch_next_ask(queue, starter, target=AgentId("specialist"), wake=wake))
    return woken


def test_a_start_that_committed_before_failing_does_not_close_the_ask() -> None:
    """C1d. A lost ack after the enqueue committed: the workflow exists and will answer. Closing
    the ask with the failure text would hand the asker a wrong answer first."""
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("ack lost"), commits_before_failing=True)

    woken = _dispatch_failing(starter, queue, RuntimeError)

    assert starter.exists_calls == ["c1"], "asked whether peer-<id> exists before deciding"
    assert queue.answered == [] and woken == []


def test_a_cancelled_start_whose_workflow_exists_does_not_close_the_ask_and_reraises() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=asyncio.CancelledError(), commits_before_failing=True)

    woken = _dispatch_failing(starter, queue, asyncio.CancelledError)

    assert queue.answered == [] and woken == []


def test_a_cancelled_start_that_definitely_enqueued_nothing_closes_the_ask() -> None:
    queue = _Queue([_ask()])
    starter = _Starter(error=asyncio.CancelledError())

    woken = _dispatch_failing(starter, queue, asyncio.CancelledError)

    assert queue.answered == [("c1", PEER_ASK_FAILED_ANSWER)]
    assert woken == [(TurnId("asker-turn"), "c1")]


def test_when_existence_cannot_be_determined_the_ask_is_left_open_for_redelivery() -> None:
    """Unknown is not 'does not exist': closing could hide a live workflow's answer. The row
    stays delivered and the C3 lease reclaims it."""
    queue = _Queue([_ask()])
    starter = _Starter(error=RuntimeError("down"), exists_error=ConnectionError("db down"))

    woken = _dispatch_failing(starter, queue, RuntimeError)

    assert queue.answered == [] and woken == []
