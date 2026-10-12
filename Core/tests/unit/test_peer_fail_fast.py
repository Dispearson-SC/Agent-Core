"""C10 (D39): the fan-out policy `peers.fan_out: all_settled | fail_fast`.

Subject: adapters/driving/workflow/turn_workflow.py (`_collect_round`),
domain/peers.py (`FanOutPolicy`), domain/profile.py (`peers.fan_out`).

`all_settled` (default) is what the workflow always did: every ask of a model step runs to its own
end and the turn resumes once with all of them. `fail_fast` cancels the siblings still out as soon
as one ask ENDS IN ERROR (the C4 cancel: row and callee workflow) and resumes once.

The workflow BODY is driven without DBOS, like `test_peer_fanout.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.peers import PEER_ASK_FAILED_ANSWER, FanOutPolicy
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import PendingKind, PendingRequest, ToolCallId, TurnId, TurnOutcome
from tests.fakes import fanout


def _run() -> TurnOutcome:
    return asyncio.run(fanout.body()(fanout.request()))


def _failed(corr: str) -> Any:
    return fanout.answer(corr, PEER_ASK_FAILED_ANSWER, failed=True)


def _statuses(world: fanout.World, call: int = 0) -> dict[str, str]:
    result: dict[str, str] = {}
    for tool_call_id, payload in world.resume.calls[call]:
        text = str(payload)
        result[tool_call_id] = text.split('status="', 1)[1].split('"', 1)[0]
    return result


def test_all_settled_is_the_default_and_waits_for_every_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [_failed("corr-1"), fanout.answer("corr-2", "fine")],
    )

    _run()

    assert len(world.resume.calls) == 1
    assert _statuses(world) == {"tc-1": "error", "tc-2": "ok"}
    assert world.mailbox.cancelled == {}, "nothing was cancelled"
    assert len(world.recv_timeouts) == 2, "it waited for the second ask"


def test_fail_fast_cancels_the_sibling_of_a_failed_ask_and_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch, (fanout.ask("tc-1"), fanout.ask("tc-2")), [_failed("corr-1")]
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)

    outcome = _run()

    assert outcome.result is not None
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    assert _statuses(world) == {"tc-1": "error", "tc-2": "cancelled"}
    assert list(world.mailbox.cancelled) == ["corr-2"], "the failed ask itself is not cancelled"
    assert "fail_fast" in world.mailbox.cancelled["corr-2"]
    assert len(world.recv_timeouts) == 1, "it did not wait for the cancelled sibling"
    cancelled_text = str(dict(world.resume.calls[0])["tc-2"])
    assert "tc-1" not in cancelled_text and fanout.PEER in cancelled_text


def test_fail_fast_cancels_the_callee_workflows_of_the_siblings_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2"), fanout.ask("tc-3")),
        [_failed("corr-2")],
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)

    _run()

    assert _statuses(world) == {"tc-1": "cancelled", "tc-2": "error", "tc-3": "cancelled"}
    assert sorted(world.mailbox.cancelled) == ["corr-1", "corr-3"]
    expected = {turn_workflow.peer_workflow_id(corr, 0) for corr in ("corr-1", "corr-3")}
    assert expected <= set(world.cancelled_workflows), "the C4 cascade reached the engine"


def test_fail_fast_does_not_trip_on_a_good_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "one"), fanout.answer("corr-2", "two")],
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)

    _run()

    assert _statuses(world) == {"tc-1": "ok", "tc-2": "ok"}
    assert world.mailbox.cancelled == {}


def test_fail_fast_does_not_trip_on_a_refused_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal never left (policy, unknown peer, the parallel cap): the live siblings are
    already on their way and the model asked for them knowingly."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1", target="nobody-home"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "fine")],
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)

    _run()

    assert _statuses(world) == {"tc-1": "refused", "tc-2": "ok"}
    assert world.mailbox.cancelled == {}


def test_a_late_answer_from_a_fail_fast_sibling_does_not_resume_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.ask("tc-2")),
        [
            _failed("corr-1"),
            fanout.answer("corr-2", "too late"),  # round 2 starts here: must be skipped
            fanout.answer("corr-3", "second round"),
        ],
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)
    world.resume.then = [TurnOutcome(turn_id=TurnId("turn-1"), pending=(fanout.ask("tc-3"),))]

    outcome = _run()

    assert outcome.result is not None
    assert [[tool for tool, _ in call] for call in world.resume.calls] == [
        ["tc-1", "tc-2"],
        ["tc-3"],
    ]
    assert world.resume.calls[1][0][1] == fanout.ok("corr-3", "second round")


def test_fail_fast_still_waits_for_a_person_deciding_in_the_same_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval = PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId("tc-h"),
        tool_name="freeze_account",
        arguments={},
        reason="a human must agree",
    )

    def decide(world: fanout.World) -> Any:
        del world
        return turn_workflow.HumanAnswer(
            turn_id=TurnId("turn-1"), tool_call_id=ToolCallId("tc-h"), approved=True, note=None
        )

    world = fanout.install(
        monkeypatch,
        (approval, fanout.ask("tc-1"), fanout.ask("tc-2")),
        [_failed("corr-1"), decide],
    )
    fanout.asker_policy(fan_out=FanOutPolicy.FAIL_FAST)

    _run()

    assert len(world.resume.calls) == 1
    assert [tool for tool, _ in world.resume.calls[0]] == ["tc-1", "tc-2", "tc-h"]
    assert list(world.mailbox.cancelled) == ["corr-2"]


def test_the_policy_is_read_through_a_step_of_its_own() -> None:
    """The policy is read by a step function, not from the live profile in the workflow body, so
    that DBOS can record it (CLAUDE.md #2). This only pins the structure; that a replay after a
    profile edit takes the first run's path is DBOS's recording, which the body-only tests here do
    not run."""
    assert hasattr(turn_workflow, "_step_peer_fan_out")


# --- the profile surface ---------------------------------------------------------------------


def _profile(**peers: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "peers": {"enabled": True, **peers}}
    )


def test_a_profile_defaults_to_all_settled_and_accepts_fail_fast() -> None:
    assert _profile().peers.fan_out is FanOutPolicy.ALL_SETTLED
    assert _profile(fan_out="fail_fast").peers.fan_out is FanOutPolicy.FAIL_FAST


def test_an_unknown_fan_out_value_refuses_to_load() -> None:
    with pytest.raises(ProfileValidationError, match="fan_out"):
        _profile(fan_out="best_effort")


def test_fan_out_is_part_of_the_content_hash() -> None:
    assert _profile().content_hash != _profile(fan_out="fail_fast").content_hash
