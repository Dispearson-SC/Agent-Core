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


def _starter(*, retire_other_versions: bool = False) -> turn_workflow.DurablePeerTurnStarter:
    return turn_workflow.DurablePeerTurnStarter(retire_other_versions=retire_other_versions)


async def _reclaim(world: w.World, *, retire_other_versions: bool = False) -> tuple[str, ...]:
    starter = _starter(retire_other_versions=retire_other_versions)
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
            seen["reclaimed"] = await _reclaim(world, retire_other_versions=True)
            seen["old_after"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["nested_after"] = w.rows(_DATABASE)[second][1]

            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            rerun = turn_workflow.peer_workflow_id(first, 1)
            seen["rerun_id"] = rerun
            outcome = await w.result_of(rerun)
            seen["rerun_text"] = outcome.result.text if outcome.result else None
            seen["again"] = await _reclaim(world, retire_other_versions=True)

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


def test_by_default_a_workflow_open_under_another_version_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1-001 (D33): during a rolling deploy the other version's executor may be alive and running
    (or parked on a human) that very workflow. Without the operator's say-so nothing cancels it."""
    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        with w.dying_process(monkeypatch):
            first, _ = await _first_run_under(world, "v-old")
            await w.let_the_dead_settle()
        seen["first"] = first
        async with w.Dbos(_DATABASE, drop=False, application_version="v-new"):
            # a DBOS call first: it re-installs the loop's default executor the destroy shut down
            seen["before"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["reclaimed"] = await _reclaim(world)
            seen["status"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["row"] = w.rows(_DATABASE)[first][1]
            seen["opted_in"] = await _reclaim(world, retire_other_versions=True)

    asyncio.run(drive())

    assert seen["before"] == "PENDING"
    assert seen["reclaimed"] == (), "an old-version workflow may still be running: not ours"
    assert seen["status"] == "PENDING", "and it was not cancelled"
    assert seen["row"] == "delivered"
    assert seen["opted_in"] == (seen["first"],), "the operator's flag restores the D32 behaviour"


# --- D33: a retire that is interrupted must converge -------------------------------------------


def test_a_retire_that_fails_after_the_engine_cancel_is_finished_by_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3/R4: `retire` cancels the stranded workflow and THEN the asks it made. If that second
    part raises, the workflow is CANCELLED but the row is still `delivered` and unanswered. The
    next pass used to see a workflow that "exists" (finished) and leave the row for ever."""
    world = w.bind(monkeypatch, _DATABASE, controlled=True)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        with w.dying_process(monkeypatch):
            first, second = await _first_run_under(world, "v-old")
            await w.let_the_dead_settle()
        seen["ids"] = (first, second)
        world.start.b_answers_directly = True
        async with w.Dbos(_DATABASE, drop=False, application_version="v-new"):
            await w.status(turn_workflow.peer_workflow_id(first))
            world.mailbox.fail("cancel_open", RuntimeError("database went away"))
            seen["first_pass"] = await _reclaim(world, retire_other_versions=True)
            seen["status_between"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["row_between"] = w.rows(_DATABASE)[first][1]
            seen["second_pass"] = await _reclaim(world, retire_other_versions=True)
            seen["nested"] = w.rows(_DATABASE)[second][1]
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            outcome = await w.result_of(turn_workflow.peer_workflow_id(first, 1))
            seen["text"] = outcome.result.text if outcome.result else None

    asyncio.run(drive())

    first, _ = seen["ids"]
    assert seen["first_pass"] == (), "the interrupted retire left the row alone"
    assert seen["status_between"] == "CANCELLED" and seen["row_between"] == "delivered"
    assert seen["second_pass"] == (first,), "a cancelled workflow without an answer is absent"
    assert seen["nested"] == "cancelled", "and the retry finished cancelling what it had asked"
    assert seen["text"] == "b answers"


def test_a_workflow_cancelled_before_the_requeue_is_requeued_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker died between `retire` and the requeue UPDATE: same persisted state, and no
    operator flag is needed - the workflow is CANCELLED, so no executor of any version runs it."""
    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        with w.dying_process(monkeypatch):
            first, _ = await _first_run_under(world, "v-old")
            await w.let_the_dead_settle()
        seen["first"] = first
        async with w.Dbos(_DATABASE, drop=False, application_version="v-new"):
            from dbos import DBOS

            await DBOS.cancel_workflow_async(turn_workflow.peer_workflow_id(first))
            seen["status"] = await w.status(turn_workflow.peer_workflow_id(first))
            seen["reclaimed"] = await _reclaim(world)

    asyncio.run(drive())

    assert seen["status"] == "CANCELLED"
    assert seen["reclaimed"] == (seen["first"],)


def test_cancelling_the_asker_reaches_a_live_generation_behind_a_lost_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: gen 0 is stranded (retired), gen 1 never started (lost), gen 2 is live. The cancel
    cascade must reach gen 2 although DBOS does not know gen 1."""
    import psycopg

    world = w.bind(monkeypatch, _DATABASE)
    seen: dict[str, Any] = {}

    async def drive() -> None:
        with w.dying_process(monkeypatch):
            first, _ = await _first_run_under(world, "v-old")
            await w.let_the_dead_settle()
        async with w.Dbos(_DATABASE, drop=False, application_version="v-new"):
            await w.status(turn_workflow.peer_workflow_id(first))
            assert await _reclaim(world, retire_other_versions=True) == (first,)  # gen 1 queued
            with psycopg.connect(w.conninfo(_DATABASE), autocommit=True) as conn:  # ... and lost
                conn.execute(
                    "UPDATE peer_messages SET redeliveries = 2 WHERE correlation_id = %s", (first,)
                )
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED  # gen 2 runs
            gen2 = turn_workflow.peer_workflow_id(first, 2)
            await w.until_step_recorded(gen2, "_step_new_turn_id")
            seen["gen1"] = await w.status(turn_workflow.peer_workflow_id(first, 1))
            seen["gen2_before"] = await w.status(gen2)
            await turn_workflow.cancel_turn(w.ASKING_TURN, "operator cancel")
            seen["gen2_after"] = await w.status(gen2)

    asyncio.run(drive())

    assert seen["gen1"] is None, "generation 1 never existed: a gap"
    assert seen["gen2_before"] in {"PENDING", "ENQUEUED"}
    assert seen["gen2_after"] == "CANCELLED", "the cascade crossed the gap"
