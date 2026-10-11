"""The peer's turn as a durable workflow (C1): it answers the asker when it is done.

Subject: `turn_workflow.run_peer_turn_workflow`. Driven without DBOS, like `test_peer_fanout.py`:
the steps that touch the world are swapped for recorders and `DBOS.recv_async` replays a script.

What is defended:
  * the callee's turn finishing - including after ITS OWN human approval - records the answer on
    the mailbox row FIRST and wakes the asker SECOND (the reverse order wakes a turn onto an
    empty row);
  * a failing or closed callee turn still answers, with fixed text (never the exception);
  * the callee's turn is not delivered to a channel: nobody is on the other end of `peer`;
  * a vanished asker does not turn a recorded answer into a failed workflow.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import PEER_ASK_CLOSED_ANSWER, PEER_ASK_FAILED_ANSWER
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    UserInput,
)
from tests.fakes import fanout

ASKING_TURN = TurnId("asker-turn")
CORRELATION = "corr-9"


def _job() -> turn_workflow.PeerTurnJob:
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("peer~case~x"), tenant_id=TenantId("t")),
        caller=CallerIdentity(
            subject_id="peer-agent",
            channel="peer",
            tenant_id=TenantId("t"),
            roles=frozenset({"peer"}),
        ),
        profile_id=fanout.PEER,
        input=UserInput(text="how much?"),
        hop=1,
    )
    return turn_workflow.PeerTurnJob(
        request=request,
        correlation_id=CORRELATION,
        asking_turn_id=ASKING_TURN,
        answer_token="secret-token",
    )


def _approval() -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId("tc-freeze"),
        tool_name="freeze_account",
        arguments={},
        reason="a human must agree",
    )


def _finishes(text: str) -> Any:
    async def start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=text))

    return start


class _Recorder:
    def __init__(self, world: fanout.World) -> None:
        self.world = world
        self.signals: list[tuple[str, str]] = []
        self.delivered: list[Any] = []
        self.events: list[str] = []
        self.signal_error: Exception | None = None


def _setup(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Recorder:
    world = fanout.install(monkeypatch, (), kwargs.pop("script", []), **kwargs)
    recorder = _Recorder(world)

    async def signal(turn_id: TurnId, correlation_id: str) -> None:
        recorder.events.append("signal")
        if recorder.signal_error is not None:
            raise recorder.signal_error
        recorder.signals.append((str(turn_id), correlation_id))

    async def deliver(*args: Any, **_: Any) -> None:
        recorder.delivered.append(args)

    original = world.mailbox.answer

    async def answer(
        correlation_id: str, text: str, *, answer_token: str | None = None
    ) -> None:
        recorder.events.append("answer")
        await original(correlation_id, text, answer_token=answer_token)

    monkeypatch.setattr(world.mailbox, "answer", answer)
    monkeypatch.setattr(turn_workflow, "signal_peer_answer", signal)
    monkeypatch.setattr(turn_workflow, "_step_deliver", deliver)
    return recorder


def _run(job: turn_workflow.PeerTurnJob | None = None) -> TurnOutcome:
    body = inspect.unwrap(turn_workflow.run_peer_turn_workflow)
    return asyncio.run(body(job or _job()))


def test_a_finished_callee_turn_records_its_answer_then_wakes_the_asker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _setup(monkeypatch, start_step=_finishes("the balance is 40"))

    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "the balance is 40"
    assert recorder.world.mailbox.answer_calls == [(CORRELATION, "the balance is 40")]
    assert recorder.signals == [(str(ASKING_TURN), CORRELATION)]
    assert recorder.events == ["answer", "signal"], "record first, wake second"


def test_the_callee_turn_is_not_delivered_to_a_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _setup(monkeypatch, start_step=_finishes("x"))

    _run()

    assert recorder.delivered == [], "the 'peer' channel has no one to send to"


def test_a_callee_that_waits_for_a_human_answers_after_the_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def suspends(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, pending=(_approval(),))

    def approved(world: fanout.World) -> Any:
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"), tool_call_id=ToolCallId("tc-freeze"), approved=True
        )

    recorder = _setup(monkeypatch, start_step=suspends, script=[approved])
    recorder.world.resume.then = [
        TurnOutcome(turn_id=TurnId("turn-1"), result=TurnResult(text="frozen"))
    ]

    _run()

    assert recorder.world.mailbox.answer_calls == [(CORRELATION, "frozen")]
    assert recorder.signals == [(str(ASKING_TURN), CORRELATION)]


def test_a_failing_callee_turn_answers_with_fixed_text_and_still_fails_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        raise RuntimeError("secret internal detail")

    recorder = _setup(monkeypatch, start_step=boom)

    with pytest.raises(RuntimeError, match="secret internal detail"):
        _run()

    assert recorder.world.mailbox.answer_calls == [(CORRELATION, PEER_ASK_FAILED_ANSWER)]
    assert "secret" not in PEER_ASK_FAILED_ANSWER
    assert recorder.signals == [(str(ASKING_TURN), CORRELATION)]


def test_a_callee_turn_closed_without_an_answer_says_so_instead_of_inventing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def suspends(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, pending=(_approval(),))

    async def expire(turn_id: TurnId) -> TurnOutcome:
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text=turn_workflow._NO_ANSWER))

    recorder = _setup(monkeypatch, start_step=suspends, script=[fanout.nobody_answers(1)])
    monkeypatch.setattr(turn_workflow, "_step_expire", expire)

    _run()

    assert recorder.world.mailbox.answer_calls == [(CORRELATION, PEER_ASK_CLOSED_ANSWER)]
    assert recorder.signals == [(str(ASKING_TURN), CORRELATION)]


def test_an_asker_that_is_gone_does_not_fail_a_recorded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _setup(monkeypatch, start_step=_finishes("late"))
    recorder.signal_error = RuntimeError("no such workflow")

    outcome = _run()

    assert outcome.result is not None
    assert recorder.world.mailbox.answer_calls == [(CORRELATION, "late")]


def test_the_peer_workflow_id_is_derived_from_the_correlation_id() -> None:
    assert turn_workflow.peer_workflow_id("abc") == "peer-abc"


def test_the_workflow_presents_the_answer_token_when_it_records_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _setup(monkeypatch, start_step=_finishes("ok"))

    _run()

    assert recorder.world.mailbox.answer_tokens == ["secret-token"]


def test_a_refused_answer_does_not_wake_the_asker_or_fail_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row refused the answer (wrong token, or the ask was cancelled): nothing was
    recorded, so waking the asker would resume it on an empty row."""
    recorder = _setup(monkeypatch, start_step=_finishes("ok"))
    recorder.world.mailbox.refuse_answers = True

    _run()

    assert recorder.signals == []
