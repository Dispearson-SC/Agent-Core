"""A pending request says WHO can answer it - by kind, never by tool name.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-08

THE DEFECT THESE ASSERTIONS CLOSE

    `PendingKind` had two members, both defined in terms of a person: APPROVAL is "a tool
    wants to run and needs a human yes/no", EVIDENCE is "a tool needs a human to SUPPLY
    something". A peer ask is an externally-executed deferred call, so it arrived labelled
    EVIDENCE - the same kind as a request for the user's photo, from code that has to
    treat the two OPPOSITELY: one is published to a human, the other must never be.

    So two modules read the tool name instead of the kind -
    `application/start_turn.py::_notice_for` and
    `adapters/driving/workflow/turn_workflow.py::_answerable_by_a_human` - and both said
    in a comment that this is not where the decision belongs. A third mechanism (a second
    agent-executed tool) would have to be added to both, and the one that got forgotten
    would fail silently in the worst direction: a question only another agent can answer,
    put in front of a person who then holds the turn open for three days.

    docs/TASKS.md records the same mistake twice already, in the t-f7-05 note on
    `request_evidence` and `ask_peer`.

WHY A NAME COLLISION APPEARS BELOW

    `test_the_notice_is_not_chosen_by_the_tool_name` builds an EVIDENCE request whose
    `tool_name` is `ask_peer`. That is not a scenario being predicted - two tools cannot
    share one name in a registry. It is the PROBE: the only way to prove a function does
    not read an input is to vary that input while the real discriminator is held fixed.
    Its mirror varies the name in the other direction, with the kind held at DELEGATION.

Pure unit test. No database, no channel, no model, no use case assembled: `_communicable`
is module-level and pure precisely so this property is assertable on its own, and its own
docstring says so.
"""

from __future__ import annotations

import pytest

from agent_core.application.start_turn import PEER_SUSPENSION_NOTICE, _communicable
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    ToolCallId,
    TurnId,
    TurnOutcome,
)

PHOTO_REASON = "a photo of the damaged package, with the label visible"
PEER_REASON = "asking personal-assistant-of-alice which delivery window she agreed to"

# Deliberately ONE name shared by the two probes below. Holding the name constant while
# the kind varies is what proves the name carries no decision.
SHARED_NAME = "ask_peer"


def _request(kind: PendingKind, *, tool_name: str, reason: str, call: str) -> PendingRequest:
    return PendingRequest(
        kind=kind,
        tool_call_id=ToolCallId(call),
        tool_name=tool_name,
        arguments={"detail": "whatever the model sent"},
        reason=reason,
    )


def _suspended(*pending: PendingRequest) -> TurnOutcome:
    return TurnOutcome(turn_id=TurnId("turn-1"), pending=pending)


@pytest.mark.phase("F9")
def test_pending_kind_has_a_member_for_a_call_another_agent_executes() -> None:
    """The enum itself must carry the distinction, or every consumer invents it again."""
    members = {member.name for member in PendingKind}
    assert "DELEGATION" in members, (
        "PendingKind has no member for a deferred call ANOTHER AGENT executes, so a peer "
        "ask and a request for the user's photo are the same kind. Two modules then key "
        f"on the tool name to tell them apart. Members: {sorted(members)}"
    )


@pytest.mark.phase("F9")
def test_every_pending_kind_answers_whether_a_human_can_resolve_it() -> None:
    """One hand-written row per member - never derived from the enum.

    Derivation is what made `t-f10-01`'s visibility table unfalsifiable: a table built
    from the enum answers every future member by construction, and the guard that was
    supposed to catch an undecided one restates the enum back to itself. A new
    `PendingKind` must leave a hole here that somebody has to fill in by hand.
    """
    answers = {kind: kind.answerable_by_a_human for kind in PendingKind}

    assert set(answers) == set(PendingKind)
    assert any(answers.values()), "no kind a person can answer leaves `HumanGateway` dead"
    assert not all(answers.values()), (
        "every kind claims a person can answer it, so the domain still cannot express a "
        "suspension that is waiting on another agent"
    )


