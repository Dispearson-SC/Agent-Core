"""Subagents on a REAL DBOS and the REAL `PgAgentMailbox` (C28, D62).

Phase:   Agent-comms v2, wave 5
Covers:  adapters/driving/workflow/turn_workflow.py (`_step_ask_peers` for `spawn_subagent`,
         the collect round, `cancel_turn`), adapters/driving/peers/worker.py (the owner of a
         subagent turn), adapters/driven/peers/mailbox.py and migration 0041.

WHAT IS BEING DEFENDED: A spawns two subagents in one model step; each runs as a durable child
workflow of its own (kind and depth on its row and on its owner), both results come back, and A is
resumed ONCE with both. Aborting A while they are still working cancels both rows and both
workflows. Only the model-side seats are doubles (`tests/fakes/peer_world.py`).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, cast

import psycopg
import pytest

from agent_core.adapters.driving.peers.worker import AskDisposition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.agent_mode import AgentMode, DelegationKind
from agent_core.domain.peers import AgentId
from agent_core.domain.subagents import SubagentPolicy
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
)
from tests.fakes import peer_world as w

_DATABASE = "agent_core_subagent_e2e_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


class _Start:
    """A spawns B and C in one step; B and C finish at once, or wait on `gate`."""

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None
        self.requests: list[TurnRequest] = []

    async def execute(self, turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        self.requests.append(request)
        if request.profile_id == w.A:
            pending = tuple(
                PendingRequest(
                    kind=PendingKind.DELEGATION,
                    tool_call_id=ToolCallId(f"call_{agent}"),
                    tool_name="spawn_subagent",
                    arguments={"agent": agent, "task": f"task for {agent}"},
                    reason="spawning",
                )
                for agent in (w.B, w.C)
            )
            return TurnOutcome(turn_id=turn_id, pending=pending)
        if self.gate is not None:
            await self.gate.wait()
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=f"{request.profile_id} did it"))


def _bind(monkeypatch: pytest.MonkeyPatch) -> tuple[w.World, _Start]:
    world = w.bind(monkeypatch, _DATABASE)
    start = _Start()
    seat = cast("turn_workflow.PeerSeat", turn_workflow._dependencies.peers)  # noqa: SLF001
    profiles = dict(seat.profiles)
    profiles[w.A] = dataclasses.replace(
        profiles[w.A], subagents=SubagentPolicy(enabled=True, agents=(AgentId(w.B), AgentId(w.C)))
    )
    for agent in (w.B, w.C):
        profiles[agent] = dataclasses.replace(profiles[agent], mode=AgentMode.SUBAGENT)
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        dataclasses.replace(
            turn_workflow._dependencies,  # noqa: SLF001
            start_turn=cast("Any", start),
            peers=turn_workflow.PeerSeat(mailbox=seat.mailbox, profiles=profiles),
        ),
    )
    return world, start


def _kinds() -> dict[str, tuple[str, int, str]]:
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        rows = conn.execute(
            "SELECT target_agent_id, kind, subagent_depth, state FROM peer_messages"
        ).fetchall()
    return {str(t): (str(k), int(d), str(s)) for t, k, d, s in rows}


def test_two_subagents_run_as_child_workflows_and_the_parent_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, start = _bind(monkeypatch)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: len(w.rows(_DATABASE)) == 2, "both spawns to reach the mailbox")
            seen["kinds"] = _kinds()
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            assert await w.dispatch(world, w.C) is AskDisposition.STARTED
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(drive())

    assert seen["kinds"] == {
        w.B: ("subagent", 1, "queued"),
        w.C: ("subagent", 1, "queued"),
    }
    assert seen["outcome"].result.text == "a done"
    assert [calls for profile, calls in world.resume.calls if profile == w.A] == [
        (("call_p-b", True), ("call_p-c", True))
    ], "the parent was resumed ONCE with both results"
    payloads = [str(p) for p in world.resume.payloads[w.A]]
    assert all('kind="subagent"' in p and 'status="ok"' in p for p in payloads), payloads
    owners = {r.profile_id: r.owner for r in start.requests if r.owner is not None}
    assert {(o.kind, o.subagent_depth) for o in owners.values()} == {(DelegationKind.SUBAGENT, 1)}
    assert {s for _, _, s in _kinds().values()} == {"answered"}


def test_aborting_the_parent_cancels_both_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    world, start = _bind(monkeypatch)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            start.gate = asyncio.Event()  # the subagents never finish by themselves
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: len(w.rows(_DATABASE)) == 2, "both spawns to reach the mailbox")
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            assert await w.dispatch(world, w.C) is AskDisposition.STARTED
            await w.until(lambda: len(start.requests) == 3, "both subagents to start")
            seen["cancelled"] = await turn_workflow.cancel_turn(w.ASKING_TURN, "operator abort")
            for name in (w.B, w.C):
                corr = next(c for c, (_, _, _) in w.rows(_DATABASE).items() if _kind(c) == name)
                seen[name] = await w.status(turn_workflow.peer_workflow_id(corr))
            start.gate.set()
            await asyncio.sleep(0.5)

    asyncio.run(drive())

    assert len(seen["cancelled"]) == 2
    assert seen[w.B] == "CANCELLED" and seen[w.C] == "CANCELLED"
    assert {s for _, _, s in _kinds().values()} == {"cancelled"}
    assert all(answer is None for _, _, answer in w.rows(_DATABASE).values())


def _kind(correlation_id: str) -> str:
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        row = conn.execute(
            "SELECT target_agent_id FROM peer_messages WHERE correlation_id = %s", (correlation_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])
