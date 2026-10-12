"""Two approvals of one model step on a REAL DBOS resume the turn once (C6, D32).

Phase:   Agent-comms v2, wave 1
Covers:  adapters/driving/workflow/turn_workflow.py (`_collect_round` for a round with people
         only), `signal_decision`, the recorded round clock and `_step_expire`

The unit suite (`tests/unit/test_human_collect_round.py`) proves the decisions; this proves the
wiring: the durable wait, the recorded clock and the expiry step, against the production DBOS
configuration. The doubles are `tests/fakes/peer_world.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import ToolCallId
from tests.fakes import peer_world as w

_DATABASE = "agent_core_human_round_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def test_two_approvals_resume_the_turn_once_with_both_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    world.start.a_asks_people = ("tc-1", "tc-2")
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: len(world.gateway.published) >= 1, "the approvals to be asked")
            # the second approval arrives first, and the turn must not resume on it alone
            await turn_workflow.signal_decision(w.ASKING_TURN, ToolCallId("tc-2"), True, "second")
            await asyncio.sleep(1.0)
            seen["resumed_early"] = list(world.resume.calls)
            await turn_workflow.signal_decision(w.ASKING_TURN, ToolCallId("tc-1"), False, "no")
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(drive())

    assert seen["resumed_early"] == [], "one decision of two is not enough to resume"
    assert world.resume.calls == [(w.A, (("tc-1", False), ("tc-2", True)))], "once, both, in order"
    assert seen["outcome"].result is not None and seen["outcome"].result.text == "a done"


def test_a_person_who_never_decides_expires_the_turn_and_nothing_is_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = w.bind(monkeypatch, _DATABASE)
    world.start.a_asks_people = ("tc-1", "tc-2")
    monkeypatch.setattr(turn_workflow, "THREE_DAYS", 3)  # the person has three seconds
    seen: dict[str, Any] = {}

    async def drive() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.until(lambda: len(world.gateway.published) >= 1, "the approvals to be asked")
            await turn_workflow.signal_decision(w.ASKING_TURN, ToolCallId("tc-1"), True, None)
            seen["outcome"] = await w.result_of(str(w.ASKING_TURN))

    asyncio.run(drive())

    assert seen["outcome"].result is not None
    assert seen["outcome"].result.text == turn_workflow._NO_ANSWER, "the turn gave up"
    assert world.resume.calls == [], "the decision that did arrive was not applied on its own"
