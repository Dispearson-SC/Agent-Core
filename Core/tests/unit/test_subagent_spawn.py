"""C28 (D62): `spawn_subagent` through the turn workflow body, on the shared peer machinery.

Subject: adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`, `_collect_round`),
adapters/driven/peers/hop_limit.py (`authorise_spawn`). Driven without DBOS, like
`test_peer_fanout.py` (`tests/fakes/fanout.py`).

What is defended: two subagents spawned in one model step are both posted before the turn waits
(they run in parallel on their own queues) and the turn resumes ONCE with both results, each in the
C8 envelope marked `kind="subagent"`; aborting or timing out the parent cancels both; the gate
(the spawner's switch and allowlist, the spawned agent's mode) refuses with a result the model can
read and nothing is posted; a subagent shares the peers' caps, `fail_fast` and background rules.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.adapters.driving.workflow import turn_workflow
from agent_core.domain.agent_mode import AgentMode, DelegationKind
from agent_core.domain.turn import TurnId, TurnOutcome
from tests.fakes import fanout

SUB = DelegationKind.SUBAGENT


def _run() -> TurnOutcome:
    return asyncio.run(fanout.body()(fanout.request()))


def _subagent_world(
    monkeypatch: pytest.MonkeyPatch,
    pending: tuple[Any, ...],
    script: list[Any],
    **subagents: Any,
) -> fanout.World:
    world = fanout.install(monkeypatch, pending, script)
    fanout.asker_subagents(**subagents)
    fanout.callee_profile(mode=AgentMode.SUBAGENT)
    return world


def test_two_subagents_in_one_step_run_in_parallel_and_the_turn_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, int] = {}

    def first_wake(world: fanout.World) -> Any:
        seen["posted_before_the_first_wait"] = len(world.mailbox.asks)
        return fanout.answer("corr-1", "alpha")(world)

    world = _subagent_world(
        monkeypatch,
        (fanout.spawn("tc-1", task="a"), fanout.spawn("tc-2", task="b")),
        [first_wake, fanout.answer("corr-2", "beta")],
    )

    _run()

    assert seen["posted_before_the_first_wait"] == 2, "both were posted before the turn waited"
    assert world.mailbox.kinds == [SUB, SUB] and world.mailbox.depths == [1, 1]
    assert len(world.resume.calls) == 1, "one resume for the whole step"
    assert dict(world.resume.calls[0]) == {
        "tc-1": fanout.ok("corr-1", "alpha", kind=SUB),
        "tc-2": fanout.ok("corr-2", "beta", kind=SUB),
    }
    assert 'kind="subagent"' in str(dict(world.resume.calls[0])["tc-1"])


def test_the_task_is_the_whole_brief_and_no_conversation_context_travels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C18 lets a peer be told about the asker's conversation; a subagent gets its task only."""
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.spawn("tc-2", task="brief")),
        [fanout.answer("corr-1", "x"), fanout.answer("corr-2", "y")],
    )
    fanout.asker_subagents()
    fanout.callee_profile(mode=AgentMode.ALL)

    async def context(request: Any) -> str:
        return "ASKER-CONVERSATION"

    monkeypatch.setattr(turn_workflow, "_step_peer_context", context)

    _run()

    peer_question = world.mailbox.asks[0][1]
    subagent_question = world.mailbox.asks[1][1]
    assert "ASKER-CONVERSATION" in peer_question
    assert subagent_question == "brief-tc-2"


def test_a_timed_out_parent_cancels_every_subagent_it_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(
        monkeypatch,
        (fanout.spawn("tc-1"), fanout.spawn("tc-2")),
        [fanout.nobody_answers(fanout.REPLY_TIMEOUT)],
    )

    outcome = _run()

    assert outcome.result is not None
    assert sorted(world.mailbox.cancelled) == ["corr-1", "corr-2"]
    assert sorted(world.cancelled_workflows) == sorted(
        [turn_workflow.peer_workflow_id("corr-1"), turn_workflow.peer_workflow_id("corr-2")]
    )
    assert all('status="timeout"' in str(r) for _, r in world.resume.calls[0])


def test_aborting_the_parent_aborts_both_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1"), fanout.spawn("tc-2")), [])
    world.mailbox.turn_of.update({"corr-1": "turn-1", "corr-2": "turn-1"})

    cancelled = asyncio.run(turn_workflow.cancel_turn(TurnId("turn-1"), "operator abort"))

    assert cancelled == ("corr-1", "corr-2")
    assert world.cancelled_workflows == [
        "turn-1",
        turn_workflow.peer_workflow_id("corr-1"),
        turn_workflow.peer_workflow_id("corr-2"),
    ], "the parent first, then each child"


