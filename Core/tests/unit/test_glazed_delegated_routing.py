"""The glazed routing under a delegated identity, offline, with the real glazed policy (S8, P9).

Subject: application/route_turn.py with the shipped glazed profiles, intent plans and policy rules
(Core's policy files, the glazed_tools addon's rules and the deployment's ask_peer rule).

THE RULES UNDER TEST. The glazed policy lets only a specialist answering a peer (`peer`) and the
backend service read store data; the manager identity reads none of it directly. With routing on,
a manager's turn the router sends to a specialist (layer A) or to an intent plan (layer B) runs as
that specialist's peer turn would, on behalf of the manager: every one of the ten plans' calls is
allowed under that identity, every route target lands, and the manager's own identity is still
denied every one of those reads. Nothing in the glazed policy changes.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.policy_fs.loader import parse_policy_rules
from agent_core.application.route_turn import RouteTurn
from agent_core.composition import route_peer_gate
from agent_core.domain.intents import resolve_arguments
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.routing import RouteDecision, RouteKind, RouteRecord, RouteRequest
from agent_core.domain.turn import (
    PEER_SUBJECT_ID,
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
    delegated_identity,
)
from tests.unit import glazed_vertical_support as glazed

ORCHESTRATOR = "glazed_orchestrator"
SPECIALISTS = tuple(f"glazed_{role}" for role in glazed.ROLES if role != "orchestrator")
MANAGER = CallerIdentity(
    subject_id="manager-1",
    channel="glazed",
    tenant_id=TenantId("S030"),
    roles=frozenset({"glazed-manager"}),
)


class GlazedPolicy:
    """The real rules, narrowed per caller and decided by the production `PgToolPolicy.decide`."""

    def __init__(self, rules: tuple[PolicyRule, ...]) -> None:
        self.rules = rules
        self._decider = PgToolPolicy(lambda: None)  # type: ignore[arg-type, return-value]

    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        return RuleSet.for_caller(caller, self.rules)

    def filter_toolset(self, rules: RuleSet, tool_names: tuple[str, ...]) -> tuple[str, ...]:
        return self._decider.filter_toolset(rules, tool_names)

    def decide(self, rules: RuleSet, tool_name: str, arguments: dict[str, object]) -> object:
        return self._decider.decide(rules, tool_name, arguments)


class Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        return self.decision


class Routes:
    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        return None


def _route(decision: RouteDecision) -> object:
    use_case = RouteTurn(
        routers=Router(decision),
        audit=Routes(),
        profiles=glazed.served(glazed.CORE / "profiles"),
        plans=glazed.contributions().intents,
        peer_gate=route_peer_gate,
        policy=GlazedPolicy(glazed.policy_rules()),  # type: ignore[arg-type]
    )
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("S030")),
        caller=MANAGER,
        profile_id=ORCHESTRATOR,
        input=UserInput(text="¿cómo vamos hoy?"),
    )
    return asyncio.run(use_case.execute(TurnId("turn-1"), request))


def _decide(caller: CallerIdentity, tool: str, arguments: dict[str, object]) -> Effect:
    policy = GlazedPolicy(glazed.policy_rules())
    rules = asyncio.run(policy.load_rules(caller))
    return policy.decide(rules, tool, arguments).effect  # type: ignore[attr-defined, no-any-return]


@pytest.mark.parametrize("intent", sorted(glazed.contributions().intents))
def test_every_intent_plan_runs_delegated_and_every_call_passes_the_glazed_policy(
    intent: str,
) -> None:
    plan = glazed.contributions().intents[intent]

    routed = _route(RouteDecision(kind=RouteKind.INTENT, intent=intent, confidence=0.95))

    assert routed.intent is not None, routed.record.fallback_reason  # type: ignore[attr-defined]
    request = routed.request  # type: ignore[attr-defined]
    assert request.profile_id == plan.profile_id
    assert request.caller == delegated_identity(MANAGER)
    record = routed.record  # type: ignore[attr-defined]
    assert (record.actor, record.on_behalf_of) == (PEER_SUBJECT_ID, "manager-1")
    context = {"today": "2026-10-14", "channel": "glazed", "tenant_id": "S030"}
    for step in plan.steps:
        call = resolve_arguments(step, {}, context)
        delegated = _decide(request.caller, call.tool, dict(call.arguments))
        direct = _decide(MANAGER, call.tool, dict(call.arguments))
        assert delegated is Effect.ALLOW, f"{intent}: {call.tool} under the delegated identity"
        assert direct is Effect.DENY, f"{intent}: the manager still never reads {call.tool}"


@pytest.mark.parametrize("target", SPECIALISTS)
def test_every_route_target_lands_delegated_because_the_orchestrator_may_ask_it(
    target: str,
) -> None:
    routed = _route(RouteDecision(kind=RouteKind.ROUTE, profile_id=target, confidence=0.95))

    record = routed.record  # type: ignore[attr-defined]
    assert record.fallback_reason is None and record.profile_id == target
    assert routed.request.caller == delegated_identity(MANAGER)  # type: ignore[attr-defined]
    assert routed.request.hop == 1  # type: ignore[attr-defined]


def test_a_manager_direct_read_is_still_denied_for_every_plan_tool() -> None:
    tools = {s.tool for p in glazed.contributions().intents.values() for s in p.steps}
    assert tools, "the plans call at least one tool"
    for tool in sorted(tools):
        assert _decide(MANAGER, tool, {}) is Effect.DENY, tool


def test_without_the_deployment_ask_rule_no_glazed_route_lands() -> None:
    """The route stands in for the orchestrator's ask_peer: with the rule that allows that ask
    removed, the manager's turns run the orchestrator as before (no policy is widened)."""
    rules = tuple(
        r for r in glazed.policy_rules() if r.rule_id != "glazed-orchestrator-asks-specialists"
    )
    use_case = RouteTurn(
        routers=Router(RouteDecision(kind=RouteKind.ROUTE, profile_id="glazed_supply")),
        audit=Routes(),
        profiles=glazed.served(glazed.CORE / "profiles"),
        peer_gate=route_peer_gate,
        policy=GlazedPolicy(rules),  # type: ignore[arg-type]
    )
    request = TurnRequest(
        session=SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("S030")),
        caller=MANAGER,
        profile_id=ORCHESTRATOR,
        input=UserInput(text="¿qué pido?"),
    )

    routed = asyncio.run(use_case.execute(TurnId("turn-1"), request))

    assert routed.request == request
    assert routed.record is not None
    assert routed.record.fallback_reason == "ask_peer_not_allowed:deny"


_DEPLOYMENT_FILE = os.environ.get("GLAZED_DEPLOYMENT_POLICY")


@pytest.mark.skipif(
    not _DEPLOYMENT_FILE, reason="GLAZED_DEPLOYMENT_POLICY names no glazed checkout's policy file"
)
def test_the_rule_copied_here_is_the_glazed_deployments_own() -> None:
    """Run with GLAZED_DEPLOYMENT_POLICY=<glazed-copilot>/infra/core/policy/glazed_deployment.yaml
    to prove the deployment rule these tests use is the file glazed ships."""
    text = Path(str(_DEPLOYMENT_FILE)).read_text(encoding="utf-8")
    shipped = parse_policy_rules(text, source="x")
    copied = parse_policy_rules(glazed.DEPLOYMENT_RULES, source="x")
    assert shipped == copied
