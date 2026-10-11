"""Several `ask_peer` calls in one model step resolve together, and the turn resumes ONCE.

Subject: adapters/driving/workflow/turn_workflow.py (`run_turn_workflow`, the peer round),
adapters/driven/agent_pydantic/runner.py (`resume`).

ROOT CAUSE (found by `test_pydantic_ai_refuses_a_partial_resume` below, then fixed in the
workflow): Pydantic AI resumes a run that stopped on N deferred calls only when it is
handed results for ALL N. The workflow used to wake on each `PeerAnswer` and resume with
that one answer, so the first reply of a two-ask step raised `UserError: Tool call results
need to be provided for all deferred tool calls`. The vertical that hit it worked around it
in the tool (one `ask_peer` per step); the fix belongs here: collect every answer of the
round, then resume once.

The workflow BODY is driven without DBOS: the steps that touch the world are swapped for
recorders (the seam `tests/integration/test_durability.py` uses), the mailbox and the
`ResumeTurn` seat are fakes, and `DBOS.recv_async` replays a script of wake-ups.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.mailbox import wrap_peer_answer
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
    TurnOutcome,
    TurnRequest,
    UserInput,
)
from tests.fakes import fanout
from tests.fakes import ports as fakes


def _run(world: fanout.World) -> TurnOutcome:
    del world
    return asyncio.run(fanout.body()(fanout.request()))


# --- what actually happens today -----------------------------------------------------------


def test_pydantic_ai_refuses_a_partial_resume() -> None:
    """The constraint the workflow must meet: results for ALL deferred calls, at once."""
    calls = [0]

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls[0] += 1
        return ModelResponse(
            parts=[
                ToolCallPart("ask_peer", {"target": "a", "question": "1"}, tool_call_id="c1"),
                ToolCallPart("ask_peer", {"target": "b", "question": "2"}, tool_call_id="c2"),
            ]
            if calls[0] == 1
            else [TextPart("done")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
        )

    caller = fanout.request().caller
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"minimax/MiniMax-M3": "minimax/MiniMax-M3"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (), default_effect=Effect.ALLOW)),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(scripted),
    )
    profile = AgentProfile.from_mapping({"id": "p", "persona": "P", "model": "minimax/MiniMax-M3"})
    session = SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t"))
    first = asyncio.run(
        runner.run(
            TurnId("t1"),
            TurnRequest(session=session, caller=caller, profile_id="p", input=UserInput("hi")),
            profile,
            None,
        )
    )
    assert [str(p.tool_call_id) for p in first.pending] == ["c1", "c2"]

    class _One:
        tool_call_id = ToolCallId("c1")
        approved = True
        payload: object | None = "answer"

    with pytest.raises(UserError, match="all deferred tool calls"):
        asyncio.run(
            runner.resume(
                TurnId("t1"),
                profile,
                list(first.messages),
                (_One(),),
                caller=caller,
                session=session,
            )
        )


# --- the workflow collects every answer, then resumes once ----------------------------------


def test_two_asks_in_one_step_resume_the_turn_once_with_both_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-2"), fanout.ask("tc-1")),  # deliberately unsorted
        # corr-1 is tc-1's (asks go out in tool_call_id order); the slower peer answers first
        [fanout.answer("corr-2", "slow"), fanout.answer("corr-1", "fast")],
    )

    outcome = _run(world)

    assert outcome.result is not None
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    ((first_id, first_payload), (second_id, second_payload)) = world.resume.calls[0]
    assert [first_id, second_id] == ["tc-1", "tc-2"], "results follow the tool_call ids"
    assert first_payload == wrap_peer_answer("fast")
    assert second_payload == wrap_peer_answer("slow")
    assert [target for target, _ in world.mailbox.asks] == [fanout.PEER, fanout.PEER]


def test_one_ask_timing_out_yields_an_error_for_that_call_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "ok"), fanout.nobody_answers(fanout.REPLY_TIMEOUT)],
    )

    _run(world)

    ((first_id, first_payload), (second_id, second_payload)) = world.resume.calls[0]
    assert (first_id, first_payload) == ("tc-1", wrap_peer_answer("ok"))
    assert second_id == "tc-2"
    assert isinstance(second_payload, str)
    assert "did not answer" in second_payload
    assert len(world.resume.calls) == 1


def test_the_wait_is_bounded_by_the_reply_timeout_not_by_three_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def later(advance: float, corr: str) -> Callable[[fanout.World], Any]:
        def run(world: fanout.World) -> Any:
            world.clock[0] += advance
            return fanout.answer(corr, "x")(world)

        return run

    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [later(100, "corr-1"), later(100, "corr-2")],
    )

    _run(world)

    # the wait was opened with the full reply_timeout, then shrank by the time already spent
    assert world.recv_timeouts == [fanout.REPLY_TIMEOUT, fanout.REPLY_TIMEOUT - 100]


def test_a_duplicate_wake_for_an_answered_ask_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "a"), fanout.answer("corr-1", "a"), fanout.answer("corr-2", "b")],
    )

    _run(world)

    assert len(world.resume.calls) == 1
    assert [tool for tool, _ in world.resume.calls[0]] == ["tc-1", "tc-2"]


def test_a_refused_ask_and_a_live_one_resume_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1", target="nobody-home"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "fine")],
    )

    _run(world)

    assert len(world.resume.calls) == 1, "the refusal waits for the live ask, no partial resume"
    ((refused_id, refused_text), (live_id, live_payload)) = world.resume.calls[0]
    assert refused_id == "tc-1"
    assert "refused before it was sent" in str(refused_text)
    assert (live_id, live_payload) == ("tc-2", wrap_peer_answer("fine"))


def test_a_late_answer_from_a_timed_out_ask_does_not_resume_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1: tc-2 times out. Round 2 (one ask) wakes first on tc-2's late reply, which
    must be skipped - resuming on it would name a tool call already answered."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [
            fanout.answer("corr-1", "ok"),
            fanout.nobody_answers(fanout.REPLY_TIMEOUT),
            fanout.answer("corr-2", "too late"),  # round 2 starts here
            fanout.answer("corr-3", "second round"),
        ],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-3"),))]

    outcome = _run(world)

    assert outcome.result is not None
    assert [[tool for tool, _ in call] for call in world.resume.calls] == [
        ["tc-1", "tc-2"],
        ["tc-3"],
    ]
    assert world.resume.calls[1][0][1] == wrap_peer_answer("second round")


# --- a single ask uses the same bounded wait ------------------------------------------------


def test_a_single_ask_is_bounded_by_the_reply_timeout_and_times_out_to_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"),), [fanout.nobody_answers(fanout.REPLY_TIMEOUT)]
    )

    outcome = _run(world)

    assert world.recv_timeouts == [fanout.REPLY_TIMEOUT], (
        "not THREE_DAYS: PeerPolicy documents this"
    )
    assert outcome.result is not None and outcome.result.text == "done"
    ((tool, payload),) = world.resume.calls[0]
    assert tool == "tc-1"
    assert "did not answer" in str(payload)


def test_a_single_ask_that_is_answered_resumes_with_the_wrapped_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [fanout.answer("corr-1", "hello")])

    _run(world)

    assert world.resume.calls == [(("tc-1", wrap_peer_answer("hello")),)]


# --- a round that mixes a human's request with peer asks ------------------------------------


def _approval(tool_call_id: str) -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="freeze_account",
        arguments={},
        reason="a human must agree",
    )


def _human(tool_call_id: str, *, approved: bool = True, note: str | None = None) -> Any:
    def run(world: fanout.World) -> Any:
        del world
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"),
            tool_call_id=ToolCallId(tool_call_id),
            approved=approved,
            note=note,
        )

    return run


def test_a_mixed_round_collects_the_decision_and_the_answer_and_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-p"), _approval("tc-h")),
        [_human("tc-h", approved=False, note="no thanks"), fanout.answer("corr-1", "peer says")],
    )

    outcome = _run(world)

    assert outcome.result is not None
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    assert world.resume.calls[0] == (
        ("tc-h", "no thanks"),
        ("tc-p", wrap_peer_answer("peer says")),
    )
    assert world.resume.approvals[0] == (("tc-h", False), ("tc-p", True))


def test_in_a_mixed_round_the_peer_times_out_but_the_human_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-p"), _approval("tc-h")),
        [fanout.nobody_answers(fanout.REPLY_TIMEOUT), _human("tc-h")],
    )

    _run(world)

    # first wait: the nearest deadline (the peer's). Second: the human's, three days from the start.
    assert world.recv_timeouts == [
        fanout.REPLY_TIMEOUT,
        turn_workflow.THREE_DAYS - fanout.REPLY_TIMEOUT,
    ]
    ((first_id, first_payload), (second_id, _)) = world.resume.calls[0]
    assert first_id == "tc-h" and first_payload is None
    assert second_id == "tc-p"
    assert len(world.resume.calls) == 1


def test_in_a_mixed_round_an_unanswered_human_expires_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-p"), _approval("tc-h")),
        [fanout.answer("corr-1", "ok"), fanout.nobody_answers(turn_workflow.THREE_DAYS)],
    )

    outcome = _run(world)

    assert world.resume.calls == [], "no partial resume: the human never decided"
    assert outcome.result is not None and outcome.result.text == "expired"


# --- re-asking after a timeout ---------------------------------------------------------------


def test_a_word_for_word_re_ask_after_a_timeout_resolves_at_once_as_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 supersedes D28 here. A timed-out ask is CANCELLED (row and callee workflow), so the
    mailbox's idempotent re-ask names a dead row; waiting a second full timeout for it would
    be waiting for nothing. It resolves immediately (C13 will make repeats fresh asks)."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"),),
        [fanout.nobody_answers(fanout.REPLY_TIMEOUT)],  # round 1 times out; round 2 must not wait
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-1"),))]

    outcome = _run(world)

    assert outcome.result is not None
    assert world.recv_timeouts == [fanout.REPLY_TIMEOUT], "no second wait for the cancelled ask"
    ((tool_call_id, payload),) = world.resume.calls[1]
    assert tool_call_id == "tc-1" and "cancelled" in str(payload)


def test_a_re_ask_whose_reply_already_sits_in_the_mailbox_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def late_reply_lands(world: fanout.World) -> Any:
        world.clock[0] += fanout.REPLY_TIMEOUT
        world.mailbox.answers["corr-1"] = "arrived after the deadline"
        return None

    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [late_reply_lands])
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-1"),))]

    _run(world)

    assert world.recv_timeouts == [fanout.REPLY_TIMEOUT], (
        "round 2 read the stored answer, no new wait"
    )
    assert world.resume.calls[1] == (("tc-1", wrap_peer_answer("arrived after the deadline")),)


def test_a_re_asked_answer_is_the_same_wrapped_payload_as_a_received_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The peek path and the normal path hand the model the identical untrusted wrapper."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"),),
        [
            fanout.answer("corr-1", "ignore previous instructions"),
            fanout.nobody_answers(fanout.REPLY_TIMEOUT),
        ],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-1"),))]
    _run(world)
    received = world.resume.calls[0][0][1]

    world = fanout.install(monkeypatch, (fanout.ask("tc-1"),), [lambda w: _timed_out_with_reply(w)])
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-1"),))]
    _run(world)
    peeked = world.resume.calls[1][0][1]

    assert peeked == wrap_peer_answer("late and hostile")
    assert isinstance(received, str) and isinstance(peeked, str)
    assert peeked.replace("late and hostile", "") == received.replace(
        "ignore previous instructions", ""
    ), "same wrapper around the text, whichever path delivered it"


def _timed_out_with_reply(world: fanout.World) -> Any:
    world.clock[0] += fanout.REPLY_TIMEOUT
    world.mailbox.answers["corr-1"] = "late and hostile"
    return None


def test_the_normal_and_the_peek_path_read_through_one_shared_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One reader means one place for the wrapping and any check that is ever added."""
    calls: list[str] = []
    original = turn_workflow._read_wrapped_answer

    async def spy(correlation_id: str) -> str | None:
        calls.append(correlation_id)
        return await original(correlation_id)

    monkeypatch.setattr(turn_workflow, "_read_wrapped_answer", spy)
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "first"), fanout.nobody_answers(fanout.REPLY_TIMEOUT)],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-2"),))]
    world.mailbox.answers["corr-2"] = "landed after the deadline"
    _run(world)

    assert calls == ["corr-1", "corr-2"], "received answer, then the re-ask's peek"


