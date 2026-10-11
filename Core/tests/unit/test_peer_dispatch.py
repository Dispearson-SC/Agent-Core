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


def _ask(corr: str = "c1", hop: int = 1) -> PeerAsk:
    return PeerAsk(
        correlation_id=corr,
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("asker-turn"),
        hop=hop,
        asker=AgentId("coordinator"),
    )


@dataclass
class _Queue:
    asks: list[PeerAsk]
    answered: list[tuple[str, str]] = field(default_factory=list)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        return self.asks.pop(0) if self.asks else None

    async def answer(self, correlation_id: str, answer: str) -> None:
        self.answered.append((correlation_id, answer))


@dataclass
class _Starter:
    started: list[tuple[str, TurnId, TurnRequest]] = field(default_factory=list)
    error: Exception | None = None

    async def start(
        self, correlation_id: str, asking_turn_id: TurnId, request: TurnRequest
    ) -> None:
        if self.error is not None:
            raise self.error
        self.started.append((correlation_id, asking_turn_id, request))


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
