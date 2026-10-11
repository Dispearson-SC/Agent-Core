"""A peer turn's session id carries the session that asked, so tools can find the case.

Subject: adapters/driving/peers/worker.py (`new_peer_session`, `answer_next_ask`)

The worker runs an answered ask in a FRESH session (never the asker's history). Verticals
whose tools must act on behalf of the asking conversation (a vertical: the case) need to know
which conversation asked, and the model must not be the one to say so. The id is built by
code from the claimed row's `from_session`, so it is as trustworthy as the row.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.adapters.driving.peers.worker import answer_next_ask, new_peer_session
from agent_core.domain.peers import AgentId, peer_session_key
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
class _Wake:
    woken: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, turn_id: str, correlation_id: str) -> None:
        self.woken.append((turn_id, correlation_id))


@dataclass
class _Runner:
    seen: list[TurnRequest] = field(default_factory=list)

    async def execute(self, turn_id: TurnId, request: TurnRequest, *, hop: int) -> TurnOutcome:
        self.seen.append(request)
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="ok"))


def test_the_worker_runs_the_answer_in_a_session_derived_from_the_asker() -> None:
    ask = PeerAsk(
        correlation_id="c1",
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("t1"),
        hop=1,
        asker=AgentId("coordinator"),
    )
    runner = _Runner()

    asyncio.run(answer_next_ask(_Queue(ask), runner, target=ask.target))

    (request,) = runner.seen
    assert request.session.session_id.startswith("peer~case-77~")
    assert request.session.tenant_id == "S030"


def test_claiming_the_same_ask_again_files_the_callee_turn_under_the_same_session() -> None:
    """C2: a worker that picks a row up again after a crash must not fork the conversation."""
    ask = PeerAsk(
        correlation_id="c3",
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("t3"),
        hop=1,
        asker=AgentId("coordinator"),
    )
    runner = _Runner()

    asyncio.run(answer_next_ask(_Queue(ask), runner, target=ask.target))
    asyncio.run(answer_next_ask(_Queue(ask), runner, target=ask.target))

    first, second = runner.seen
    assert first.session == second.session
    assert first.session.session_id == f"peer~case-77~{peer_session_key('c3')}"


def _ask() -> PeerAsk:
    return PeerAsk(
        correlation_id="c2",
        target=AgentId("specialist"),
        question="plan?",
        from_session=ORIGIN,
        turn_id=TurnId("t2"),
        hop=1,
        asker=AgentId("coordinator"),
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


class _ExplodingRunner:
    async def execute(self, turn_id: TurnId, request: TurnRequest, *, hop: int) -> TurnOutcome:
        raise RuntimeError("model blew up: secret-internal-detail")


def test_a_failure_after_the_claim_answers_the_ask_with_an_error_and_wakes_the_asker() -> None:
    """`claim_next` marks the row delivered and nothing redelivers it, so a raise after the
    claim used to strand the ask: the asking turn waited out its whole reply timeout.
    Recording a deterministic failure answer (`answer` is idempotent, first wins) and waking
    the asker lets it continue now; the error is still raised so the worker surfaces it."""
    ask, wake = _ask(), _Wake()
    queue = _Queue(ask)

    with pytest.raises(RuntimeError, match="model blew up"):
        asyncio.run(answer_next_ask(queue, _ExplodingRunner(), target=ask.target, wake=wake))

    ((correlation_id, text),) = queue.answered
    assert correlation_id == "c2"
    assert "could not be answered" in text
    assert "secret-internal-detail" not in text  # the asker's model never sees internals
    assert wake.woken == [("t2", "c2")]


def test_a_refused_minter_also_answers_the_ask_instead_of_stranding_it() -> None:
    ask = _ask()
    queue = _Queue(ask)

    with pytest.raises(ValueError):
        asyncio.run(
            answer_next_ask(
                queue,
                _Runner(),
                target=ask.target,
                mint_session=lambda tenant, origin: new_peer_session(tenant),
            )
        )

    assert [c for c, _ in queue.answered] == ["c2"]


# --- T2b: nested origin, `~` enforcement, cancellation, minter arity ---------------------


def test_a_nested_ask_keeps_the_root_origin() -> None:
    """Hop > 1: the asker is itself a peer session; the minted id must name the ROOT case."""
    first = new_peer_session(TenantId("S030"), origin=ORIGIN)
    second = new_peer_session(TenantId("S030"), origin=first)

    assert second.session_id.startswith("peer~case-77~")
    assert second.session_id.count("~") == 2
    assert second.session_id != first.session_id


def test_an_origin_containing_a_tilde_that_is_not_a_peer_id_is_refused() -> None:
    bad = SessionRef(session_id=SessionId("case~77"), tenant_id=TenantId("S030"))

    with pytest.raises(ValueError, match="~"):
        new_peer_session(TenantId("S030"), origin=bad)


def test_a_malformed_peer_id_origin_is_refused() -> None:
    bad = SessionRef(session_id=SessionId("peer~a~b~c"), tenant_id=TenantId("S030"))

    with pytest.raises(ValueError, match="~"):
        new_peer_session(TenantId("S030"), origin=bad)


def test_the_worker_names_a_nested_peer_session_after_the_root_case() -> None:
    nested = new_peer_session(TenantId("S030"), origin=ORIGIN)
    ask = PeerAsk(
        correlation_id="c3",
        target=AgentId("specialist"),
        question="plan?",
        from_session=nested,
        turn_id=TurnId("t3"),
        hop=2,
        asker=AgentId("coordinator"),
    )
    runner = _Runner()

    asyncio.run(answer_next_ask(_Queue(ask), runner, target=ask.target))

    (request,) = runner.seen
    assert request.session.session_id.startswith("peer~case-77~")
    assert request.session.session_id.count("~") == 2


class _CancellingRunner:
    async def execute(self, turn_id: TurnId, request: TurnRequest, *, hop: int) -> TurnOutcome:
        raise asyncio.CancelledError


def test_a_cancellation_after_the_claim_closes_the_ask_and_reraises() -> None:
    ask, wake = _ask(), _Wake()
    queue = _Queue(ask)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(answer_next_ask(queue, _CancellingRunner(), target=ask.target, wake=wake))

    ((correlation_id, text),) = queue.answered
    assert correlation_id == "c2"
    assert "could not be answered" in text
    assert wake.woken == [("t2", "c2")]


def test_a_wrong_arity_minter_fails_loudly_before_the_claim() -> None:
    ask = _ask()
    queue = _Queue(ask)

    with pytest.raises(TypeError, match="mint_session"):
        asyncio.run(
            answer_next_ask(
                queue,
                _Runner(),
                target=ask.target,
                mint_session=lambda tenant: new_peer_session(tenant),  # type: ignore[arg-type]
            )
        )

    assert queue.answered == []
