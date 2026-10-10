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
SPECIALISTS = ("present", "past", "supply", "strategist", "sentinel", "auditor", "liaison")
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


def test_orchestrator_routes_data_integrity_questions_to_the_auditor() -> None:
    persona = " ".join(_profile("orchestrator").persona.split())
    assert "glazed_auditor (data integrity" in persona
    assert "exposure_usd" in persona
    assert "is not askable" not in persona
    assert "¿problemas en los datos?" in persona
    assert "¿perdemos dinero sin darnos cuenta?" in persona


def test_auditor_answers_the_orchestrator_and_cannot_ask_anybody() -> None:
    persona = " ".join(_profile("auditor").persona.split())
    assert "you do not talk to other agents" not in persona
    assert "answer the orchestrator" in persona


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
    "get_snapshot check_compliance get_integrity_issues audit_promo "
    "get_kpis get_day_summary get_issues explain_metric get_order_plan project_inventory "
    "get_supplier_performance get_history evaluate_promo recall_experiences "
    "offer_surplus request_stock forecast_demand forecast_daily_flow "
    "get_inventory_status get_expiring get_deliveries get_issue_analysis "
    "get_staffing get_promos get_outlook"
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
    assert decision.rule_id == "glazed-peer-turn-no-ask-peer"


def test_classify_text_is_allowed_for_manager_service_and_peer_turns_only_on_glazed_channels() -> (
    None
):
    for caller in (MANAGER, SERVICE, PEER):
        assert _effect(caller, "classify_text") is Effect.ALLOW
    stranger = CallerIdentity("x", "http", TenantId("S030"), frozenset({"glazed-manager"}))
    assert _effect(stranger, "classify_text") is not Effect.ALLOW


def _rule(rule_id: str):  # type: ignore[no-untyped-def]
    return next(r for r in RULES if r.rule_id == rule_id)


def test_the_peer_turn_deny_rows_name_their_scope_and_stay_deny() -> None:
    for rule_id in ("glazed-peer-turn-no-ask-peer", "glazed-peer-turn-no-propose-action"):
        rule = _rule(rule_id)
        assert rule.effect is Effect.DENY
        assert rule.channels == frozenset({"peer"})
        assert rule.subject_roles == frozenset({"peer"})  # not "any role on the peer channel"
    assert _effect(PEER, "propose_action") is Effect.DENY
    assert _effect(PEER, "ask_peer") is Effect.DENY


def test_stub_tools_are_named_placeholders_not_reads() -> None:
    ids = {r.rule_id for r in RULES}
    assert {"glazed-placeholder-offer-surplus", "glazed-placeholder-request-stock"} <= ids
    assert not {"glazed-read-offer-surplus", "glazed-read-request-stock"} & ids
    assert "not available" in _rule("glazed-placeholder-offer-surplus").reason.lower()
    # the deny default for an unknown tool did not move
    assert _effect(MANAGER, "something_new") is Effect.DENY
    assert _effect(PEER, "something_new") is Effect.DENY


def test_get_briefing_is_allowed_for_the_manager_turn_only() -> None:
    assert _effect(MANAGER, "get_briefing") is Effect.ALLOW
    assert _effect(PEER, "get_briefing") is Effect.DENY
    assert _effect(SERVICE, "get_briefing") is Effect.DENY


def test_supply_and_strategist_personas_route_future_questions_to_forecast_tools() -> None:
    for role, tool in (("supply", "forecast_demand"), ("strategist", "forecast_daily_flow")):
        persona = _profile(role).persona
        assert tool in persona
        assert "never estimate a forecast yourself" in persona.lower()
        assert "unavailable" in persona.lower()


NEW_VIEW_TOOLS = (
    "get_inventory_status get_expiring get_deliveries get_issue_analysis "
    "get_staffing get_promos get_outlook"
).split()


@pytest.mark.parametrize("tool", NEW_VIEW_TOOLS)
def test_each_view_tool_has_its_own_read_row_for_peer_and_service_on_glazed(tool: str) -> None:
    rule = _rule("glazed-read-" + tool.replace("_", "-"))
    assert rule.tool_pattern == tool
    assert rule.effect is Effect.ALLOW
    assert rule.subject_roles == frozenset({"peer", "glazed-service"})
    assert rule.channels == frozenset({"peer", "glazed"})


def test_the_orchestrator_toolset_stays_free_of_data_tools() -> None:
    from agent_core.adapters.driven.tools.glazed.tools import TOOLSETS

    tools = set(TOOLSETS["orchestrator"]().tools)
    assert tools == {"propose_action", "classify_text", "get_briefing"}


def test_orchestrator_routes_staffing_promos_expiry_inventory_and_today_tomorrow() -> None:
    persona = " ".join(_profile("orchestrator").persona.split())
    assert "staffing hours" in persona and "glazed_strategist" in persona
    assert "promotions" in persona
    assert "expiring items" in persona and "inventory cover" in persona
    assert "deliveries" in persona
    assert "today or tomorrow" in persona and "glazed_present" in persona


@pytest.mark.parametrize(
    ("role", "tools"),
    [
        (
            "supply",
            ("get_inventory_status", "get_expiring", "get_deliveries", "get_issue_analysis"),
        ),
        ("present", ("get_expiring", "get_outlook")),
        ("strategist", ("get_staffing", "get_promos", "get_outlook")),
    ],
)
def test_specialist_personas_call_their_tool_before_saying_data_is_missing(
    role: str, tools: tuple[str, ...]
) -> None:
    persona = " ".join(_profile(role).persona.split())
    for tool in tools:
        assert tool in persona
    assert "BEFORE saying data is missing" in persona


@pytest.mark.parametrize("role", ["orchestrator", "present", "supply", "strategist"])
def test_weekdays_come_from_the_briefing_calendar(role: str) -> None:
    persona = " ".join(_profile(role).persona.split())
    assert "weekday_es" in persona
    assert "never compute a weekday" in persona.lower()


def test_order_recommendations_carry_confidence_do_nothing_and_min_order_warning() -> None:
    for role in ("orchestrator", "supply"):
        persona = " ".join(_profile(role).persona.split())
        assert "min_order_warning" in persona
        assert "do-nothing comparison" in persona


def test_past_outcomes_are_cited_for_what_happened_before() -> None:
    for role in ("orchestrator", "past"):
        persona = " ".join(_profile(role).persona.split())
        assert "decision_log" in persona and "outcome" in persona
        assert "what happened before" in persona.lower()
