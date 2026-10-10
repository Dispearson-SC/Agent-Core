"""The Glazed vertical's configuration: profiles, star topology and policy rows.

Subject: Core/profiles/glazed_*.yaml, Core/policy/rules.yaml
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.adapters.driven.peers.hop_limit import authorise_hop
from agent_core.adapters.driven.policy_fs.loader import load_policy_rules
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.adapters.driven.tools.provider import build_tool_provider
from agent_core.composition import PEER_TOOLSET, TOOL_PACKAGES
from agent_core.domain.peers import AgentId
from agent_core.domain.policy import Effect, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import CallerIdentity, TenantId
from tests.fakes import ports as fakes

ROOT = Path(__file__).resolve().parents[2]
ROLES = (
    "orchestrator",
    "present",
    "past",
    "supply",
    "strategist",
    "sentinel",
    "auditor",
    "liaison",
)
SPECIALISTS = ("present", "past", "supply", "strategist", "sentinel", "liaison")
MODEL = "minimax/MiniMax-M3.1-flash-preview"


def _profile(role: str) -> AgentProfile:
    return load_profile_sync(ROOT / "profiles" / f"glazed_{role}.yaml")


@pytest.mark.parametrize("role", ROLES)
def test_profile_shape(role: str) -> None:
    p = _profile(role)
    assert p.id == f"glazed_{role}"
    assert p.toolsets == (f"glazed_{role}",)
    assert p.model == MODEL
    assert p.max_iterations <= 15
    assert p.max_cost_usd <= 1


def test_persona_rules_of_the_orchestrator() -> None:
    persona = _profile("orchestrator").persona.lower()
    for phrase in ("never invent", "do nothing", "evidence", "specialists"):
        assert phrase in persona


def test_past_persona_requires_reverification_of_recalled_numbers() -> None:
    persona = _profile("past").persona
    assert "recall_experiences" in persona
    assert "decision_id" in persona
    assert "get_history" in persona and "evaluate_promo" in persona
    assert "unavailable" in persona


def test_star_topology() -> None:
    orch = _profile("orchestrator")
    assert orch.peers.enabled
    assert {p.agent_id for p in orch.peers.peers} == {f"glazed_{r}" for r in SPECIALISTS}
    for role in SPECIALISTS:
        p = _profile(role)
        assert [x.agent_id for x in p.peers.peers] == ["glazed_orchestrator"]
        assert p.peers.max_hops == 1
    assert not _profile("auditor").peers.enabled


def test_orchestrator_can_ask_specialists_but_specialists_cannot_chain() -> None:
    orch = _profile("orchestrator")
    for role in SPECIALISTS:
        spec = _profile(role)
        ask = authorise_hop(
            caller=AgentId(orch.id), caller_policy=orch.peers,
            callee=AgentId(spec.id), callee_policy=spec.peers, hop=0,
        )
        assert ask.allowed, role
        back = authorise_hop(
            caller=AgentId(spec.id), caller_policy=spec.peers,
            callee=AgentId(orch.id), callee_policy=orch.peers, hop=1,
        )
        assert not back.allowed
        for other in SPECIALISTS:
            sideways = authorise_hop(
                caller=AgentId(spec.id), caller_policy=spec.peers,
                callee=AgentId(f"glazed_{other}"), callee_policy=_profile(other).peers, hop=0,
            )
            assert not sideways.allowed


@pytest.mark.parametrize("role", ROLES)
def test_every_profile_is_servable(role: str) -> None:
    from dataclasses import replace

    profile = _profile(role)
    if profile.peers.enabled:
        profile = replace(profile, toolsets=(*profile.toolsets, PEER_TOOLSET))
    names = asyncio.run(build_tool_provider(TOOL_PACKAGES).tool_names_for(profile))
    assert names
    assert ("ask_peer" in names) == (role == "orchestrator" or profile.peers.enabled)


# ---- policy --------------------------------------------------------------------------

RULES = load_policy_rules(ROOT / "policy")
MANAGER = CallerIdentity("m-1", "glazed", TenantId("S030"), frozenset({"glazed-manager"}))
SERVICE = CallerIdentity("svc", "glazed", TenantId("S030"), frozenset({"glazed-service"}))
PEER = CallerIdentity("peer-agent", "peer", TenantId("S030"), frozenset({"peer"}))
READ = (
    "get_kpis get_day_summary get_issues explain_metric get_order_plan project_inventory "
    "get_supplier_performance get_history evaluate_promo recall_experiences "
    "offer_surplus request_stock"
).split()


def _effect(caller: CallerIdentity, tool: str) -> Effect:
    rules = RuleSet.for_caller(caller, RULES)
    return fakes.FakeToolPolicy(rules).decide(rules, tool, {}).effect


@pytest.mark.parametrize("tool", [*READ, "record_event"])
def test_specialists_and_service_turns_may_use_their_tools(tool: str) -> None:
    assert _effect(PEER, tool) is Effect.ALLOW
    assert _effect(SERVICE, tool) is Effect.ALLOW


@pytest.mark.parametrize("tool", [*READ, "record_event"])
def test_the_manager_turn_has_no_direct_data_tools(tool: str) -> None:
    assert _effect(MANAGER, tool) is Effect.DENY


def test_only_the_manager_turn_proposes_and_delegates() -> None:
    assert _effect(MANAGER, "propose_action") is Effect.ALLOW
    assert _effect(MANAGER, "ask_peer") is Effect.ALLOW
    for caller in (PEER, SERVICE):
        assert _effect(caller, "propose_action") is Effect.DENY
    assert _effect(PEER, "ask_peer") is Effect.DENY


def test_no_glazed_execution_tool_exists_or_is_allowed() -> None:
    for tool in ("glazed_execute", "auto_apply_action", "execute_action"):
        assert _effect(MANAGER, tool) is Effect.DENY


def test_the_orchestrator_persona_lists_the_exact_peer_ids_for_ask_peer() -> None:
    """A model once sent target "billing" instead of the real id and was refused."""
    orch = _profile("orchestrator")
    for peer in orch.peers.peers:
        assert f"`{peer.agent_id}`" in orch.persona or f" {peer.agent_id}" in orch.persona
    assert "exact" in orch.persona.lower()
    assert "ask_peer" in orch.persona


def _decision(caller: CallerIdentity, tool: str):  # type: ignore[no-untyped-def]
    rules = RuleSet.for_caller(caller, RULES)
    return fakes.FakeToolPolicy(rules).decide(rules, tool, {})


def test_ask_peer_is_allowed_by_the_named_row_for_channel_glazed_and_the_manager_role() -> None:
    decision = _decision(MANAGER, "ask_peer")

    assert decision.effect is Effect.ALLOW
    assert decision.rule_id == "glazed-orchestrator-asks-specialists"
    # the same role on another channel, or another role on glazed, gets nothing from it
    other_channel = CallerIdentity("m", "http", TenantId("S030"), frozenset({"glazed-manager"}))
    other_role = CallerIdentity("m", "glazed", TenantId("S030"), frozenset({"glazed-service"}))
    assert _decision(other_channel, "ask_peer").effect is not Effect.ALLOW
    assert _decision(other_role, "ask_peer").effect is not Effect.ALLOW


def test_the_peer_channel_is_denied_by_the_specific_glazed_row() -> None:
    decision = _decision(PEER, "ask_peer")

    assert decision.effect is Effect.DENY
    assert decision.rule_id == "glazed-specialists-do-not-ask-peers"
