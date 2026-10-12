"""A turn that keeps killing its process is failed, not retried for ever (C5, D32) - REAL DBOS.

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driving/workflow/turn_workflow.py (`AttemptLedger` hooks, `PoisonTurnError`),
         adapters/driven/persistence_pg/attempt_ledger.py

A crash is simulated the way it is seen by the engine: the first step of the turn never returns
and the engine is destroyed under it, so the workflow stays `PENDING`. Each relaunch recovers it
and re-runs its body, which is one more attempt. With a budget of 2 the third entry is refused:
the workflow ends `ERROR` with the reason, and for a callee the asker is answered with the fixed
failure text. A relaunch that finds the workflow parked on a person is NOT an attempt.

The doubles and databases are `tests/fakes/peer_world.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg.attempt_ledger import PgAttemptLedger
from agent_core.adapters.driving.peers.worker import AskDisposition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import PEER_ASK_FAILED_ANSWER
from agent_core.domain.turn import TurnId, TurnOutcome
from tests.fakes import peer_world as w

_DATABASE = "agent_core_poison_budget_test"
_BUDGET = 2

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _bind_with_ledger(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> w.World:
    world = w.bind(monkeypatch, _DATABASE, **kwargs)
    deps = turn_workflow._dependencies
    assert deps is not None
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=deps.start_turn,
            channels=deps.channels,
            human_gateway=deps.human_gateway,
            resume_turn=deps.resume_turn,
            peers=deps.peers,
            attempts=PgAttemptLedger(w.conninfo(_DATABASE)),
            max_attempts=_BUDGET,
        ),
    )
    return world


class _HangingStart:
    """`StartTurn` whose first step never returns: the process is 'killed' inside it."""

    def __init__(self) -> None:
        self.entered = 0

    async def execute(self, turn_id: TurnId, request: Any) -> TurnOutcome:
        self.entered += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _attempts(workflow_id: str) -> tuple[int, bool] | None:
    with psycopg.connect(w.conninfo(_DATABASE)) as conn:
        row = conn.execute(
            "SELECT attempts, parked FROM workflow_attempts WHERE workflow_id = %s", (workflow_id,)
        ).fetchone()
    return None if row is None else (int(row[0]), bool(row[1]))


def test_a_turn_that_crashes_past_the_budget_fails_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_with_ledger(monkeypatch)
    hanging = _HangingStart()
    deps = turn_workflow._dependencies
    assert deps is not None
    monkeypatch.setattr(
        turn_workflow,
        "_dependencies",
        turn_workflow.TurnWorkflowDependencies(
            start_turn=hanging,  # type: ignore[arg-type]
            channels=deps.channels,
            human_gateway=deps.human_gateway,
            resume_turn=deps.resume_turn,
            peers=deps.peers,
            attempts=deps.attempts,
            max_attempts=_BUDGET,
        ),
    )
    seen: dict[str, Any] = {}

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: hanging.entered == 1, "the turn to start")

    async def crashed_again() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            await w.until(lambda: hanging.entered == 2, "the recovered turn to start again")
            seen["attempts_before"] = _attempts(str(w.ASKING_TURN))

    async def third_process() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            from dbos import DBOS

            handle = await DBOS.retrieve_workflow_async(str(w.ASKING_TURN))
            try:
                await asyncio.wait_for(handle.get_result(), w.DEADLINE)
            except turn_workflow.PoisonTurnError as poisoned:
                seen["error"] = str(poisoned)
            seen["status"] = await w.status(str(w.ASKING_TURN))

    asyncio.run(first_process())
    asyncio.run(crashed_again())
    asyncio.run(third_process())

    assert seen["attempts_before"] == (2, False)
    assert seen["status"] == "ERROR"
    assert "3 attempts" in seen["error"] and f"budget of {_BUDGET}" in seen["error"]
    assert hanging.entered == 2, "the poisoned entry ran no step"
    assert _attempts(str(w.ASKING_TURN)) == (3, False), "the evidence stays on the ledger"


def test_a_callee_past_the_budget_answers_its_asker_with_the_fixed_failure_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _bind_with_ledger(monkeypatch)
    hanging = _HangingStart()
    original = world.start.execute

    async def route(turn_id: TurnId, request: Any) -> TurnOutcome:
        if request.profile_id == w.B:
            return await hanging.execute(turn_id, request)  # B's turn never gets past start
        return await original(turn_id, request)

    world.start.execute = route  # type: ignore[method-assign]
    world.start.b_answers_directly = False

    async def drive(launch_drop: bool) -> None:
        async with w.Dbos(_DATABASE, drop=launch_drop):
            if launch_drop:
                await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
                await w.until(lambda: w.rows(_DATABASE), "A's ask to reach the mailbox")
                assert await w.dispatch(world, w.B) is AskDisposition.STARTED
                await w.until(lambda: hanging.entered == 1, "B to start")
            elif hanging.entered == 1:
                await w.until(lambda: hanging.entered == 2, "B to be recovered")
            else:
                (first,) = list(w.rows(_DATABASE))
                await w.until(
                    lambda: w.rows(_DATABASE)[first][2] is not None, "the asker to be answered"
                )

    with w.dying_process(monkeypatch):
        asyncio.run(drive(True))
        asyncio.run(drive(False))
    asyncio.run(drive(False))

    ((first, (_, state, answer)),) = list(w.rows(_DATABASE).items())
    assert answer == PEER_ASK_FAILED_ANSWER and state == "answered"
    assert hanging.entered == 2


def test_restarting_a_turn_parked_on_a_person_never_uses_up_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C waits for its human. The engine is restarted far more often than the budget allows."""
    world = _bind_with_ledger(monkeypatch, c_waits_for_a_human=True)
    world.start.b_answers_directly = False
    restarts = _BUDGET * 3
    seen: dict[str, Any] = {}

    async def first() -> None:
        async with w.Dbos(_DATABASE):
            seen["ids"] = await w.run_to_the_third_agent(world)
            await w.until(lambda: world.gateway.published, "C's approval request")

    async def restart() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            await asyncio.sleep(1.5)  # let recovery re-run the parked bodies

    async def finish() -> None:
        async with w.Dbos(_DATABASE, drop=False):
            _, second = seen["ids"]
            await turn_workflow.signal_decision(
                TurnId(turn_workflow.peer_workflow_id(second)), w.APPROVAL_CALL, True, None
            )
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    with w.dying_process(monkeypatch):
        asyncio.run(first())
        for _ in range(restarts):
            asyncio.run(restart())
    asyncio.run(finish())

    assert seen["outcome"].result is not None and seen["outcome"].result.text == "a done"
    assert _attempts(str(w.ASKING_TURN)) is None, "finished: the ledger row is gone"


async def _to_the_third_agent(world: w.World) -> tuple[str, str]:
    return await w.run_to_the_third_agent(world)
