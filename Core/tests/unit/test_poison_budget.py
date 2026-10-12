"""The poison-turn budget (C5, D32): a turn that keeps killing its worker stops being retried.

DBOS re-runs a pending workflow's body after every crash. The body counts its own entries in a
durable ledger and, past `max_attempts`, fails the turn visibly instead of crash-looping. Driven
without DBOS (`tests/fakes/fanout.py`); a "crash" is a `BaseException` out of the body, which is
what a killed process looks like to the next run.

What is defended:
  * below the budget the turn simply continues, however often it was restarted;
  * past it the turn fails with a reason, and none of its steps run again;
  * a restart out of a wait is NOT a crash: a turn parked on a person for weeks survives any
    number of deploys, while a crash AFTER the wake counts;
  * a peer workflow past the budget answers its asker with the fixed failure text, wakes it, and
    cancels the asks it made itself.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import PEER_ASK_FAILED_ANSWER
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
from tests.fakes.attempt_ledger import InMemoryAttemptLedger

BUDGET = 3


class _Died(BaseException):
    """The process was killed. A BaseException so no `except Exception` in the body sees it."""



def _callee(correlation_id: str) -> str:
    """The id of the workflow that answers this ask (a UUID since D33)."""
    return turn_workflow.peer_workflow_id(correlation_id)

def _run() -> TurnOutcome:
    return asyncio.run(fanout.body()(fanout.request()))


def _crashes_in_start(times: int) -> Any:
    calls = {"n": 0}

    async def start(turn_id: TurnId, request: TurnRequest) -> TurnOutcome:
        calls["n"] += 1
        if calls["n"] <= times:
            raise _Died
        return TurnOutcome(turn_id=turn_id, result=TurnResult(text="finally"))

    start.calls = calls  # type: ignore[attr-defined]
    return start


def _approval(tool_call_id: str = "tc-h") -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="freeze",
        arguments={},
        reason="needs a human",
    )


def test_a_turn_restarted_below_the_budget_just_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = InMemoryAttemptLedger()
    start = _crashes_in_start(BUDGET - 1)
    fanout.install(monkeypatch, (), [], start_step=start, ledger=ledger, max_attempts=BUDGET)

    for _ in range(BUDGET - 1):
        with pytest.raises(_Died):
            _run()
    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "finally"
    assert ledger.begun == ["turn-1"] * BUDGET
    assert "turn-1" not in ledger.attempts, "a finished turn forgets its count"


def test_a_turn_that_keeps_crashing_fails_visibly_past_the_budget_and_runs_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryAttemptLedger()
    start = _crashes_in_start(10_000)
    fanout.install(monkeypatch, (), [], start_step=start, ledger=ledger, max_attempts=BUDGET)

    for _ in range(BUDGET):
        with pytest.raises(_Died):
            _run()
    with pytest.raises(turn_workflow.PoisonTurnError) as failed:
        _run()

    assert "turn-1" in str(failed.value)
    assert f"{BUDGET + 1} attempts" in str(failed.value) and f"budget of {BUDGET}" in str(
        failed.value
    )
    assert start.calls["n"] == BUDGET, "the poisoned run executed no step"


def test_a_restart_out_of_a_wait_is_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parked on a person, the process is restarted far more often than the budget allows."""
    ledger = InMemoryAttemptLedger()

    def killed_while_waiting(world: fanout.World) -> Any:
        raise _Died

    def approved(world: fanout.World) -> Any:
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"), tool_call_id=ToolCallId("tc-h"), approved=True
        )

    restarts = BUDGET * 4
    fanout.install(
        monkeypatch,
        (_approval(),),
        [killed_while_waiting] * restarts + [approved],
        ledger=ledger,
        max_attempts=BUDGET,
    )

    for _ in range(restarts):
        with pytest.raises(_Died):
            _run()
    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "done"


def test_a_crash_after_the_wake_counts_even_though_the_turn_had_waited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryAttemptLedger()

    def approved(world: fanout.World) -> Any:
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"), tool_call_id=ToolCallId("tc-h"), approved=True
        )

    class _ResumeDies:
        async def execute(self, *args: Any, **kwargs: Any) -> TurnOutcome:
            raise _Died

    fanout.install(
        monkeypatch,
        (_approval(),),
        [approved] * (BUDGET + 2),
        ledger=ledger,
        max_attempts=BUDGET,
        resume_seat=_ResumeDies(),
    )

    for _ in range(BUDGET):
        with pytest.raises(_Died):
            _run()
    with pytest.raises(turn_workflow.PoisonTurnError):
        _run()


def test_a_poisoned_turn_cancels_the_peer_asks_it_still_has_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryAttemptLedger()
    world = fanout.install(
        monkeypatch, (), [], start_step=_crashes_in_start(10_000), ledger=ledger, max_attempts=1
    )
    world.mailbox.turn_of["corr-open"] = "turn-1"

    with pytest.raises(_Died):
        _run()
    with pytest.raises(turn_workflow.PoisonTurnError):
        _run()

    assert list(world.mailbox.cancelled) == ["corr-open"]
    assert world.cancelled_workflows == [_callee("corr-open")]


# --- the peer workflow ---------------------------------------------------------------------------


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
        correlation_id="corr-9",
        asking_turn_id=TurnId("asker-turn"),
        answer_token="secret-token",
    )


def test_a_peer_workflow_past_the_budget_answers_its_asker_with_the_fixed_failure_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryAttemptLedger()
    world = fanout.install(
        monkeypatch,
        (),
        [],
        start_step=_crashes_in_start(10_000),
        ledger=ledger,
        max_attempts=BUDGET,
    )
    woken: list[tuple[str, str]] = []

    async def signal(turn_id: TurnId, correlation_id: str) -> None:
        woken.append((str(turn_id), correlation_id))

    monkeypatch.setattr(turn_workflow, "signal_peer_answer", signal)
    body = inspect.unwrap(turn_workflow.run_peer_turn_workflow)
    world.mailbox.turn_of["corr-nested"] = "turn-1"

    for _ in range(BUDGET):
        with pytest.raises(_Died):
            asyncio.run(body(_job()))
    with pytest.raises(turn_workflow.PoisonTurnError):
        asyncio.run(body(_job()))

    assert world.mailbox.answer_calls == [("corr-9", PEER_ASK_FAILED_ANSWER)]
    assert world.mailbox.answer_tokens == ["secret-token"]
    assert woken == [("asker-turn", "corr-9")]
    assert list(world.mailbox.cancelled) == ["corr-nested"], "and its own asks stop"
    assert "secret" not in PEER_ASK_FAILED_ANSWER


def test_a_workflow_without_a_ledger_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    fanout.install(monkeypatch, (), [], start_step=_crashes_in_start(5), max_attempts=1)

    for _ in range(5):
        with pytest.raises(_Died):
            _run()

    assert _run().result is not None