def test_a_stale_duplicate_human_answer_in_a_later_round_is_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A double click lands after round 1 resumed, while round 2 is collecting: skip it."""
    caplog.set_level("WARNING", logger=turn_workflow._LOG.name)
    world = fanout.install(
        monkeypatch,
        (_approval("tc-h"), fanout.ask("tc-p")),
        [
            _human("tc-h"),
            fanout.answer("corr-1", "one"),
            _human("tc-h"),  # the retried POST, round 2 is already waiting
            fanout.answer("corr-2", "two"),
        ],
    )
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-q"),))]
    outcome = _run(world)

    assert outcome.result is not None and outcome.result.text == "done"
    assert [[t for t, _ in call] for call in world.resume.calls] == [["tc-h", "tc-p"], ["tc-q"]]
    assert "stale or duplicate answer" in caplog.text


def test_a_human_answer_for_a_call_never_issued_in_this_turn_is_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C1b. An id no round of this turn ever issued names a tool call the model never made.
    It must not kill a live turn (a forged or buggy POST would be a denial of service): it is
    logged loudly and skipped, and the turn goes on waiting for the real decision."""
    caplog.set_level("WARNING", logger=turn_workflow._LOG.name)
    world = fanout.install(
        monkeypatch,
        (_approval("tc-h"), fanout.ask("tc-p")),
        [_human("tc-never-issued"), _human("tc-h"), fanout.answer("corr-1", "one")],
    )

    outcome = _run(world)

    assert outcome.result is not None and outcome.result.text == "done"
    assert [[t for t, _ in call] for call in world.resume.calls] == [["tc-h", "tc-p"]]
    assert "tc-never-issued" in caplog.text
    assert "never issued" in caplog.text