def test_fail_fast_cancels_the_sibling_subagent_when_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(
        monkeypatch,
        (fanout.spawn("tc-1"), fanout.spawn("tc-2")),
        [fanout.answer("corr-1", "boom", failed=True)],
    )
    fanout.asker_policy(fan_out=turn_workflow.FanOutPolicy.FAIL_FAST)

    _run()

    assert list(world.mailbox.cancelled) == ["corr-2"]
    results = dict(world.resume.calls[0])
    assert 'status="error"' in str(results["tc-1"])
    assert 'status="cancelled"' in str(results["tc-2"]) and 'kind="subagent"' in str(
        results["tc-2"]
    )


# --- the gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda: fanout.asker_subagents(enabled=False), "caller_subagents_disabled"),
        (lambda: fanout.asker_subagents(agents=()), "caller_does_not_allow_subagent"),
        (lambda: fanout.callee_profile(mode=AgentMode.PEER), "callee_mode_not_spawnable"),
        (lambda: fanout.callee_profile(mode=AgentMode.PRIMARY), "callee_mode_not_spawnable"),
    ],
)
def test_a_spawn_the_gate_refuses_is_a_readable_result_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, setup: Any, reason: str
) -> None:
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1"),), [])
    setup()

    _run()

    assert world.mailbox.asks == []
    text = str(dict(world.resume.calls[0])["tc-1"])
    assert 'status="refused"' in text and 'kind="subagent"' in text and reason in text


def test_an_unknown_agent_and_a_malformed_call_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = fanout.spawn("tc-2")
    broken = type(broken)(
        kind=broken.kind,
        tool_call_id=broken.tool_call_id,
        tool_name=broken.tool_name,
        arguments={"agent": fanout.PEER},  # no task
        reason=broken.reason,
    )
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1", agent="ghost"), broken), [])

    _run()

    results = dict(world.resume.calls[0])
    assert world.mailbox.asks == []
    assert all('status="refused"' in str(results[t]) for t in ("tc-1", "tc-2"))


def test_a_spawner_does_not_need_peers_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1"),), [fanout.answer("corr-1", "done")])
    fanout.asker_policy(enabled=False)

    _run()

    assert len(world.mailbox.asks) == 1, "spawning has its own switch"


# --- shared caps and background rules ---------------------------------------------------------


def test_the_parallel_cap_of_the_spawning_profile_applies_to_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(
        monkeypatch,
        tuple(fanout.spawn(f"tc-{n}") for n in (1, 2, 3)),
        [fanout.answer("corr-1", "a"), fanout.answer("corr-2", "b")],
    )
    fanout.asker_policy(max_parallel_asks=2)

    _run()

    assert len(world.mailbox.asks) == 2
    assert 'status="refused"' in str(dict(world.resume.calls[0])["tc-3"])


def test_a_background_spawn_returns_a_handle_and_a_cap_and_a_follow_up_refuse_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(
        monkeypatch,
        (
            fanout.spawn("tc-1", background=True),
            fanout.spawn("tc-2", background=True),
        ),
        [],
    )
    fanout.asker_policy(allow_background=True, max_background_asks=1)

    _run()

    results = dict(world.resume.calls[0])
    assert 'status="running"' in str(results["tc-1"]) and 'handle="corr-1"' in str(results["tc-1"])
    assert 'kind="subagent"' in str(results["tc-1"])
    assert 'status="refused"' in str(results["tc-2"]), "the cap of background asks is shared"
    assert world.mailbox.kinds == [SUB]


def test_a_follow_up_turn_may_not_spawn_further_background_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1", background=True),), [])
    fanout.asker_policy(allow_background=True)
    world.mailbox.followup_turns.add("turn-1")

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in str(dict(world.resume.calls[0])["tc-1"])


def test_background_is_refused_for_a_profile_that_did_not_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(monkeypatch, (fanout.spawn("tc-1", background=True),), [])

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in str(dict(world.resume.calls[0])["tc-1"])


def test_a_subagent_may_continue_its_earlier_session_when_the_profile_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _subagent_world(
        monkeypatch,
        (fanout.spawn("tc-1", continue_session="peer~s~earlier"),),
        [fanout.answer("corr-1", "again")],
    )
    fanout.asker_policy(allow_continuation=True)

    _run()

    assert world.mailbox.continued == ["peer~s~earlier"]
    assert world.mailbox.kinds == [SUB]


def test_a_peer_ask_in_the_same_step_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(
        monkeypatch,
        (fanout.ask("tc-1"), fanout.spawn("tc-2", agent="p-sub")),
        [fanout.answer("corr-1", "peer says"), fanout.answer("corr-2", "sub says")],
    )
    fanout.asker_subagents(agents=("p-sub",))  # type: ignore[arg-type]
    seat = turn_workflow._dependencies.peers  # noqa: SLF001
    assert seat is not None
    seat.profiles["p-sub"] = type(seat.profiles[fanout.PEER])(
        id="p-sub", persona="s", model="m", mode=AgentMode.SUBAGENT
    )

    _run()

    assert world.mailbox.kinds == [DelegationKind.PEER, SUB]
    results = dict(world.resume.calls[0])
    assert 'kind="subagent"' not in str(results["tc-1"]) and 'kind="subagent"' in str(
        results["tc-2"]
    )
