"""C19 (D49): `ask_peer(..., background=true)` starts a peer without waiting for it.

Subjects: domain/profile.py (`peers.allow_background`, `peers.max_background_asks`),
domain/peer_result.py (the `running` status and the `handle`), adapters/driven/tools/peers.py (the
parameter, offered only to a profile that opted in), adapters/driving/workflow/turn_workflow.py
(`_step_ask_peers`: the opt-in and the caps; `_collect_round` resuming at once).

How the answer comes back (a follow-up turn on the owner conversation) is
`tests/unit/test_peer_background_delivery.py`; the rows are the conformance suite's. The workflow
BODY is driven without DBOS, like `test_peer_fanout.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.tools.peers import ask_peer, build_toolset
from agent_core.domain.peer_result import PeerResult, PeerStatus
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile, ProfileValidationError
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import fanout
from tests.fakes import ports as fakes

# --- the profile switches ---------------------------------------------------------------------


def _profile_from(**peers: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "peers": {"enabled": True, **peers}}
    )


def test_background_asks_are_off_by_default_and_a_profile_can_switch_them_on() -> None:
    assert _profile_from().peers.allow_background is False
    assert _profile_from(allow_background=True).peers.allow_background is True


@pytest.mark.parametrize("bad", ["yes", "false", 1, 0, None])
def test_the_switch_must_be_a_real_boolean(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="allow_background"):
        _profile_from(allow_background=bad)


@pytest.mark.parametrize("bad", [0, -1, 1.5, "4", True])
def test_the_background_cap_must_be_a_positive_whole_number(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="max_background_asks"):
        _profile_from(max_background_asks=bad)


def test_the_switch_and_the_cap_are_part_of_the_content_hash() -> None:
    base = _profile_from().content_hash
    assert _profile_from(allow_background=True).content_hash != base
    assert _profile_from(max_background_asks=2).content_hash != base


# --- the result the model reads ---------------------------------------------------------------


def test_a_running_result_names_its_handle() -> None:
    text = PeerResult(
        peer="billing", status=PeerStatus.RUNNING, text="started", session="peer~s~k", handle="h-1"
    ).render()

    assert 'status="running"' in text and 'handle="h-1"' in text


def test_a_result_without_a_handle_has_no_handle_attribute() -> None:
    text = PeerResult(peer="billing", status=PeerStatus.OK, text="x").render()

    assert "handle" not in text


# --- the tool ---------------------------------------------------------------------------------


def test_the_tool_carries_the_background_flag_in_its_deferral() -> None:
    with pytest.raises(CallDeferred) as caught:
        ask_peer("billing", "slow question", background=True)

    assert caught.value.metadata == {
        "target": "billing",
        "question": "slow question",
        "background": True,
    }


def test_an_ordinary_ask_has_no_background_flag_in_its_deferral() -> None:
    with pytest.raises(CallDeferred) as caught:
        ask_peer("billing", "first question")

    assert "background" not in (caught.value.metadata or {})


def _offered_schema(*, allow: bool) -> dict[str, Any]:
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    world = fanout.profiles()
    world[fanout.ASKER] = _with_peers(world[fanout.ASKER], allow_background=allow)
    caller = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=caller,
        profile_id=fanout.ASKER,
        input=UserInput(text="hello"),
    )
    rule = PolicyRule(rule_id="a", tool_pattern="ask_peer", effect=Effect.ALLOW, reason="ok")
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (rule,))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        peer_profiles=world,
    )
    asyncio.run(runner.run(TurnId("t1"), request, world[fanout.ASKER], None))
    tool = {t.name: t for t in seen[0].function_tools}["ask_peer"]
    return dict(tool.parameters_json_schema["properties"])


def _with_peers(profile: AgentProfile, **changes: Any) -> AgentProfile:
    import dataclasses

    return dataclasses.replace(profile, peers=dataclasses.replace(profile.peers, **changes))


def test_a_profile_without_the_switch_is_not_offered_the_parameter() -> None:
    assert "background" not in _offered_schema(allow=False)


def test_a_profile_with_the_switch_is_offered_it() -> None:
    properties = _offered_schema(allow=True)

    assert "background" in properties
    assert "answer" in properties["background"]["description"]


# --- the workflow -----------------------------------------------------------------------------


def _background_ask(tool_call_id: str, background: object = True) -> Any:
    ask = fanout.ask(tool_call_id)
    return type(ask)(
        kind=ask.kind,
        tool_call_id=ask.tool_call_id,
        tool_name=ask.tool_name,
        arguments={**ask.arguments, "background": background},
        reason=ask.reason,
    )


def _run() -> Any:
    return asyncio.run(fanout.body()(fanout.request()))


def _result(world: fanout.World, tool_call_id: str) -> str:
    return str(dict(world.resume.calls[0])[tool_call_id])


def test_a_background_ask_returns_at_once_with_a_handle_and_the_turn_never_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The script answers the ask: a build that WAITED for it (the foreground path) would resume
    # on "ok". Background resumes on "running" without ever blocking on the durable topic.
    world = fanout.install(
        monkeypatch, (_background_ask("tc-1"),), [fanout.answer("corr-1", "late answer")]
    )
    fanout.asker_policy(allow_background=True)

    _run()

    envelope = _result(world, "tc-1")
    assert 'status="running"' in envelope and 'handle="corr-1"' in envelope
    assert world.recv_timeouts == [], "the turn did not wait for the peer"
    assert world.mailbox.asks == [("p-peer", "q-tc-1")]


def test_the_ask_is_sent_with_the_return_address_of_the_asking_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True)

    _run()

    (caller,) = world.mailbox.background_callers
    assert caller is not None
    assert (caller.subject_id, caller.channel, str(caller.tenant_id)) == ("u", "http", "t")


def test_an_ordinary_ask_in_the_same_step_still_waits_for_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_background_ask("tc-1"), fanout.ask("tc-2")),
        [fanout.answer("corr-2", "now")],
    )
    fanout.asker_policy(allow_background=True)

    _run()

    assert 'status="running"' in _result(world, "tc-1")
    assert 'status="ok"' in _result(world, "tc-2")
    assert world.mailbox.background_callers == [CALLER, None]


def test_a_profile_that_did_not_opt_in_is_refused_and_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])

    _run()

    assert world.mailbox.asks == []
    envelope = _result(world, "tc-1")
    assert 'status="refused"' in envelope and "background" in envelope


@pytest.mark.parametrize("bad", ["true", 1, ["x"]])
def test_a_flag_that_is_not_a_boolean_is_a_malformed_ask(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    world = fanout.install(monkeypatch, (_background_ask("tc-1", bad),), [])
    fanout.asker_policy(allow_background=True)

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in _result(world, "tc-1")


def test_background_asks_beyond_the_conversations_cap_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, tuple(_background_ask(f"tc-{n}") for n in (1, 2, 3)), [])
    fanout.asker_policy(allow_background=True, max_background_asks=2)

    _run()

    assert [s for s in ("tc-1", "tc-2", "tc-3") if 'status="running"' in _result(world, s)] == [
        "tc-1",
        "tc-2",
    ]
    assert 'status="refused"' in _result(world, "tc-3")
    assert len(world.mailbox.asks) == 2


def test_background_asks_already_running_in_the_conversation_count_toward_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True, max_background_asks=1)
    world.mailbox.open_background = 1  # one started by an earlier turn, still running

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in _result(world, "tc-1")


def test_the_cap_counts_every_open_ask_not_just_the_newest_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: the cap used to be summed over a listing of the newest 50, so a conversation with more
    open asks than that could always start another."""
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True, max_background_asks=100)
    world.mailbox.open_background = 100

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in _result(world, "tc-1")


