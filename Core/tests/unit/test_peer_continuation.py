"""C12 (D45): `ask_peer(target, question, continue_session?)`.

Subjects: adapters/driven/tools/peers.py (the parameter, offered only to a profile that opted in),
adapters/driving/workflow/turn_workflow.py (`_step_ask_peers`: the opt-in, the refusal, the session
the envelope names), domain/profile.py (`peers.allow_continuation`).

The ownership rules themselves (same tenant, case, asking agent and target) are the mailbox's and
are pinned by the conformance suite. The workflow BODY is driven without DBOS, like
`test_peer_fanout.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.tools.peers import ask_peer, build_toolset
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
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

# --- the profile switch -----------------------------------------------------------------------


def _profile_from(**peers: object) -> AgentProfile:
    return AgentProfile.from_mapping(
        {"id": "p", "persona": "P", "model": "m", "peers": {"enabled": True, **peers}}
    )


def test_continuation_is_off_by_default_and_a_profile_can_switch_it_on() -> None:
    assert _profile_from().peers.allow_continuation is False
    assert _profile_from(allow_continuation=True).peers.allow_continuation is True


@pytest.mark.parametrize("bad", ["yes", "false", 1, 0, None])
def test_the_switch_must_be_a_real_boolean(bad: object) -> None:
    with pytest.raises(ProfileValidationError, match="allow_continuation"):
        _profile_from(allow_continuation=bad)


def test_the_switch_is_part_of_the_content_hash() -> None:
    assert _profile_from(allow_continuation=True).content_hash != _profile_from().content_hash


# --- the tool ---------------------------------------------------------------------------------


def test_the_tool_carries_the_session_to_continue_in_its_deferral() -> None:
    with pytest.raises(CallDeferred) as caught:
        ask_peer("billing", "and the second invoice?", "peer~case~key")

    assert caught.value.metadata == {
        "target": "billing",
        "question": "and the second invoice?",
        "continue_session": "peer~case~key",
    }


def test_an_ordinary_ask_has_no_session_in_its_deferral() -> None:
    with pytest.raises(CallDeferred) as caught:
        ask_peer("billing", "first question")

    assert "continue_session" not in (caught.value.metadata or {})


def _world(*, allow: bool) -> Mapping[str, AgentProfile]:
    def side(me: str, other: str, **kw: Any) -> AgentProfile:
        return AgentProfile(
            id=me,
            persona=me,
            model="m",
            peers=PeerPolicy(
                enabled=True,
                peers=(AgentRef(agent_id=AgentId(other), display_name=other),),
                **kw,
            ),
        )

    return {
        "asker": side("asker", "billing", allow_continuation=allow),
        "billing": side("billing", "asker"),
    }


def _offered_schema(*, allow: bool, with_directory: bool = True) -> dict[str, Any]:
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    world = _world(allow=allow)
    caller = CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t"))
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=caller,
        profile_id="asker",
        input=UserInput(text="hello"),
    )
    rule = PolicyRule(rule_id="a", tool_pattern="ask_peer", effect=Effect.ALLOW, reason="ok")
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (rule,))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        peer_profiles=world if with_directory else None,
    )
    asyncio.run(runner.run(TurnId("t1"), request, world["asker"], None))
    tool = {t.name: t for t in seen[0].function_tools}["ask_peer"]
    return dict(tool.parameters_json_schema["properties"])


def test_a_profile_without_the_switch_is_not_offered_the_parameter() -> None:
    assert "continue_session" not in _offered_schema(allow=False)
    assert "continue_session" not in _offered_schema(allow=False, with_directory=False)


def test_a_profile_with_the_switch_is_offered_it() -> None:
    properties = _offered_schema(allow=True)

    assert "continue_session" in properties
    assert "session" in properties["continue_session"]["description"]


# --- the workflow -----------------------------------------------------------------------------


def _ask_continuing(tool_call_id: str, session: object) -> Any:
    ask = fanout.ask(tool_call_id)
    return type(ask)(
        kind=ask.kind,
        tool_call_id=ask.tool_call_id,
        tool_name=ask.tool_name,
        arguments={**ask.arguments, "continue_session": session},
        reason=ask.reason,
    )


def _run() -> Any:
    return asyncio.run(fanout.body()(fanout.request()))


def test_an_opted_in_asker_continues_a_session_and_the_envelope_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = "peer~s~1111"
    world = fanout.install(
        monkeypatch, (_ask_continuing("tc-1", session),), [fanout.answer("corr-1", "as before")]
    )
    fanout.asker_policy(allow_continuation=True)

    _run()

    assert world.mailbox.continued == [session]
    envelope = str(dict(world.resume.calls[0])["tc-1"])
    assert f'session="{session}"' in envelope and 'status="ok"' in envelope


def test_an_asker_that_did_not_opt_in_is_refused_and_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(monkeypatch, (_ask_continuing("tc-1", "peer~s~1111"),), [])

    _run()

    assert world.mailbox.asks == []
    envelope = str(dict(world.resume.calls[0])["tc-1"])
    assert 'status="refused"' in envelope and "continue" in envelope


def test_a_session_the_mailbox_refuses_is_a_refused_result_and_the_other_asks_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = fanout.install(
        monkeypatch,
        (_ask_continuing("tc-1", "peer~s~stolen"), fanout.ask("tc-2")),
        [fanout.answer("corr-1", "fine")],
    )
    fanout.asker_policy(allow_continuation=True)
    world.mailbox.refuse_continuation = True

    _run()

    results = dict(world.resume.calls[0])
    assert 'status="refused"' in str(results["tc-1"])
    assert 'status="ok"' in str(results["tc-2"])
    assert len(world.mailbox.asks) == 1


@pytest.mark.parametrize("bad", [7, ["x"], ""])
def test_a_session_that_is_not_text_is_a_malformed_ask(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    world = fanout.install(monkeypatch, (_ask_continuing("tc-1", bad),), [])
    fanout.asker_policy(allow_continuation=True)

    _run()

    assert world.mailbox.asks == []
    assert 'status="refused"' in str(dict(world.resume.calls[0])["tc-1"])
