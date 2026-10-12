"""A deploy that bumps the DBOS application version must not strand peer asks (C1g, D32).

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driving/workflow/turn_workflow.py (`DurablePeerTurnStarter.exists/retire`,
         generation-suffixed peer workflow ids), adapters/driven/peers/mailbox.py (`reclaim_stale`)

THE DEFECT. `reclaim_stale` requeues a delivered, unanswered row only when no
callee workflow of the ask exists. DBOS knows the workflow a previous build started, so `exists`
was true for it for ever, although DBOS recovers a PENDING workflow only for the application version
it claims: the new build never would. The row stayed `delivered` and nothing ran it.

THE FIX BEING DEFENDED. A workflow that is still open under another application version counts as
absent. The reclaim retires it (cancels it and the asks it made, which also frees the one queue
slot its peer session holds), requeues the row with `redeliveries` + 1, and the next claim starts
the next generation of its id (`peer_workflow_id(corr, n)`). A workflow of the CURRENT version is
never touched.

Real DBOS, real `PgAgentMailbox`; the engine is relaunched under a different version to simulate
the deploy (`tests/fakes/peer_world.py`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_core.adapters.driving.peers.worker import AskDisposition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import AgentId
from tests.fakes import peer_world as w

_DATABASE = "agent_core_peer_version_bump_test"
_LEASE = timedelta(seconds=300)
_LATER = timedelta(hours=1)

pytestmark = pytest.mark.skipif(
    not w.postgres_reachable(), reason="no reachable Postgres instance"
)


async def _reclaim(world: w.World) -> tuple[str, ...]:
    starter = turn_workflow.DurablePeerTurnStarter()
    return await world.mailbox.reclaim_stale(
        AgentId(w.B),
        lease=_LEASE,
        exists=starter.exists,
        retire=starter.retire,
        now=datetime.now(UTC) + _LATER,
    )


async def _first_run_under(world: w.World, version: str) -> tuple[str, str]:
    """A asks B, B starts and asks C, nobody serves C: B's workflow is PENDING under `version`."""
    async with w.Dbos(_DATABASE, application_version=version):
        await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
        await w.until(lambda: w.rows(_DATABASE), "A's ask to reach the mailbox")
        assert await w.dispatch(world, w.B) is AskDisposition.STARTED
        await w.until(lambda: len(w.rows(_DATABASE)) == 2, "B's ask to C")
        found = w.rows(_DATABASE)
        first = next(c for c, (turn, _, _) in found.items() if turn == str(w.ASKING_TURN))
        second = next(c for c in found if c != first)
        assert await w.status(turn_workflow.peer_workflow_id(first)) == "PENDING"
        return first, second


def test_an_ask_whose_workflow_is_stranded_by_a_version_bump_is_rerun_under_the_new_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        with w.dying_process(monkeypatch):
            first, second = await _first_run_under(world, "v-old")
            await w.let_the_dead_settle()
        seen["ids"] = (first, second)

        world.start.b_answers_directly = True  # the rerun answers at once
        async with w.Dbos(_DATABASE, drop=False, application_version="v-new"):
            # the deploy did not recover B: still PENDING, under the old version
            seen["old_before"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["reclaimed"] = await _reclaim(world)
            seen["old_after"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["nested_after"] = w.rows(_DATABASE)[second][1]

            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            rerun = turn_workflow.peer_workflow_id(first, 1)
            seen["rerun_id"] = rerun
            outcome = await w.result_of(rerun)
            seen["rerun_text"] = outcome.result.text if outcome.result else None
            seen["again"] = await _reclaim(world)

    asyncio.run(drive())

    first, second = seen["ids"]
    assert seen["old_before"] == "PENDING", "the bump stranded the workflow"
    assert seen["reclaimed"] == (first,), "a workflow of another version counts as absent"
    assert seen["old_after"] == "CANCELLED", "the stranded run is retired, its slot freed"
    assert seen["nested_after"] == "cancelled", "and so is the ask it had made to C"
    assert seen["rerun_id"] == turn_workflow.peer_workflow_id(first, 1)
    assert seen["rerun_text"] == "b answers"
    assert w.rows(_DATABASE)[first][2] == "b answers", "the asker's row now holds the answer"
    assert world.start.starts(w.B) == 2, "the callee ran once per generation"
    assert seen["again"] == (), "the answered row is not reclaimed again"


def test_an_ask_whose_workflow_is_open_under_the_current_version_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE, application_version="v-same"):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: w.rows(_DATABASE), "A's ask to reach the mailbox")
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: len(w.rows(_DATABASE)) == 2, "B's ask to C")
            seen["reclaimed"] = await _reclaim(world)
            (first,) = [c for c, (t, _, _) in w.rows(_DATABASE).items() if t == str(w.ASKING_TURN)]
            seen["status"] = await w.status(turn_workflow.peer_workflow_id(first))

    asyncio.run(drive())

    assert seen["reclaimed"] == (), "a live workflow is B's own business, however old the claim"
    assert seen["status"] == "PENDING"
