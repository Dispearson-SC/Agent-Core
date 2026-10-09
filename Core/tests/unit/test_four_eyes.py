"""Four-eyes: the approver must not be the human who asked.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-05, docs/TASKS.md#t-f3-14
Decision: docs/DECISIONS.md#d25

Fakes only: no database, no network, no model, no DBOS. The requester lookup is an
injected awaitable - the same seat arrangement, for the same reason, as `DecisionSignal`
in this use case already: `application/` may not reach for a repository, and a narrow
callable gives this file nothing it could accidentally wait on.

WHAT THIS MODULE PINS, AND WHY EACH HALF IS HERE
    D25 rules four ways, and a test asserting only the first would certify a rule that is
    half a rule:

    1. Same person, `approved=True`  -> refused, recorded as a REJECTED ATTEMPT (never
       as a human decision), nothing woken.
    2. Different person, same request -> accepted. This is the OPPOSITE of 1, and without
       it the module would still pass if `execute` refused every approval on earth.
    3. Same person, `approved=False`  -> accepted. Four-eyes stops a human GRANTING
       themselves a permission; cancelling your own request grants nothing, and blocking
       it strands the turn until it times out.
    4. Requester unknown -> refused. An approver who cannot be SHOWN to be a second
       person is not a second person.

    Identity is compared on `subject_id` alone, case- and whitespace-insensitively. See
    D25 for why that direction is the safe one: two genuinely distinct subjects differing
    only by case would be an identity-provider defect, while a self-approval slipping
    through on `U-1` vs `u-1 ` is exactly the failure this rule exists to prevent.

`FakeHumanGateway` is duplicated from test_decide_approval.py rather than shared, because
`tests/fakes/ports.py` still carries `HumanGateway` as an F3 TODO and neither anchor owns
that file - the same arrangement that module already documents.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.application.decide_approval import (
    SELF_APPROVAL,
    UNKNOWN_REQUESTER,
    DecideApproval,
    FourEyesError,
    UnknownCorrelationError,
)
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

REQUESTER = "u-alice"
SECOND_PERSON = "u-bob"


class FakeHumanGateway:
    """Publishes nowhere; correlates from a fixed table."""

    def __init__(self, table: dict[str, tuple[TurnId, ToolCallId]]) -> None:
        self._table = table

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:  # pragma: no cover - not the subject of this module
        raise NotImplementedError

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        return self._table.get(correlation_id)


class RecordingSignal:
    """The wake-up, instrumented. A refused decision must never reach it."""

    def __init__(self) -> None:
        self.calls: list[tuple[TurnId, ToolCallId, bool, str | None]] = []

    async def __call__(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        approved: bool,
        note: str | None,
    ) -> None:
        self.calls.append((turn_id, tool_call_id, approved, note))


class RecordingRequesterLookup:
    """Answers who started the turn. `answer=None` is the 'nobody on record' case, which
    D25 treats as a refusal rather than as a pass."""

    def __init__(self, answer: str | None) -> None:
        self._answer = answer
        self.turns: list[TurnId] = []

    async def __call__(self, turn_id: TurnId) -> str | None:
        self.turns.append(turn_id)
        return self._answer


def _use_case(
    audit: FakeAuditSink,
    signal: RecordingSignal,
    lookup: RecordingRequesterLookup,
) -> DecideApproval:
    return DecideApproval(
        gateway=FakeHumanGateway({CORRELATION: (TURN, CALL)}),
        audit=audit,
        signal=signal,
        requester=lookup,
    )


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_the_requester_cannot_approve_their_own_request() -> None:
    """D25, the rule itself, plus the row it leaves (t-f3-14).

    The refusal is filed through `record_rejected_decision` and never through
    `record_human_decision`: that member means 'a human decided', and a refused attempt
    recorded as `approved=False` would read in the trail as a refusal the human never
    made. Asserting on the KIND is what keeps the two apart - an assertion about the
    payload alone would pass either way, which is the one distinction the two members
    exist to hold.

    The grounds are asserted verbatim because they are the half of the row that answers
    the question it is read for, and the note is absent because an audit row is grounds,
    not a payload dump."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(REQUESTER)

    with pytest.raises(FourEyesError):
        asyncio.run(
            _use_case(audit, signal, lookup).execute(
                CORRELATION, REQUESTER, approved=True, note="mine, and I like it"
            )
        )

    assert lookup.turns == [TURN]
    assert [call.kind for call in audit.calls] == ["rejected_decision"]
    assert audit.calls[0].payload == (TURN, CALL, REQUESTER, SELF_APPROVAL)
    assert signal.calls == []


