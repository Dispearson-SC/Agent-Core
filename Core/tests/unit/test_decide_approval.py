"""DecideApproval - the recording must outlive the notification.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-03

Fakes only: no database, no network, no model, and above all no DBOS. The wake-up is an
injected awaitable, so this use case can be driven to the exact failure that matters -
the signal blowing up - without a workflow engine anywhere near the test.

The two properties this module exists to pin, and why an integration test cannot pin them:

1. ORDER. `record_human_decision` must have COMPLETED before the signal is emitted. Asserted
   from INSIDE the signal, by snapshotting what the audit sink already holds at the moment
   it is called - the only vantage point from which "before" is a fact rather than an
   inference from two call lists. Asserting that both happened passes on the wrong order,
   and the wrong order is the whole bug: signal first and the tool can run before anything
   records who authorised it, which is precisely the question an auditor asks first.

2. A FAILED SIGNAL STILL LEAVES THE DECISION RECORDED. Same reasoning as the DENY path in
   `PolicyEnforcement.before_tool_execute`, which writes the audit row before it raises: a
   record written on the far side of a failure is a record that is missing exactly when
   something went wrong. A decision that vanishes because a notification failed is a
   decision nobody can prove was made, and CLAUDE.md non-negotiable #6 says a failed turn
   must still leave a trace.

`FakeHumanGateway` is declared here rather than in tests/fakes/ports.py because that file
still carries it as an F3 TODO and this task does not own it - same arrangement as
`FakeContextEngine` in test_start_turn.py.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.application.decide_approval import DecideApproval, UnknownCorrelationError
from agent_core.domain.turn import (
    PendingRequest,
    SessionRef,
    ToolCallId,
    TurnId,
)
from tests.fakes.ports import FakeAuditSink

TURN = TurnId("t-1")
CALL = ToolCallId("call_abc123")
CORRELATION = "c-unguessable"
SUBJECT = "u-approver"


class FakeHumanGateway:
    """Publishes nowhere; correlates from a fixed table. `correlate` returns None for an
    unknown handle because the port says a miss is a VALUE, not an exception."""

    def __init__(self, table: dict[str, tuple[TurnId, ToolCallId]]) -> None:
        self._table = table
        self.published: list[tuple[TurnId, SessionRef, tuple[PendingRequest, ...]]] = []

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        self.published.append((turn_id, session, requests))

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return self._table.get(correlation_id)


class RecordingSignal:
    """The wake-up, instrumented. Snapshots the audit sink's contents AT THE MOMENT it is
    called - that snapshot is the ordering evidence, and it cannot be reconstructed after
    the fact from two separate call lists."""

    def __init__(self, audit: FakeAuditSink, *, fails: bool = False) -> None:
        self._audit = audit
        self._fails = fails
        self.calls: list[tuple[TurnId, ToolCallId, bool, str | None]] = []
        self.audit_kinds_at_call: tuple[str, ...] | None = None

    async def __call__(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        approved: bool,
        note: str | None,
    ) -> None:
        self.audit_kinds_at_call = tuple(call.kind for call in self._audit.calls)
        self.calls.append((turn_id, tool_call_id, approved, note))
        if self._fails:
            raise RuntimeError("the workflow signal failed")


def _gateway(*, known: bool = True) -> FakeHumanGateway:
    return FakeHumanGateway({CORRELATION: (TURN, CALL)} if known else {})


@pytest.mark.phase("F3")
def test_the_audit_row_exists_before_the_signal_is_emitted() -> None:
    """Property 1. Snapshotted from inside the signal, so 'before' is observed, not inferred."""
    audit = FakeAuditSink()
    signal = RecordingSignal(audit)
    use_case = DecideApproval(gateway=_gateway(), audit=audit, signal=signal)

    resolved = asyncio.run(
        use_case.execute(CORRELATION, SUBJECT, approved=True, note="looks legitimate")
    )

    assert signal.audit_kinds_at_call == ("human_decision",)
    assert signal.calls == [(TURN, CALL, True, "looks legitimate")]
    assert resolved == (TURN, CALL)
    assert audit.calls[0].payload == (TURN, CALL, SUBJECT, True, "looks legitimate")


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_a_failed_signal_still_leaves_the_decision_recorded() -> None:
    """Property 2. The signal raises; the record must survive it, and the caller must hear
    about the failure rather than be told the turn was woken when it was not."""
    audit = FakeAuditSink()
    signal = RecordingSignal(audit, fails=True)
    use_case = DecideApproval(gateway=_gateway(), audit=audit, signal=signal)

    with pytest.raises(RuntimeError):
        asyncio.run(use_case.execute(CORRELATION, SUBJECT, approved=False, note="not this one"))

    assert [call.kind for call in audit.calls] == ["human_decision"]
    assert audit.calls[0].payload == (TURN, CALL, SUBJECT, False, "not this one")


@pytest.mark.phase("F3")
def test_an_unknown_handle_records_nothing_and_signals_nothing() -> None:
    """A stray reply must not be guessed into a turn. Nothing recorded, nothing woken -
    an audit row filed against a turn nobody decided is worse than no row at all."""
    audit = FakeAuditSink()
    signal = RecordingSignal(audit)
    use_case = DecideApproval(gateway=_gateway(known=False), audit=audit, signal=signal)

    with pytest.raises(UnknownCorrelationError):
        asyncio.run(use_case.execute(CORRELATION, SUBJECT, approved=True, note=None))

    assert audit.calls == []
    assert signal.calls == []
