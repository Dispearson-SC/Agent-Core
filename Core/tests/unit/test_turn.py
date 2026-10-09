"""Turn domain invariants.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-01

Covers invariant I2 from `agent_core.domain.turn`: a `TurnOutcome` is SUSPENDED or
FINISHED, never both and never neither. The check belongs at construction because an
adapter producing a third shape must fail at the boundary, not three steps deep inside a
DBOS workflow where the traceback no longer names the culprit.
"""

import pytest

from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnResult,
)


def _pending() -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId("call-1"),
        tool_name="refund",
        arguments={"amount": 10},
        reason="over the auto-approval threshold",
    )


def test_suspended_outcome_is_valid() -> None:
    outcome = TurnOutcome(turn_id=TurnId("turn-1"), pending=(_pending(),))

    assert outcome.is_suspended is True
    assert outcome.result is None


def test_finished_outcome_is_valid() -> None:
    outcome = TurnOutcome(turn_id=TurnId("turn-1"), result=TurnResult(text="done"))

    assert outcome.is_suspended is False
    assert outcome.pending == ()


def test_both_pending_and_result_is_rejected() -> None:
    with pytest.raises(ValueError):
        TurnOutcome(
            turn_id=TurnId("turn-1"),
            pending=(_pending(),),
            result=TurnResult(text="done"),
        )


def test_neither_pending_nor_result_is_rejected() -> None:
    with pytest.raises(ValueError):
        TurnOutcome(turn_id=TurnId("turn-1"))
