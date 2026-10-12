"""C16 (D46): a peer's own approval carries the chain that owns it.

Subjects: domain/turn.py (`PeerOwner`), adapters/driving/peers/worker.py (`peer_turn_request`),
adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`, `_step_publish`),
adapters/driven/human/gateway.py (`_recipient`).

When a specialist suspends on a person, the request used to be filed under the specialist's own
session (`peer~<case>~<key>`), which no conversation the person is in lists, and was addressed to
that session id on the channel. The row that started the specialist knows better: the asking turn,
the asking agent, the call, and the root turn a person started. The worker turns that into
`TurnRequest.owner`; the workflow hands it to the gateway; asks made by a callee carry the same
root on.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

import pytest

from agent_core.adapters.driven.human.gateway import ChannelHumanGateway
from agent_core.adapters.driven.peers.mailbox import PeerAsk
from agent_core.adapters.driving.channels.registry import ChannelRegistry
from agent_core.adapters.driving.peers.worker import peer_turn_request
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.agent_mode import DelegationKind
from agent_core.domain.peers import AgentId
from agent_core.domain.turn import (
    PeerOwner,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
)
from tests.fakes import fanout
from tests.fakes import ports as fakes

TENANT = TenantId("t-1")
CASE = SessionRef(session_id=SessionId("case-77"), tenant_id=TENANT)


def _ask(**changes: Any) -> PeerAsk:
    values: dict[str, Any] = {
        "correlation_id": "corr-1",
        "target": AgentId("billing"),
        "question": "q",
        "from_session": CASE,
        "turn_id": TurnId("asker-turn"),
        "hop": 1,
        "asker": AgentId("support"),
        "root_turn_id": "root-turn",
    }
    values.update(changes)
    return PeerAsk(**values)


def test_the_callees_turn_request_carries_the_owner_chain_of_its_row() -> None:
    session = SessionRef(session_id=SessionId("peer~case-77~key"), tenant_id=TENANT)

    request = peer_turn_request(_ask(), session=session)

    assert request.owner == PeerOwner(
        root_turn_id="root-turn", asker_turn_id="asker-turn", asker="support", call="corr-1"
    )


def test_a_row_without_a_root_is_owned_by_its_asking_turn_and_one_without_an_asker_says_so() -> (
    None
):
    session = SessionRef(session_id=SessionId("peer~case-77~key"), tenant_id=TENANT)

    request = peer_turn_request(_ask(root_turn_id=None, asker=None), session=session)

    assert request.owner == PeerOwner(
        root_turn_id="asker-turn", asker_turn_id="asker-turn", asker=None, call="corr-1"
    )


# --- the gateway call ---------------------------------------------------------------------------


def _publish_request(owner: PeerOwner | None) -> TurnRequest:
    base = fanout.request()
    return TurnRequest(
        session=base.session,
        caller=base.caller,
        profile_id=base.profile_id,
        input=base.input,
        owner=owner,
    )


def _publish(monkeypatch: pytest.MonkeyPatch, request: TurnRequest) -> fakes.FakeHumanGateway:
    gateway = fakes.FakeHumanGateway()
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=None,  # type: ignore[arg-type]
            channels=ChannelRegistry(()),
            human_gateway=gateway,
        ),
    )
    pending = (fanout.ask("tc-1"),)
    asyncio.run(inspect.unwrap(turn_workflow._step_publish)(TurnId("callee"), request, pending))
    return gateway


def test_the_owner_of_a_peer_turn_goes_to_the_gateway_with_its_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = PeerOwner(root_turn_id="root", asker_turn_id="asker", asker="support", call="c")

    gateway = _publish(monkeypatch, _publish_request(owner))

    assert gateway.owners == [owner]


def test_a_turn_a_person_started_publishes_without_an_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _publish(monkeypatch, _publish_request(None))

    assert gateway.owners == [None]


# --- asks made by a callee ----------------------------------------------------------------------


def test_a_direct_ask_is_rooted_at_the_asking_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "x")])

    asyncio.run(fanout.body()(fanout.request()))

    assert world.mailbox.root_turns == ["turn-1"]


def test_an_ask_made_by_a_callee_keeps_the_root_it_was_started_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "x")])
    owned = _publish_request(
        PeerOwner(root_turn_id="the-root", asker_turn_id="b", asker="b", call="c")
    )

    asyncio.run(fanout.body()(owned))

    assert world.mailbox.root_turns == ["the-root"]


# --- who is told --------------------------------------------------------------------------------


def _gateway() -> ChannelHumanGateway:
    return ChannelHumanGateway(
        "postgresql://unused", ChannelRegistry(()), channel_id="whatsapp", ttl=timedelta(days=1)
    )


def test_an_approval_of_a_peer_session_is_addressed_to_the_case_the_person_is_in() -> None:
    peer = SessionRef(session_id=SessionId("peer~case-77~key"), tenant_id=TENANT)

    recipient = _gateway()._recipient(peer)  # noqa: SLF001

    assert recipient.subject_id == "case-77" and recipient.tenant_id == TENANT


def test_an_ordinary_session_is_addressed_as_before() -> None:
    assert _gateway()._recipient(CASE).subject_id == "case-77"  # noqa: SLF001


def test_the_owner_of_a_spawned_subagent_says_so_and_carries_its_depth() -> None:
    """C28. The row that started the callee knows whether it was asked or spawned and how deep the
    subagent chain down to it is; the worker copies both onto the owner, never a model."""
    session = SessionRef(session_id=SessionId("peer~case-77~key"), tenant_id=TENANT)

    plain = peer_turn_request(_ask(), session=session).owner
    spawned = peer_turn_request(
        _ask(kind=DelegationKind.SUBAGENT, subagent_depth=2), session=session
    ).owner

    assert plain is not None and spawned is not None
    assert (plain.kind, plain.subagent_depth) == (DelegationKind.PEER, 0)
    assert (spawned.kind, spawned.subagent_depth) == (DelegationKind.SUBAGENT, 2)
