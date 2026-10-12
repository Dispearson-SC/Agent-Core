"""C4: cancel and expire cascade (D31). Driven without DBOS, like `test_peer_fanout.py`.

What is defended:
  * a peer ask that times out in a round is cancelled (its row and its callee workflow) and ONLY
    that ask; an answered ask in the same round is untouched;
  * an asking turn that expires with peer asks still outstanding cancels them all;
  * `cancel_turn` cancels the asking workflow and every open ask it made;
  * the cancel is a recorded step (replay-safe) that never fails the turn when the callee
    workflow is already gone.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.turn import TurnId
from tests.fakes import fanout


def _callee(correlation_id: str) -> str:
    """The id of the workflow that answers this ask (a UUID since D33)."""
    return turn_workflow.peer_workflow_id(correlation_id)

def _run() -> Any:
    return asyncio.run(fanout.body()(fanout.request()))


def _approval(tool_call_id: str) -> Any:
    from agent_core.domain.turn import PendingKind, PendingRequest, ToolCallId

    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="freeze",
        arguments={},
        reason="needs a human",
    )


def test_a_peer_timeout_cancels_only_the_ask_that_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "ok"), fanout.nobody_answers(fanout.REPLY_TIMEOUT)],
    )

    _run()

    assert list(world.mailbox.cancelled) == ["corr-2"], "the answered ask is left alone"
    assert world.cancelled_workflows == [_callee("corr-2")], "its callee workflow is cancelled"
    assert "reply timeout" in world.mailbox.cancelled["corr-2"]


def test_an_asker_that_expires_cancels_every_outstanding_peer_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed round: two peers (one queued, one running, from the asker's side both just
    'awaiting') and a person who never decides. The turn expires: nothing may keep running."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2"), _approval("tc-h")),
        [fanout.nobody_answers(turn_workflow.THREE_DAYS)],
    )
    # The clock jumps straight to the person's three-day deadline, which is later than the peers'.
    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "expired"
    assert sorted(world.mailbox.cancelled) == ["corr-1", "corr-2"]
    assert sorted(world.cancelled_workflows) == sorted(
        [_callee("corr-1"), _callee("corr-2")]
    )
    assert all("expired" in reason for reason in world.mailbox.cancelled.values())


def test_a_cancelled_ask_refuses_a_late_answer_and_wakes_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.nobody_answers(fanout.REPLY_TIMEOUT)]
    )
    _run()

    from agent_core.domain.peers import PeerAnswerRefusedError

    with pytest.raises(PeerAnswerRefusedError):
        asyncio.run(world.mailbox.answer("corr-1", "too late"))
    assert "corr-1" not in world.mailbox.answers, "nothing was written"


def test_a_workflow_that_is_already_gone_does_not_fail_the_cancel_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.nobody_answers(fanout.REPLY_TIMEOUT)]
    )

    async def boom(workflow_id: str, **_: Any) -> None:
        raise RuntimeError("no such workflow")

    monkeypatch.setattr(turn_workflow.DBOS, "cancel_workflow_async", boom)

    outcome = _run()

    assert outcome.result is not None, "the turn went on"
    assert list(world.mailbox.cancelled) == ["corr-1"], "the row is cancelled regardless"


def test_cancel_turn_cancels_the_asking_workflow_then_its_open_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [])
    world.mailbox.turn_of.update({"corr-a": "asker-1", "corr-b": "asker-1", "corr-c": "other"})
    world.mailbox.answers["corr-b"] = "already answered"

    cancelled = asyncio.run(turn_workflow.cancel_turn(TurnId("asker-1"), "operator request"))

    assert cancelled == ("corr-a",), "an answered ask is history; another turn's ask is not ours"
    assert world.cancelled_workflows == ["asker-1", _callee("corr-a")], "asker first, then callees"
    assert world.mailbox.cancelled == {"corr-a": "operator request"}


def test_cancelling_a_callee_cascades_to_the_asks_the_callee_made_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1f. A -> B -> C: when A gives up on B, B's own open ask to C must not keep running.
    B's turn id is its workflow id `peer-corr-1`, and the asks it made are filed under it."""
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.nobody_answers(fanout.REPLY_TIMEOUT)]
    )
    world.mailbox.turn_of["corr-b-to-c"] = _callee("corr-1")
    world.mailbox.turn_of["corr-c-to-d"] = _callee("corr-b-to-c")

    _run()

    assert sorted(world.mailbox.cancelled) == ["corr-1", "corr-b-to-c", "corr-c-to-d"]
    assert world.cancelled_workflows == [
        _callee("corr-1"),
        _callee("corr-b-to-c"),
        _callee("corr-c-to-d"),
    ], "every level down the chain is cancelled, nearest first"


def test_the_cascade_stops_at_an_ask_that_was_already_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.nobody_answers(fanout.REPLY_TIMEOUT)]
    )
    world.mailbox.turn_of["corr-b-to-c"] = _callee("corr-1")
    world.mailbox.answers["corr-b-to-c"] = "C already answered"

    _run()

    assert list(world.mailbox.cancelled) == ["corr-1"], "an answered ask is history"
