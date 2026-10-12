"""Restart scenarios of a peer ask on a REAL DBOS: each delivers exactly once (C7, D32).

Phase:   Agent-comms v2, wave 1
Covers:  the whole ask -> claim -> callee workflow -> answer -> wake -> resume chain, the redelivery
         lease, and DBOS recovery, with the mailbox behind `ControlledMailbox`

A process death is simulated the way the engine sees it: the code at the chosen point never
returns (a gate that is never released) and the engine is destroyed under it, so the workflows it
was running stay `PENDING`; the next phase launches the engine again and DBOS recovers them. The
databases survive between phases (`Dbos(drop=False)`), the in-memory doubles are the test's.

Exactly once means, per scenario: the asker's ask was posted once, the callee's turn started once
(or once per redelivery generation, and only the last one answered), the answer was recorded once
and the asker's resume COMPLETED once. A resume that died mid-step is entered twice by design (the
step re-executes after a crash; the real `ResumeTurn` keys its ledger on `(turn, tool_call)`), so
`Resume.calls` lists completions and `Resume.entered` entries.
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
from tests.fakes.controlled_mailbox import ProcessDied

_DATABASE = "agent_core_peer_restarts_test"
_LEASE = timedelta(seconds=300)

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _world(monkeypatch: pytest.MonkeyPatch) -> w.World:
    world = w.bind(monkeypatch, _DATABASE, controlled=True)
    world.start.b_answers_directly = True  # A asks B, B answers at once
    return world


async def _until_asked(world: w.World) -> str:
    await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
    await w.until(lambda: w.rows(_DATABASE), "A's ask to reach the mailbox")
    # The ask is posted INSIDE the step; kill only after the engine has recorded that step, or the
    # recovery would run it again (harmlessly: the post is idempotent, but then it is not a replay).
    await w.until_step_recorded(str(w.ASKING_TURN), "_step_ask_peers")
    (correlation_id,) = w.rows(_DATABASE)
    return correlation_id


def _assert_delivered_exactly_once(world: w.World, correlation_id: str, outcome: Any) -> None:
    assert outcome.result is not None and outcome.result.text == "a done"
    row = w.rows(_DATABASE)[correlation_id]
    assert row[1] == "answered" and row[2] == "b answers"
    assert len(w.rows(_DATABASE)) == 1, "one ask, one row"
    assert world.mailbox.count("ask") == 1, "a replay of the asking step does not post again"
    assert world.mailbox.count("answer") == 1, "the answer was recorded once"
    assert world.resume.payloads[w.A] and len(world.resume.payloads[w.A]) == 1
    assert [profile for profile, _ in world.resume.calls] == [w.A], "A resumed once, to completion"


def test_a_kill_between_the_post_and_the_claim_delivers_once_after_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    seen: dict[str, Any] = {}

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            seen["corr"] = await _until_asked(world)
            assert world.mailbox.count("claim_next") == 0, "nobody claimed it before the kill"

    async def second_process() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    with w.dying_process(monkeypatch):
        asyncio.run(first_process())
    asyncio.run(second_process())

    _assert_delivered_exactly_once(world, seen["corr"], seen["outcome"])
    assert world.start.starts(w.B) == 1


def test_a_kill_between_the_claim_and_the_workflow_start_is_redelivered_once_by_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    seen: dict[str, Any] = {}
    # The claim commits and then the process dies: the row is `delivered` and nothing runs it.
    world.mailbox.fail("claim_next", ProcessDied(), after=True)

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            seen["corr"] = await _until_asked(world)
            with pytest.raises(ProcessDied):
                await w.dispatch(world, w.B)
            assert w.rows(_DATABASE)[seen["corr"]][1] == "delivered"

    async def second_process() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            starter = turn_workflow.DurablePeerTurnStarter()
            seen["reclaimed"] = await world.mailbox.reclaim_stale(
                AgentId(w.B),
                lease=_LEASE,
                exists=starter.exists,
                retire=starter.retire,
                now=datetime.now(UTC) + timedelta(hours=1),
            )
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))
            seen["again"] = await world.mailbox.reclaim_stale(
                AgentId(w.B),
                lease=_LEASE,
                exists=starter.exists,
                now=datetime.now(UTC) + timedelta(hours=1),
            )

    with w.dying_process(monkeypatch):
        asyncio.run(first_process())
    asyncio.run(second_process())

    assert seen["reclaimed"] == (seen["corr"],)
    assert seen["again"] == (), "the answered ask is not redelivered again"
    _assert_delivered_exactly_once(world, seen["corr"], seen["outcome"])
    assert world.start.starts(w.B) == 1, "the callee never ran before the redelivery"
    assert world.mailbox.count("claim_next") == 2


def test_a_kill_between_the_answer_and_the_wake_still_wakes_the_asker_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    seen: dict[str, Any] = {}
    real_signal = turn_workflow.signal_peer_answer
    signal_calls: list[str] = []

    async def dies_before_the_wake(turn_id: Any, correlation_id: str) -> None:
        signal_calls.append("died")
        await asyncio.Event().wait()

    async def counted_signal(turn_id: Any, correlation_id: str) -> None:
        signal_calls.append("sent")
        await real_signal(turn_id, correlation_id)

    async def first_process() -> None:
        with monkeypatch.context() as dying:
            dying.setattr(turn_workflow, "signal_peer_answer", dies_before_the_wake)
            async with w.Dbos(_DATABASE):
                seen["corr"] = await _until_asked(world)
                assert await w.dispatch(world, w.B) is AskDisposition.STARTED
                await w.until(
                    lambda: w.rows(_DATABASE)[seen["corr"]][1] == "answered",
                    "the answer to be recorded",
                )
                await w.until(lambda: signal_calls == ["died"], "the callee to reach the wake")
                assert world.resume.payloads == {}, "the asker was not woken before the kill"

    async def second_process() -> None:
        with monkeypatch.context() as alive:
            alive.setattr(turn_workflow, "signal_peer_answer", counted_signal)
            async with w.Dbos(_DATABASE, drop=False):
                seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(first_process())
    asyncio.run(second_process())

    _assert_delivered_exactly_once(world, seen["corr"], seen["outcome"])
    assert world.start.starts(w.B) == 1, "recovering the callee did not run its turn again"
    assert signal_calls == ["died", "sent"], "one wake before the kill never left, one after"


def test_a_kill_during_the_resume_completes_the_resume_once_after_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    world.resume.die_on_entry = {1}
    seen: dict[str, Any] = {}

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            seen["corr"] = await _until_asked(world)
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.resume.entered == 1, "A to start its resume")

    async def second_process() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(first_process())
    asyncio.run(second_process())

    _assert_delivered_exactly_once(world, seen["corr"], seen["outcome"])
    assert world.resume.entered == 2, "the interrupted step ran again, and only that one finished"
    assert world.start.starts(w.B) == 1
