"""Nested peer asks (A -> B -> C) on a REAL DBOS and the REAL `PgAgentMailbox` (C1f, D32).

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driven/peers/mailbox.py (`peer_messages.turn_id` as TEXT, migration 0029),
         adapters/driving/workflow/turn_workflow.py (`_cancel_callees` cascade)

WHAT IS BEING DEFENDED

    1. A CALLEE CAN ASK A THIRD AGENT. B's turn id is its workflow id, which was no UUID (C1f) and
       is one now (C1i). Its ask to C used to fail on INSERT (`peer_messages.turn_id` was a UUID
       column);
       now C's answer flows to B, B's reply flows to A, and each hop is exactly one row.

    2. GIVING UP ON B GIVES UP ON C. When A cancels, or its reply timeout fires, B's workflow is
       cancelled and so are the asks B made (rows and workflows), however deep the chain.

Only the model-facing seats (`StartTurn`, `ResumeTurn`) and the human gateway are in-memory
doubles (`tests/fakes/peer_world.py`); the mailbox is the Postgres adapter, the engine is DBOS
with the production config.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import TurnId, TurnRequest
from tests.fakes import peer_envelope
from tests.fakes import peer_world as w

_DATABASE = "agent_core_peer_nested_test"

pytestmark = pytest.mark.skipif(
    not w.postgres_reachable(), reason="no reachable Postgres instance"
)


def test_a_nested_ask_is_answered_through_both_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            seen["ids"] = await w.run_to_the_third_agent(world)
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(drive())

    first, second = seen["ids"]
    found = w.rows(_DATABASE)
    callee_turn = turn_workflow.peer_workflow_id(first)
    assert found[second][0] == callee_turn, "B's ask is filed under B's workflow id"
    assert found[first][2] == "b relays" and found[second][2] == "c answers"
    assert seen["outcome"].result.text == "a done"
    assert [peer_envelope.contents(p) for p in world.resume.payloads[w.B]] == [
        wrap_peer_answer("c answers")
    ]
    assert [peer_envelope.contents(p) for p in world.resume.payloads[w.A]] == [
        wrap_peer_answer("b relays")
    ]


def test_cancelling_the_asker_cancels_the_third_agent_too(monkeypatch: pytest.MonkeyPatch) -> None:
    world = w.bind(monkeypatch, _DATABASE, c_waits_for_a_human=True)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            seen["ids"] = await w.run_to_the_third_agent(world)
            await w.until(lambda: world.gateway.published, "C's approval request")
            seen["cancelled"] = await turn_workflow.cancel_turn(w.ASKING_TURN, "operator cancel")
            first, second = seen["ids"]
            for name, corr in (("b", first), ("c", second)):
                seen[name] = await w.status(turn_workflow.peer_workflow_id(corr))
            # C's human approves after the fact: nothing may be written or woken
            await turn_workflow.signal_decision(
                TurnId(turn_workflow.peer_workflow_id(second)), w.APPROVAL_CALL, True, None
            )
            await asyncio.sleep(1.0)

    asyncio.run(drive())

    first, second = seen["ids"]
    assert seen["cancelled"] == (first, second)
    assert seen["b"] == "CANCELLED" and seen["c"] == "CANCELLED"
    found = w.rows(_DATABASE)
    assert found[first][1] == "cancelled" and found[second][1] == "cancelled"
    assert found[first][2] is None and found[second][2] is None, "no answer was written"


def test_the_askers_reply_timeout_cancels_the_third_agent_through_the_recorded_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE, c_waits_for_a_human=True)

    async def timeout(request: TurnRequest) -> int:
        return 1 if request.profile_id == w.A else 600

    monkeypatch.setattr(turn_workflow, "_step_peer_reply_timeout", timeout)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            seen["ids"] = await w.run_to_the_third_agent(world)
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))
            first, second = seen["ids"]
            seen["b"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["c"] = await w.status(turn_workflow.peer_workflow_id(second))

    asyncio.run(drive())

    first, second = seen["ids"]
    assert seen["outcome"].result.text == "a done", "A went on with the timeout text"
    assert seen["b"] == "CANCELLED" and seen["c"] == "CANCELLED"
    found = w.rows(_DATABASE)
    assert found[first][1] == "cancelled" and found[second][1] == "cancelled"