def test_a_step_that_runs_again_does_not_count_its_own_earlier_asks_against_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: a step that crashed after the INSERT and runs again finds its own row open. With a cap
    of one it must still get its handle back, not a refusal of the ask it already made."""
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True, max_background_asks=1)

    _run()
    first = _result(world, "tc-1")
    world.resume.calls.clear()
    _run()

    assert 'status="running"' in first
    assert 'status="running"' in _result(world, "tc-1"), "the replay was refused by its own row"
    assert len(world.mailbox.asks) == 1, "and it is still ONE ask"


def test_a_follow_up_turn_may_not_start_the_next_background_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1o: the follow-up runs the asking profile at depth zero, so without this a model could keep
    a background -> follow-up -> background chain going for ever."""
    world = fanout.install(
        monkeypatch, (_background_ask("tc-1"), fanout.ask("tc-2")), [fanout.answer("corr-1", "now")]
    )
    fanout.asker_policy(allow_background=True)
    world.mailbox.followup_turns.add("turn-1")

    _run()

    envelope = _result(world, "tc-1")
    assert 'status="refused"' in envelope and "follow-up" in envelope
    assert 'status="ok"' in _result(world, "tc-2"), "an ordinary ask still waits for its answer"
    assert world.mailbox.asks == [("p-peer", "q-tc-2")]