@pytest.mark.phase("F3")
def test_a_second_person_approving_the_same_request_is_recorded_and_woken() -> None:
    """The OPPOSITE of the rule. Without this the module would pass just as happily
    against an `execute` that refused every approval ever made."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(REQUESTER)

    resolved = asyncio.run(
        _use_case(audit, signal, lookup).execute(
            CORRELATION, SECOND_PERSON, approved=True, note="checked the invoice"
        )
    )

    assert resolved == (TURN, CALL)
    assert audit.calls[0].payload == (
        TURN,
        CALL,
        SECOND_PERSON,
        True,
        "checked the invoice",
    )
    assert signal.calls == [(TURN, CALL, True, "checked the invoice")]


@pytest.mark.phase("F3")
def test_the_requester_may_refuse_their_own_request() -> None:
    """Four-eyes gates GRANTING, not cancelling. Refusing your own request removes a
    permission rather than conferring one, and blocking it would leave the turn asleep
    until it expires with the one person who wants it stopped unable to stop it."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(REQUESTER)

    resolved = asyncio.run(
        _use_case(audit, signal, lookup).execute(
            CORRELATION, REQUESTER, approved=False, note="wrong account, cancel it"
        )
    )

    assert resolved == (TURN, CALL)
    assert audit.calls[0].payload == (
        TURN,
        CALL,
        REQUESTER,
        False,
        "wrong account, cancel it",
    )
    assert signal.calls == [(TURN, CALL, False, "wrong account, cancel it")]


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_an_unknown_requester_fails_closed() -> None:
    """Nobody on record for this turn. The approver may well be a second person, but
    nothing here can show it, and 'probably fine' is how a control becomes decoration.

    Its grounds differ from the self-approval path's: both refusals reach the same
    outcome, but a reader months later is asking WHICH control fired, and one shared
    string answers that with 'four-eyes, somehow'."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(None)

    with pytest.raises(FourEyesError):
        asyncio.run(
            _use_case(audit, signal, lookup).execute(
                CORRELATION, SECOND_PERSON, approved=True, note=None
            )
        )

    assert [call.kind for call in audit.calls] == ["rejected_decision"]
    assert audit.calls[0].payload == (TURN, CALL, SECOND_PERSON, UNKNOWN_REQUESTER)
    assert signal.calls == []


@pytest.mark.phase("F3")
@pytest.mark.silent
@pytest.mark.parametrize("disguise", ["U-ALICE", " u-alice ", "U-Alice\n"])
def test_case_and_padding_do_not_make_a_second_person(disguise: str) -> None:
    """The comparison is on `subject_id`, casefolded and stripped. An exact-match rule is
    defeated by whichever spelling of their own id the requester can get a channel to
    send, and the cost of over-matching is one identity provider that issues subjects
    differing only by case - which is a defect there, not a decision here."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(REQUESTER)

    with pytest.raises(FourEyesError):
        asyncio.run(
            _use_case(audit, signal, lookup).execute(
                CORRELATION, disguise, approved=True, note=None
            )
        )

    assert [call.kind for call in audit.calls] == ["rejected_decision"]
    assert audit.calls[0].payload == (TURN, CALL, disguise, SELF_APPROVAL)
    assert signal.calls == []


@pytest.mark.phase("F3")
def test_an_unknown_handle_is_still_a_404_and_never_consults_the_requester() -> None:
    """Ordering: correlation first. A stray reply must not be turned into a lookup against
    a turn id nobody resolved, and `UnknownCorrelationError` must not be shadowed by the
    new one - the HTTP adapter maps them to different statuses."""
    audit = FakeAuditSink()
    signal = RecordingSignal()
    lookup = RecordingRequesterLookup(REQUESTER)
    use_case = DecideApproval(
        gateway=FakeHumanGateway({}), audit=audit, signal=signal, requester=lookup
    )

    with pytest.raises(UnknownCorrelationError):
        asyncio.run(use_case.execute("c-stray", REQUESTER, approved=True, note=None))

    assert lookup.turns == []
    assert audit.calls == []
    assert signal.calls == []


@pytest.mark.phase("F3")
@pytest.mark.silent
def test_the_seat_is_optional_only_so_that_leaving_it_empty_is_visible() -> None:
    """The transitional state, pinned deliberately rather than left to be discovered.

    Nothing constructs `DecideApproval` in `composition.py` yet, so the seat has a default
    and an unwired use case cannot evaluate the rule. That is a DEPLOYMENT defect, not a
    permitted mode, and D25 says so. It is asserted here for one reason: an unenforced
    rule that nobody wrote a test for looks exactly like an enforced one from the outside.
    """
    audit = FakeAuditSink()
    signal = RecordingSignal()
    use_case = DecideApproval(
        gateway=FakeHumanGateway({CORRELATION: (TURN, CALL)}), audit=audit, signal=signal
    )

    resolved = asyncio.run(
        use_case.execute(CORRELATION, REQUESTER, approved=True, note=None)
    )

    assert resolved == (TURN, CALL)
    assert [call.kind for call in audit.calls] == ["human_decision"]
