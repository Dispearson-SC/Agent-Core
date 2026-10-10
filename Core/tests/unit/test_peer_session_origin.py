"""A peer turn's session id carries the session that asked, so tools can find the case.

Subject: adapters/driving/peers/worker.py (`new_peer_session`, `answer_next_ask`)

The worker runs an answered ask in a FRESH session (never the asker's history). Verticals
whose tools must act on behalf of the asking conversation (Glazed: the case) need to know
which conversation asked, and the model must not be the one to say so. The id is built by
code from the claimed row's `from_session`, so it is as trustworthy as the row.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.adapters.driving.peers.worker import answer_next_ask, new_peer_session
from agent_core.domain.peers import AgentId
from agent_core.domain.turn import (
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)

ORIGIN = SessionRef(session_id=SessionId("case-77"), tenant_id=TenantId("S030"))


def test_the_peer_session_embeds_the_asking_session_id() -> None:
    session = new_peer_session(TenantId("S030"), origin=ORIGIN)

    assert session.tenant_id == "S030"
    assert session.session_id.startswith("peer~case-77~")
    assert session.session_id != new_peer_session(TenantId("S030"), origin=ORIGIN).session_id


def test_without_an_origin_the_session_stays_opaque() -> None:
    session = new_peer_session(TenantId("S030"))

    assert session.session_id.startswith("peer-")
    assert "~" not in session.session_id


@dataclass
class _Queue:
    ask: PeerAsk
    answered: list[tuple[str, str]] = field(default_factory=list)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        return self.ask

    async def answer(self, correlation_id: str, answer: str) -> None:
        self.answered.append((correlation_id, answer))


@dataclass
class _Runner:
    seen: list[TurnRequest] = field(default_factory=list)

    async def execute(self, turn_id: TurnId, request: TurnRequest, *, hop: int) -> TurnOutcome:
        self.seen.append(request)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="ok"))


def test_the_worker_runs_the_answer_in_a_session_derived_from_the_asker() -> None:
    ask = PeerAsk(
        correlation_id="c1",
        target=AgentId("glazed_supply"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("t1"),
        hop=1,
        asker=AgentId("glazed_orchestrator"),
    )
    runner = _Runner()

    asyncio.run(answer_next_ask(_Queue(ask), runner, target=ask.target))

    (request,) = runner.seen
    assert request.session.session_id.startswith("peer~case-77~")
    assert request.session.tenant_id == "S030"


def _ask() -> PeerAsk:
    return PeerAsk(
        correlation_id="c2",
        target=AgentId("glazed_supply"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("t2"),
        hop=1,
        asker=AgentId("glazed_orchestrator"),
    )


def test_an_injected_session_minter_receives_the_asking_session() -> None:
    """R3: the injected path used to drop the origin, so the peer lost its case."""
    ask, runner = _ask(), _Runner()

    asyncio.run(
        answer_next_ask(
            _Queue(ask),
            runner,
            target=ask.target,
            mint_session=lambda tenant, origin: new_peer_session(tenant, origin),
        )
    )

    (request,) = runner.seen
    assert request.session.session_id.startswith("peer~case-77~")


def test_an_injected_minter_that_drops_the_origin_is_refused() -> None:
    ask, runner = _ask(), _Runner()

    with pytest.raises(ValueError, match="peer~case-77~"):
        asyncio.run(
            answer_next_ask(
                _Queue(ask),
                runner,
                target=ask.target,
                mint_session=lambda tenant, origin: new_peer_session(tenant),
            )
        )

    assert runner.seen == []