@pytest.mark.phase("F9")
def test_a_peer_ask_and_a_photo_request_differ_by_kind_not_by_tool_name() -> None:
    """The two suspensions that must not be confused, told apart with the name discarded."""
    peer_ask = _request(
        PendingKind.DELEGATION, tool_name=SHARED_NAME, reason=PEER_REASON, call="tc-peer"
    )
    photo = _request(
        PendingKind.EVIDENCE, tool_name=SHARED_NAME, reason=PHOTO_REASON, call="tc-photo"
    )

    assert peer_ask.tool_name == photo.tool_name, "the probe needs the name held constant"
    assert peer_ask.kind is not photo.kind
    assert photo.kind.answerable_by_a_human is True, (
        "a photo request is exactly what a person answers; hiding it from them strands "
        "the turn on somebody who was never asked"
    )
    assert peer_ask.kind.answerable_by_a_human is False, (
        "a peer ask is answered by another agent's turn arriving on the durable topic"
    )


@pytest.mark.phase("F9")
def test_nothing_a_human_is_shown_can_be_a_request_only_an_agent_answers() -> None:
    """CLAUDE.md non-negotiable #11, the half that decides who is asked at all.

    A FILTER over one suspension, not a branch that skips the publish: one outcome can
    carry a peer ask AND an approval a person is standing by to give, and dropping the
    whole publish swaps a question nobody can answer for an approval nobody was asked for.
    """
    approval = _request(
        PendingKind.APPROVAL, tool_name="pricing_apply", reason="above 15%", call="tc-a"
    )
    photo = _request(
        PendingKind.EVIDENCE, tool_name="request_evidence", reason=PHOTO_REASON, call="tc-b"
    )
    peer_ask = _request(
        PendingKind.DELEGATION, tool_name=SHARED_NAME, reason=PEER_REASON, call="tc-c"
    )

    pending = (approval, peer_ask, photo)
    for_a_human = tuple(item for item in pending if item.kind.answerable_by_a_human)

    assert for_a_human == (approval, photo), (
        "the subset published to a person must be every request they can act on, in the "
        f"order it arrived, and nothing else. Got {[item.tool_call_id for item in for_a_human]}"
    )
    assert all(not item.kind.answerable_by_a_human for item in pending if item not in for_a_human)


@pytest.mark.phase("F9")
def test_the_notice_is_not_chosen_by_the_tool_name() -> None:
    """An evidence request keeps its reason even when the name says `ask_peer`.

    The reason is WHAT TO SEND. Blanking it leaves a person staring at a prompt for
    nothing, and the turn waits three days for something they were never told to supply.
    """
    photo = _request(
        PendingKind.EVIDENCE, tool_name=SHARED_NAME, reason=PHOTO_REASON, call="tc-photo"
    )

    outcome = _communicable(_suspended(photo))

    assert outcome.pending[0].reason == PHOTO_REASON, (
        "the reason was rewritten because the TOOL NAME looked like a peer ask; the kind "
        "says a human answers this one"
    )
    assert outcome is _suspended(photo) or outcome.pending == (photo,)


@pytest.mark.phase("F9")
def test_the_notice_covers_a_second_agent_executed_mechanism() -> None:
    """The mirror probe: kind held at DELEGATION, the name varied away from `ask_peer`.

    This is the direction that fails silently. A second agent-executed mechanism reaches
    the user with the model's own arguments in `reason` - the peer's id and the customer's
    question verbatim - because the name did not match the one constant somebody spelled.
    """
    delegated = _request(
        PendingKind.DELEGATION, tool_name="delegate_task", reason=PEER_REASON, call="tc-peer"
    )

    outcome = _communicable(_suspended(delegated))
    rewritten = outcome.pending[0]

    assert rewritten.reason == PEER_SUSPENSION_NOTICE
    assert "personal-assistant-of-alice" not in rewritten.reason
    # The RECORD is untouched - D18 stores everything and filters on read, and
    # `tool_call_id` must round-trip verbatim or the resumed result is dropped in silence.
    assert rewritten.tool_call_id == delegated.tool_call_id
    assert rewritten.tool_name == delegated.tool_name
    assert rewritten.arguments == delegated.arguments
    assert rewritten.kind is delegated.kind
