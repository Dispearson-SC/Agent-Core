"""A background ask on a REAL DBOS and the REAL Postgres mailbox (C19, D49).

Phase:   Agent-comms v2, wave 3
Covers:  adapters/driving/workflow/turn_workflow.py (`_step_ask_peers` background branch,
         `_close_peer_ask` -> `_deliver_background_answer`, `close_conversation`),
         adapters/driven/peers/mailbox.py (the return address on the row)

WHAT IS BEING DEFENDED

    1. THE ASKER IS NOT HELD. A asks B in the background and A's turn completes with nobody having
       claimed the ask: it did not wait.
    2. THE ANSWER COMES BACK ONCE, EVEN ACROSS A RESTART. B's callee workflow is killed mid-turn,
       the engine is relaunched, DBOS recovers it, and the answer reaches A's conversation as ONE
       follow-up turn (deterministic turn id), run as the identity that asked.
    3. CLOSING THE CONVERSATION STOPS THE CHILD. The callee's workflow is cancelled, a late answer
       is refused and no follow-up turn is ever created.

Only the seats a model or a person sits behind are doubles (`tests/fakes/peer_world.py`).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.peers.worker import AskDisposition
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import background_followup_turn_id
from tests.fakes import peer_world as w

_DATABASE = "agent_core_peer_background_test"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _world(monkeypatch: pytest.MonkeyPatch) -> w.World:
    world = w.bind(monkeypatch, _DATABASE)
    world.start.b_answers_directly = True
    world.start.a_asks_in_background = True
    return world


def test_a_background_ask_frees_the_asker_and_its_answer_returns_once_across_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    world.start.b_hangs = True
    seen: dict[str, Any] = {}

    async def first_process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            # A's turn ends although nobody has claimed the ask.
            seen["asker"] = await w.result_of(str(w.ASKING_TURN))
            (seen["corr"],) = w.rows(_DATABASE)
            assert w.rows(_DATABASE)[seen["corr"]][1] == "queued", "B has not even been asked yet"
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.start.starts(w.B) == 1, "B to start its turn")

    async def second_process() -> None:
        world.start.b_hangs = False
        async with w.Dbos(_DATABASE, drop=False):
            follow_up = background_followup_turn_id(seen["corr"])
            # The recovered callee answers, and only then does the follow-up turn exist.
            for _ in range(int(w.DEADLINE / w.POLL)):
                if await w.status(follow_up) is not None:
                    break
                await asyncio.sleep(w.POLL)
            seen["follow_up"] = await w.result_of(follow_up)
            seen["status"] = await w.status(follow_up)

    with w.dying_process(monkeypatch):
        asyncio.run(first_process())
    asyncio.run(second_process())

    assert seen["asker"].result is not None and seen["asker"].result.text == "a done"
    assert [p for p, _ in world.resume.calls] == [w.A], "A's own turn resumed once, on `running`"
    envelope = str(world.resume.payloads[w.A][0])
    assert 'status="running"' in envelope and f'handle="{seen["corr"]}"' in envelope
    assert w.rows(_DATABASE)[seen["corr"]][1:] == ("answered", "b answers")
    assert seen["follow_up"].result is not None and seen["follow_up"].result.text == "a follow-up"
    assert seen["status"] == "SUCCESS"
    follow_ups = [r for r in world.start.requests if r.input.text.startswith("[Background result]")]
    assert len(follow_ups) == 1, "one follow-up turn, however many times the callee was entered"
    (follow_up_request,) = follow_ups
    assert follow_up_request.profile_id == w.A
    assert follow_up_request.session == w.request().session
    assert follow_up_request.caller == w.request().caller, "run as the identity that asked"
    assert f'handle="{seen["corr"]}"' in follow_up_request.input.text
    assert "b answers" in follow_up_request.input.text


def test_closing_the_conversation_stops_the_child_and_no_follow_up_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(monkeypatch)
    world.start.b_hangs = True
    seen: dict[str, Any] = {}

    async def process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.result_of(str(w.ASKING_TURN))
            (seen["corr"],) = w.rows(_DATABASE)
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.start.starts(w.B) == 1, "B to be running")
            seen["ended"] = await turn_workflow.close_conversation(
                w.request().session, "conversation closed"
            )
            await asyncio.sleep(0.5)  # let the cancel reach the engine
            seen["callee"] = await w.status(turn_workflow.peer_workflow_id(seen["corr"]))
            seen["follow_up"] = await w.status(background_followup_turn_id(seen["corr"]))

    asyncio.run(process())

    assert seen["ended"] == (seen["corr"],)
    assert w.rows(_DATABASE)[seen["corr"]][1] == "cancelled"
    assert seen["callee"] == "CANCELLED"
    assert seen["follow_up"] is None, "a stopped child answers nobody"


# --- managing a running child (C20, D50) ----------------------------------------------------------


def _seat(world: w.World) -> Any:
    from agent_core.adapters.driving.workflow.background_agents import WorkflowBackgroundAgents

    return WorkflowBackgroundAgents(world.mailbox, w.profiles())


def _owner() -> Any:
    from agent_core.adapters.driven.tools.background import BackgroundOwner

    request = w.request()
    return BackgroundOwner(
        session=request.session, profile_id=w.A, caller=request.caller, turn_id="turn-2"
    )


def test_a_message_to_a_running_child_runs_after_the_turn_it_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.driven.tools.background import MessageMode

    world = _world(monkeypatch)
    world.start.b_gate = asyncio.Event()
    seen: dict[str, Any] = {}

    async def process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.result_of(str(w.ASKING_TURN))
            (corr,) = w.rows(_DATABASE)
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.start.starts(w.B) == 1, "B to be running")

            sent = await _seat(world).message(
                _owner(), corr, "use March", MessageMode.STEER, call_id="call-1"
            )
            assert sent.handle is not None, sent.refusal
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await asyncio.sleep(1.5)
            seen["while_running"] = world.start.starts(w.B)

            world.start.b_gate.set()
            await w.until(lambda: world.start.starts(w.B) == 2, "the message to run")
            for handle in (corr, sent.handle):
                follow_up = background_followup_turn_id(handle)
                await w.until_status(follow_up)
                seen[handle] = (await w.result_of(follow_up)).result
            seen["ids"] = (corr, sent.handle)

    asyncio.run(process())

    assert seen["while_running"] == 1, "the message waited for the child's turn to end"
    first, second = [r for r in world.start.requests if r.profile_id == w.B]
    assert second.session == first.session, "it continues the child's own session"
    assert second.input.text.startswith("Steering note") and "use March" in second.input.text
    corr, sent = seen["ids"]
    assert seen[corr] is not None and seen[sent] is not None, "both answers came back"
    follow_ups = [r for r in world.start.requests if r.input.text.startswith("[Background result]")]
    assert {f'handle="{corr}"' in f.input.text for f in follow_ups} == {True, False}
    assert len(follow_ups) == 2, "one follow-up per answer"


def test_stopping_a_running_child_cancels_it_and_nothing_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.driven.tools.background import StopResult

    world = _world(monkeypatch)
    world.start.b_hangs = True
    seen: dict[str, Any] = {}

    async def process() -> None:
        async with w.Dbos(_DATABASE):
            await turn_workflow.enqueue_turn(w.request(), turn_id=w.ASKING_TURN)
            await w.result_of(str(w.ASKING_TURN))
            (corr,) = w.rows(_DATABASE)
            assert await w.dispatch(world, w.B) is AskDisposition.STARTED
            await w.until(lambda: world.start.starts(w.B) == 1, "B to be running")
            seat = _seat(world)
            seen["stop"] = await seat.stop(_owner(), corr)
            seen["again"] = await seat.stop(_owner(), corr)
            await asyncio.sleep(0.5)
            seen["callee"] = await w.status(turn_workflow.peer_workflow_id(corr))
            seen["follow_up"] = await w.status(background_followup_turn_id(corr))
            seen["state"] = w.rows(_DATABASE)[corr][1]

    asyncio.run(process())

    assert seen["stop"] is StopResult.STOPPED and seen["again"] is StopResult.NOT_RUNNING
    assert seen["callee"] == "CANCELLED" and seen["state"] == "cancelled"
    assert seen["follow_up"] is None
