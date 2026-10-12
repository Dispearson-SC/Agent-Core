"""C6 (D32): a round that asks only PEOPLE is collected and resumed once, like every other.

Subject: `turn_workflow._collect_round` driven through the workflow body, without DBOS
(`tests/fakes/fanout.py`).

THE DEFECT. Pydantic AI continues a run that stopped on N deferred calls only when it is handed
results for all N. A round with several human requests and no peer ask still waited per answer
and resumed on the first decision, so a step that stopped on two approvals failed at the first
click (`UserError: Tool call results need to be provided for all deferred tool calls`). D28
fixed it for rounds with a peer ask; this is the rest.

What is defended:
  * two approvals of one model step resume ONCE, with both decisions, in `tool_call_id` order;
  * a person who has not decided by `THREE_DAYS` expires the turn, and nothing is resumed on the
    decisions that did arrive (no partial resume);
  * the wait is the explicit three days, never DBOS' 60-second default;
  * a duplicate decision does not resolve a second call; a note, including an uploaded file,
    reaches the resolution unchanged;
  * what the old per-answer step refused is still refused: a payload that is not a `HumanAnswer`
    of THIS turn, and a peer wake for a handle the turn never asked under.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.media import MediaId, MediaKind, MediaRef
from agent_core.domain.turn import PendingKind, PendingRequest, ToolCallId, TurnId
from tests.fakes import fanout


def _approval(tool_call_id: str) -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="freeze_account",
        arguments={},
        reason="a human must agree",
    )


def _decides(tool_call_id: str, *, approved: bool = True, note: Any = None) -> Any:
    def run(world: fanout.World) -> Any:
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"),
            tool_call_id=ToolCallId(tool_call_id),
            approved=approved,
            note=note,
        )

    return run


def _run() -> Any:
    return asyncio.run(fanout.body()(fanout.request()))


def test_two_approvals_of_one_step_resume_once_with_both_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_approval("tc-2"), _approval("tc-1")),
        [_decides("tc-2", note="second ok"), _decides("tc-1", approved=False, note="no")],
    )

    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "done"
    assert world.resume.calls == [(("tc-1", "no"), ("tc-2", "second ok"))], (
        "ONE resume, with every decision, in tool_call_id order and not in arrival order"
    )
    assert world.resume.approvals == [(("tc-1", False), ("tc-2", True))]


def test_one_decision_and_one_expiry_expires_the_turn_with_nothing_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_approval("tc-1"), _approval("tc-2")),
        [_decides("tc-1"), fanout.nobody_answers(turn_workflow.THREE_DAYS)],
    )

    outcome = _run()

    assert outcome.result is not None and outcome.result.text == "expired"
    assert world.resume.calls == [], "no partial resume on the decision that did arrive"


def test_a_single_human_request_still_resumes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(monkeypatch, (_approval("tc-1"),), [_decides("tc-1")])

    outcome = _run()

    assert outcome.result is not None
    assert world.resume.calls == [(("tc-1", None),)]


def test_the_wait_for_a_person_is_the_explicit_three_days(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(
        monkeypatch, (_approval("tc-1"), _approval("tc-2")), [_decides("tc-1"), _decides("tc-2")]
    )

    _run()

    assert world.recv_timeouts[0] == turn_workflow.THREE_DAYS, (
        "DBOS.recv defaults to 60 seconds; a person decides in hours"
    )
    assert all(timeout > 60 for timeout in world.recv_timeouts)


def test_a_duplicate_decision_does_not_resolve_a_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_approval("tc-1"), _approval("tc-2")),
        [_decides("tc-1"), _decides("tc-1", approved=False), _decides("tc-2")],
    )

    _run()

    assert world.resume.calls == [(("tc-1", None), ("tc-2", None))]
    assert world.resume.approvals == [(("tc-1", True), ("tc-2", True))], "the first decision stands"


def test_an_uploaded_file_reaches_the_resolution_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = MediaRef(
        media_id=MediaId("m-1"),
        kind=MediaKind.IMAGE,
        mime_type="image/png",
        size_bytes=3,
        sha256="0" * 64,
    )
    world = fanout.install(
        monkeypatch,
        (_approval("tc-1"), _approval("tc-2")),
        [_decides("tc-2"), _decides("tc-1", note=upload)],
    )

    _run()

    ((first, second),) = world.resume.calls
    assert first == ("tc-1", upload), "the file is the tool's result, passed through as it came"
    assert second == ("tc-2", None)


# --- what the old per-answer step refused is still refused ---------------------------------------


def test_a_decision_addressed_to_another_turn_is_refused_and_nothing_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def other_turn(world: fanout.World) -> Any:
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("another-turn"), tool_call_id=ToolCallId("tc-1"), approved=True
        )

    world = fanout.install(monkeypatch, (_approval("tc-1"),), [other_turn])

    with pytest.raises(turn_workflow.UnusableResumeAnswerError):
        _run()

    assert world.resume.calls == []


def test_a_payload_that_is_no_answer_at_all_is_refused_and_nothing_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_approval("tc-1"),),
        [lambda world: {"tool_call_id": "tc-1", "approved": True}],
    )

    with pytest.raises(turn_workflow.UnusableResumeAnswerError):
        _run()

    assert world.resume.calls == []


def test_a_peer_wake_for_a_handle_this_turn_never_asked_under_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stranger(world: fanout.World) -> Any:
        return turn_workflow.PeerAnswer(turn_id=TurnId("turn-1"), correlation_id="never-asked")

    world = fanout.install(monkeypatch, (_approval("tc-1"),), [stranger])

    with pytest.raises(turn_workflow.UnusableResumeAnswerError):
        _run()

    assert world.resume.calls == []
