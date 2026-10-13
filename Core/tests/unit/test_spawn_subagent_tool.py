"""C28 (D62): the `spawn_subagent` tool, its per-turn menu and the runner's translation of a call.

Subjects: adapters/driven/tools/subagents.py (the deferring tool and its `prepare`),
adapters/driven/peers/catalogue.py (`build_subagent_catalogue`),
adapters/driven/agent_pydantic/runner.py (the menu per turn, the pending request),
composition.py (`peers`-style grant of the toolset from `subagents.enabled`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.peers.catalogue import build_subagent_catalogue
from agent_core.adapters.driven.tools.subagents import build_toolset, spawn_subagent
from agent_core.composition import SUBAGENT_TOOLSET, _grant_peer_mechanism
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.peers import AgentId
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.subagents import SubagentPolicy
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from tests.fakes import ports as fakes

BOSS, RESEARCHER, WRITER, PEERISH = "boss", "researcher", "writer", "peerish"


def _profile(
    profile_id: str,
    *,
    spawns: tuple[str, ...] = (),
    enabled: bool = True,
    mode: AgentMode = AgentMode.ALL,
    description: str = "",
) -> AgentProfile:
    return AgentProfile(
        id=profile_id,
        persona=profile_id,
        model="m",
        mode=mode,
        description=description,
        subagents=SubagentPolicy(
            enabled=enabled and bool(spawns),
            agents=tuple(AgentId(a) for a in spawns),
            # R1: every worker in this world consents to every spawner in it.
            accept_from=(AgentId(BOSS), AgentId(RESEARCHER), AgentId(WRITER)),
        ),
    )


def _world() -> dict[str, AgentProfile]:
    return {
        BOSS: _profile(BOSS, spawns=(RESEARCHER, WRITER, PEERISH, "ghost")),
        RESEARCHER: _profile(
            RESEARCHER, mode=AgentMode.SUBAGENT, description="Reads sources and reports."
        ),
        WRITER: _profile(WRITER, mode=AgentMode.SUBAGENT),
        PEERISH: _profile(PEERISH, mode=AgentMode.PEER),  # listed, but not spawnable by mode
    }


# --- the tool ---------------------------------------------------------------------------------


def test_the_tool_always_defers_carrying_exactly_the_models_arguments() -> None:
    with pytest.raises(CallDeferred) as plain:
        spawn_subagent("researcher", "find the date")
    with pytest.raises(CallDeferred) as full:
        spawn_subagent("researcher", "find the date", background=True, continue_session="peer~s~k")

    assert plain.value.metadata == {"agent": "researcher", "task": "find the date"}
    assert full.value.metadata == {
        "agent": "researcher",
        "task": "find the date",
        "background": True,
        "continue_session": "peer~s~k",
    }


def test_the_toolset_is_exactly_spawn_subagent() -> None:
    assert list(build_toolset().tools) == ["spawn_subagent"]


# --- the catalogue ----------------------------------------------------------------------------


def test_the_catalogue_lists_only_what_the_gate_would_let_through() -> None:
    world = _world()

    listed = build_subagent_catalogue(world[BOSS], world, permits=lambda _: True)

    assert [(e.agent_id, e.summary) for e in listed] == [
        (RESEARCHER, "Reads sources and reports."),
        (WRITER, WRITER),
    ], "an unknown, a not-spawnable and an unlisted agent are absent; no description falls back"


def test_a_denied_subagent_is_hidden_and_a_disabled_spawner_lists_nobody() -> None:
    world = _world()

    denied = build_subagent_catalogue(world[BOSS], world, permits=lambda a: a != RESEARCHER)
    off = build_subagent_catalogue(
        _profile(BOSS, spawns=(RESEARCHER,), enabled=False), world, permits=lambda _: True
    )

    assert [e.agent_id for e in denied] == [WRITER]
    assert off == ()


# --- what the model is offered ------------------------------------------------------------------


def _request(profile_id: str = BOSS) -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s"), tenant_id=TenantId("t")),
        caller=CallerIdentity(subject_id="u", channel="http", tenant_id=TenantId("t")),
        profile_id=profile_id,
        input=UserInput(text="hello"),
    )


def _runner(
    world: Mapping[str, AgentProfile], model: Any, rules: tuple[PolicyRule, ...] = ()
) -> Any:
    allow = PolicyRule(
        rule_id="allow-spawn", tool_pattern="spawn_subagent", effect=Effect.ALLOW, reason="ok"
    )
    return runner_module.PydanticAgentRunner(
        model=fakes.FakeModelGateway(model_map={"m": "m"}),
        policy=fakes.FakeToolPolicy(RuleSet.for_caller(_request().caller, (allow, *rules))),
        audit=fakes.FakeAuditSink(),
        tools=fakes.FakeToolProvider(build_toolset(), ("spawn_subagent",)),
        model_factory=lambda model_id, base_url: FunctionModel(model),
        peer_profiles=world,
    )


def _offered(
    world: Mapping[str, AgentProfile],
    rules: tuple[PolicyRule, ...] = (),
    request: TurnRequest | None = None,
) -> dict[str, Any] | None:
    seen: list[AgentInfo] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    asked = request or _request()
    asyncio.run(
        _runner(world, model, rules).run(TurnId("t1"), asked, world[asked.profile_id], None)
    )
    tool = {t.name: t for t in seen[0].function_tools}.get("spawn_subagent")
    return (
        None
        if tool is None
        else {
            "description": tool.description,
            "schema": tool.parameters_json_schema,
        }
    )


def test_the_menu_is_an_enum_of_the_spawnable_agents_with_their_descriptions() -> None:
    offered = _offered(_world())

    assert offered is not None
    assert offered["schema"]["properties"]["agent"]["enum"] == [RESEARCHER, WRITER]
    assert f"- {RESEARCHER}: Reads sources and reports." in offered["description"]
    assert PEERISH not in offered["description"] and "ghost" not in offered["description"]


def test_the_follow_up_and_background_parameters_are_offered_only_to_a_profile_that_opted_in() -> (
    None
):
    plain = _offered(_world())
    assert plain is not None
    assert set(plain["schema"]["properties"]) == {"agent", "task"}

    world = _world()
    from dataclasses import replace

    from agent_core.domain.peers import PeerPolicy

    world[BOSS] = replace(
        world[BOSS], peers=PeerPolicy(allow_continuation=True, allow_background=True)
    )
    opted = _offered(world)
    assert opted is not None
    assert set(opted["schema"]["properties"]) == {
        "agent",
        "task",
        "continue_session",
        "background",
    }


def test_a_policy_deny_for_one_agent_hides_it_and_for_the_tool_hides_the_tool() -> None:
    deny_one = PolicyRule(
        rule_id="no-researcher",
        tool_pattern="spawn_subagent",
        effect=Effect.DENY,
        reason="no",
        resource_pattern=RESEARCHER,
    )
    deny_all = PolicyRule(
        rule_id="no-spawn", tool_pattern="spawn_subagent", effect=Effect.DENY, reason="no"
    )

    one = _offered(_world(), rules=(deny_one,))
    assert one is not None and one["schema"]["properties"]["agent"]["enum"] == [WRITER]
    assert _offered(_world(), rules=(deny_all,)) is None


def test_deny_wins_over_an_allow_for_the_same_agent() -> None:
    allow = PolicyRule(
        rule_id="yes",
        tool_pattern="spawn_subagent",
        effect=Effect.ALLOW,
        reason="y",
        resource_pattern=RESEARCHER,
    )
    deny = PolicyRule(
        rule_id="no",
        tool_pattern="spawn_subagent",
        effect=Effect.DENY,
        reason="n",
        resource_pattern=RESEARCHER,
    )

    offered = _offered(_world(), rules=(allow, deny))

    assert offered is not None and offered["schema"]["properties"]["agent"]["enum"] == [WRITER]


def test_nobody_to_spawn_means_no_tool() -> None:
    world = _world()
    world[BOSS] = _profile(BOSS, spawns=(PEERISH,))

    assert _offered(world) is None


def test_a_subagents_own_menu_has_no_spawn_tool() -> None:
    """The restricted toolset: a turn that is itself a subagent does not get `spawn_subagent`
    (C29 makes this depth-aware; here a subagent never spawns)."""
    from agent_core.domain.agent_mode import DelegationKind
    from agent_core.domain.turn import PeerOwner

    world = _world()
    world[RESEARCHER] = _profile(RESEARCHER, spawns=(WRITER,), mode=AgentMode.SUBAGENT)
    child = TurnRequest(
        session=_request().session,
        caller=_request().caller,
        profile_id=RESEARCHER,
        input=UserInput(text="task"),
        owner=PeerOwner(
            "root", "root", BOSS, "corr", kind=DelegationKind.SUBAGENT, subagent_depth=1
        ),
    )

    assert _offered(world, request=child) is None
    assert _offered(world) is not None, "the same profile as a top-level turn keeps its tool"


# --- a call becomes a pending delegation -------------------------------------------------------


def test_a_spawn_call_suspends_the_turn_as_a_delegation_naming_the_agent_and_task() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "spawn_subagent", {"agent": RESEARCHER, "task": "find the date"}, "call-1"
                )
            ]
        )

    world = _world()
    outcome = asyncio.run(_runner(world, model).run(TurnId("t1"), _request(), world[BOSS], None))

    (pending,) = outcome.pending
    assert pending.kind is PendingKind.DELEGATION and pending.tool_name == "spawn_subagent"
    assert pending.arguments == {"agent": RESEARCHER, "task": "find the date"}
    assert RESEARCHER in pending.reason and "find the date" in pending.reason


# --- the toolset is granted from the profile's own switch --------------------------------------


def test_subagents_enabled_grants_the_spawn_toolset_and_peers_enabled_is_not_needed() -> None:
    boss = _world()[BOSS]

    granted = _grant_peer_mechanism(boss)

    assert SUBAGENT_TOOLSET in granted.toolsets
    assert "peers" not in granted.toolsets
    assert _grant_peer_mechanism(granted).toolsets == granted.toolsets, "idempotent"
    assert _grant_peer_mechanism(_world()[RESEARCHER]).toolsets == (), "nothing enabled, no grant"


def test_a_subagent_whose_profile_allows_a_deeper_chain_keeps_the_spawn_tool() -> None:
    """C29: the menu hides the tool only once a spawn would pass the profile's own depth limit."""
    from dataclasses import replace

    from agent_core.domain.agent_mode import DelegationKind
    from agent_core.domain.turn import PeerOwner

    world = _world()
    deeper = replace(
        _profile(RESEARCHER, spawns=(WRITER,), mode=AgentMode.SUBAGENT),
        subagents=SubagentPolicy(
            enabled=True,
            agents=(AgentId(WRITER),),
            depth=2,
            accept_from=(AgentId(BOSS), AgentId(RESEARCHER)),
        ),
    )
    world[RESEARCHER] = deeper

    def child(depth: int) -> TurnRequest:
        return TurnRequest(
            session=_request().session,
            caller=_request().caller,
            profile_id=RESEARCHER,
            input=UserInput(text="task"),
            owner=PeerOwner(
                "root", "root", BOSS, "corr", kind=DelegationKind.SUBAGENT, subagent_depth=depth
            ),
        )

    assert _offered(world, request=child(1)) is not None, "one link above, depth 2 allows another"
    assert _offered(world, request=child(2)) is None, "two links above, no more"