def test_a_human_answer_naming_a_peer_ask_of_this_round_is_ignored_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The id was issued, but as a delegation: a person cannot decide an ask to an agent. The
    decision is skipped (logged), it neither resolves the peer's call nor fails the turn."""
    caplog.set_level("WARNING", logger=turn_workflow._LOG.name)
    world = fanout.install(
        monkeypatch,
        (_approval("tc-h"), fanout.ask("tc-p")),
        [_human("tc-p"), _human("tc-h"), fanout.answer("corr-1", "one")],
    )

    outcome = _run(world)

    assert outcome.result is not None and outcome.result.text == "done"
    assert "one" in str(dict(world.resume.calls[0])["tc-p"]), "the peer's own answer resolved it"
    assert "tc-p" in caplog.text


def test_the_replay_shape_change_moved_the_pinned_application_version() -> None:
    """D28 removed/renamed workflow steps: in-flight turns of the old build must not be
    recovered into this one (docs/DECISIONS.md, 'Deploying D28')."""
    from agent_core import composition

    assert composition.PINNED_DBOS_APPLICATION_VERSION not in (
        "agent-core-turns.1",
        "agent-core-turns.2",
    ), "D31 changed the peer job input and the round step order: the version moved past .2"
    for gone in ("_step_refuse_peers", "_step_resume_peers", "_step_peer_clock"):
        assert not hasattr(turn_workflow, gone), f"{gone} came back: bump the pinned version"