def test_background_asks_share_the_per_step_parallel_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    world = fanout.install(
        monkeypatch,
        (_background_ask("tc-1"), fanout.ask("tc-2"), _background_ask("tc-3")),
        [fanout.answer("corr-2", "ok")],
    )
    fanout.asker_policy(allow_background=True, max_parallel_asks=2)

    _run()

    assert 'status="running"' in _result(world, "tc-1")
    assert 'status="ok"' in _result(world, "tc-2")
    assert 'status="refused"' in _result(world, "tc-3")


def test_a_mailbox_that_cannot_run_in_the_background_is_a_refused_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True)
    world.mailbox.refuse_background = True

    _run()

    assert 'status="refused"' in _result(world, "tc-1")


CALLER = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))


# --- the ownership boundary -------------------------------------------------------------------


def test_closing_the_conversation_cancels_its_background_asks_and_their_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.driving.workflow import turn_workflow

    world = fanout.install(
        monkeypatch,
        (_background_ask("tc-1"), _background_ask("tc-2"), fanout.ask("tc-3")),
        [fanout.answer("corr-3", "ok")],
    )
    fanout.asker_policy(allow_background=True)
    _run()
    world.cancelled_workflows.clear()

    ended = asyncio.run(
        turn_workflow.close_conversation(fanout.request().session, "conversation closed")
    )

    assert ended == ("corr-1", "corr-2"), "only the background asks; the ordinary one answered"
    assert set(world.mailbox.cancelled) == {"corr-1", "corr-2"}
    assert {turn_workflow.peer_workflow_id(c) for c in ("corr-1", "corr-2")} <= set(
        world.cancelled_workflows
    ), "and the callee workflows were cancelled with the rows"


def test_cancelling_the_asking_turn_leaves_its_background_asks_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.driving.workflow import turn_workflow

    world = fanout.install(monkeypatch, (_background_ask("tc-1"),), [])
    fanout.asker_policy(allow_background=True)
    _run()

    asyncio.run(turn_workflow.cancel_turn(turn_workflow.TurnId("turn-1"), "operator cancelled"))

    assert world.mailbox.cancelled == {}, "a background ask is the conversation's, not the turn's"


def test_the_background_batch_moved_the_pinned_application_version_to_nine() -> None:
    """D49 (C19/C20): the pickled `PeerDispatch` and `PeerOwner` gained fields and the peer
    workflow records `_step_background_follow_up`, so work in flight under .8 is stranded on
    purpose (drain first, OPERATIONS.md). One bump for the batch."""
    from agent_core import composition
    from agent_core.adapters.driving.workflow import turn_workflow

    assert int(composition.PINNED_DBOS_APPLICATION_VERSION.rsplit(".", 1)[1]) >= 9
    assert hasattr(turn_workflow, "_step_background_follow_up")
