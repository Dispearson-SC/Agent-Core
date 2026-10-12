"""C14: the `ask_peer` tool the model sees is generated per turn from who it may actually ask.

Subject: adapters/driven/peers/catalogue.py (`build_peer_catalogue`),
adapters/driven/tools/peers.py (the per-turn `prepare` of `ask_peer`),
adapters/driven/agent_pydantic/runner.py (the catalogue is computed per turn from the policy
snapshot and carried in the turn's deps).

Before this the tool listed no one: the persona carried a hard-coded list of agents, so adding a
specialist meant editing prompts, and a peer the policy forbade was still offered (and refused at
call time). Now the description lists the available peers with their profile's own `description`,
the `target` argument is an enum of exactly those ids, and a peer that is denied, disabled or
unknown is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.catalogue import build_peer_catalogue
from agent_core.adapters.driven.tools.peers import build_toolset
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

ASKER, BILLING, FRAUD, SHIPPING = "asker", "billing", "fraud", "shipping"


def _profile(
    profile_id: str,
    *,
    asks: tuple[str, ...] = (),
    answers: tuple[str, ...] = (),
    description: str = "",
    enabled: bool = True,
) -> AgentProfile:
    """`asks`: who this agent may ask; `answers` is only documentation of the other side."""
    del answers
    return AgentProfile(
        id=profile_id,
        persona=profile_id,
        model="m",
        description=description,
        peers=PeerPolicy(
            enabled=enabled,
            peers=tuple(AgentRef(agent_id=AgentId(a), display_name=a.title()) for a in asks),
        ),
    )


def _world() -> dict[str, AgentProfile]:
    return {
        ASKER: _profile(ASKER, asks=(BILLING, FRAUD, SHIPPING)),
        BILLING: _profile(
            BILLING, asks=(ASKER,), description="Answers invoice and refund questions."
        ),
        # fraud does not allow the asker to ask it back: the two-sided allowlist hides it
        FRAUD: _profile(FRAUD, asks=(), description="Scores transactions."),
        SHIPPING: _profile(SHIPPING, asks=(ASKER,), enabled=False),  # peers switched off
    }


def _permit_all(_: str) -> bool:
    return True


# --- the catalogue ---------------------------------------------------------------------------


def test_only_peers_the_gate_would_allow_are_listed() -> None:
    world = _world()

    catalogue = build_peer_catalogue(world[ASKER], world, permits=_permit_all)

    assert [peer.agent_id for peer in catalogue] == [BILLING]


def test_a_peer_the_policy_denies_is_hidden() -> None:
    world = _world()

    catalogue = build_peer_catalogue(world[ASKER], world, permits=lambda target: False)

    assert catalogue == ()


def test_an_unknown_peer_and_a_disabled_asker_list_nobody() -> None:
    world = _world()
    world[ASKER] = _profile(ASKER, asks=("ghost",))
    assert build_peer_catalogue(world[ASKER], world, permits=_permit_all) == ()

    world[ASKER] = _profile(ASKER, asks=(BILLING,), enabled=False)
    assert build_peer_catalogue(world[ASKER], world, permits=_permit_all) == ()


def test_a_new_peer_appears_without_any_prompt_edit() -> None:
    world = _world()
    world[ASKER] = _profile(ASKER, asks=(BILLING, "returns"))
    world["returns"] = _profile("returns", asks=(ASKER,), description="Handles return requests.")

    catalogue = build_peer_catalogue(world[ASKER], world, permits=_permit_all)

    assert {peer.agent_id: peer.summary for peer in catalogue} == {
        BILLING: "Answers invoice and refund questions.",
        "returns": "Handles return requests.",
    }
    assert world[ASKER].persona == ASKER, "the persona never named them"


def test_the_summary_falls_back_to_the_display_name_and_is_sorted_by_id() -> None:
    world = _world()
    world[BILLING] = _profile(BILLING, asks=(ASKER,))  # no description
    world[ASKER] = _profile(ASKER, asks=(FRAUD, BILLING))
    world[FRAUD] = _profile(FRAUD, asks=(ASKER,), description="Scores transactions.")

    catalogue = build_peer_catalogue(world[ASKER], world, permits=_permit_all)

    assert [(p.agent_id, p.summary) for p in catalogue] == [
        (BILLING, "Billing"),
        (FRAUD, "Scores transactions."),
    ]


# --- what the model is offered -----------------------------------------------------------------


def _request(profile_id: str = ASKER) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=profile_id,
        input=UserInput(text="hello"),
    )


def _offered(
    world: Mapping[str, AgentProfile], *, rules: tuple[PolicyRule, ...] = ()
) -> dict[str, Any] | None:
    """The `ask_peer` tool definition the model saw on its first request, or None if absent."""
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    request = _request()
    caller = request.caller
    allow = PolicyRule(
        rule_id="allow-peer", tool_pattern="ask_peer", effect=Effect.ALLOW, reason="ok"
    )
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(caller, (allow, *rules))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        peer_profiles=world,
    )

    asyncio.run(runner.run(TurnId("t1"), request, world[ASKER], None))

    tools = {tool.name: tool for tool in seen[0].function_tools}
    tool = tools.get("ask_peer")
    if tool is None:
        return None
    return {"description": tool.description, "schema": tool.parameters_json_schema}


def test_a_denied_peer_is_absent_from_the_tool_schema() -> None:
    offered = _offered(_world())

    assert offered is not None
    assert offered["schema"]["properties"]["target"]["enum"] == [BILLING]
    assert FRAUD not in offered["description"] and SHIPPING not in offered["description"]


def test_the_description_lists_each_available_peer_with_its_own_description() -> None:
    offered = _offered(_world())

    assert offered is not None
    assert f"- {BILLING}: Answers invoice and refund questions." in offered["description"]


def test_a_new_peer_appears_in_the_tool_without_touching_the_persona() -> None:
    world = _world()
    world[ASKER] = _profile(ASKER, asks=(BILLING, "returns"))
    world["returns"] = _profile("returns", asks=(ASKER,), description="Handles return requests.")

    offered = _offered(world)

    assert offered is not None
    assert offered["schema"]["properties"]["target"]["enum"] == [BILLING, "returns"]
    assert "- returns: Handles return requests." in offered["description"]


def test_when_the_policy_denies_ask_peer_the_tool_is_not_offered_at_all() -> None:
    deny = PolicyRule(rule_id="deny-peer", tool_pattern="ask_peer", effect=Effect.DENY, reason="no")

    assert _offered(_world(), rules=(deny,)) is None


def test_when_nobody_is_available_the_tool_is_not_offered() -> None:
    world = _world()
    world[ASKER] = _profile(ASKER, asks=(FRAUD,))  # fraud does not allow the asker back

    assert _offered(world) is None


def test_without_a_directory_the_tool_keeps_its_static_shape() -> None:
    """A runner built without `peer_profiles` (a test, a deployment without peers) behaves as
    before: no enum, no list, still offered."""
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    request = _request()
    allow = PolicyRule(rule_id="a", tool_pattern="ask_peer", effect=Effect.ALLOW, reason="ok")
    runner = runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(request.caller, (allow,))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("ask_peer",)),
        model_factory=lambda model_id, base_url: FunctionModel(model),
    )

    asyncio.run(runner.run(TurnId("t1"), request, _world()[ASKER], None))

    (tool,) = [t for t in seen[0].function_tools if t.name == "ask_peer"]
    assert "enum" not in tool.parameters_json_schema["properties"]["target"]


def test_the_profile_description_loads_from_the_mapping_and_moves_the_hash() -> None:
    plain = AgentProfile.from_mapping({"id": "x", "persona": "p", "model": "m"})
    described = AgentProfile.from_mapping(
        {"id": "x", "persona": "p", "model": "m", "description": "Does things."}
    )

    assert plain.description == "" and described.description == "Does things."
    assert plain.content_hash != described.content_hash
    with pytest.raises(Exception, match="description"):
        AgentProfile.from_mapping({"id": "x", "persona": "p", "model": "m", "description": 5})
