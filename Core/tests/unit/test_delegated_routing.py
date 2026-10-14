"""A routed turn runs under a delegated identity, like an ask_peer to its target (S8).

Subject: domain/turn.py (`delegated_identity`, `reply_identity`, `CallerIdentity.on_behalf_of`),
application/route_turn.py (the delegation guard) and domain/routing.py (its fallback codes).

THE RULES UNDER TEST. A route (layer A) or an intent plan (layer B) that lands on another profile
runs that profile as a peer turn of it runs: the caller is the peer identity (`peer-agent` on the
`peer` channel with the `peer` role), acting on behalf of the person, one hop deep. Policy judges
the delegated identity exactly as it judges a peer turn, so a read allowed only to peers is
allowed, and the person's own identity keeps its policy. A route lands only when the entry
profile could itself ask the target: the two-sided peer gate allows the hop AND the person's
policy allows `ask_peer` to that target. Anything else is refused with the reason recorded and
the turn runs the requested profile as the person. A turn the router did not route keeps the
person's identity. The route audit row names both identities.
"""

from __future__ import annotations

import asyncio
import pickle
from typing import Any

import pytest

from agent_core.application.route_turn import RouteTurn
from agent_core.domain.agent_mode import AgentMode
from agent_core.domain.intents import IntentPlan, PlanStep
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.routing import (
    FALLBACK_ASK_PEER_NOT_ALLOWED,
    FALLBACK_DELEGATION_UNAVAILABLE,
    FALLBACK_PEER_REFUSED,
    RouteDecision,
    RouteKind,
    RouteRecord,
    RouteRequest,
)
from agent_core.domain.turn import (
    PEER_CHANNEL,
    PEER_ROLE,
    PEER_SUBJECT_ID,
    CallerIdentity,
    SessionId,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
    delegated_identity,
    peer_identity,
    reply_identity,
)
from tests.fakes.ports import FakeToolPolicy

FRONT = "front_desk"
BILLING = "billing"
STRANGER = "stranger"  # a routing target the front desk cannot ask
SHY = "shy"  # a routing target that does not accept the front desk's asks
PERSON = CallerIdentity(
    subject_id="manager-7",
    channel="shop",
    tenant_id=TenantId("t"),
    roles=frozenset({"manager"}),
    modality="voice",
)

ASK_BILLING = PolicyRule(
    rule_id="manager-asks",
    tool_pattern="ask_peer",
    effect=Effect.ALLOW,
    reason="the manager's agent asks its specialists",
    subject_roles=frozenset({"manager"}),
    channels=frozenset({"shop"}),
)
PEER_READS = PolicyRule(
    rule_id="peer-reads",
    tool_pattern="read_ledger",
    effect=Effect.ALLOW,
    reason="only a specialist answering a peer reads the ledger",
    subject_roles=frozenset({PEER_ROLE}),
    channels=frozenset({PEER_CHANNEL}),
)


def _peers(*names: str, enabled: bool = True) -> dict[str, Any]:
    return {"enabled": enabled, "peers": [{"agent_id": n, "display_name": n} for n in names]}


def _profiles() -> dict[str, AgentProfile]:
    front = AgentProfile.from_mapping(
        {
            "id": FRONT,
            "persona": "You greet.",
            "model": "m",
            "routing": {"router": "r", "targets": [BILLING, STRANGER, SHY], "intents": True},
            "peers": _peers(BILLING, SHY),
        }
    )

    def specialist(name: str, *back: str) -> AgentProfile:
        return AgentProfile.from_mapping(
            {"id": name, "persona": "p", "model": "m", "peers": _peers(*back)}
        )

    return {
        FRONT: front,
        BILLING: specialist(BILLING, FRONT),
        STRANGER: specialist(STRANGER, FRONT),
        SHY: specialist(SHY),
    }


class NarrowingPolicy(FakeToolPolicy):
    """A `ToolPolicy` that narrows the rules to the caller it is asked for, as the real one does."""

    def __init__(self, *rules: PolicyRule) -> None:
        super().__init__(RuleSet.for_caller(PERSON, ()))
        self.rules = rules

    async def load_rules(self, caller: CallerIdentity) -> RuleSet:
        self.load_rules_calls.append(caller)
        return RuleSet.for_caller(caller, self.rules)


def _gate(entry: AgentProfile, target: AgentProfile, hop: int) -> tuple[int | None, str | None]:
    """The peer gate composition binds (`authorise_hop`), reduced to what the tests need."""
    from agent_core.adapters.driven.peers.hop_limit import authorise_hop
    from agent_core.domain.peers import AgentId

    decision = authorise_hop(
        caller=AgentId(entry.id),
        caller_policy=entry.peers,
        callee=AgentId(target.id),
        callee_policy=target.peers,
        hop=hop,
        callee_mode=target.mode,
    )
    return decision.next_hop, None if decision.reason is None else decision.reason.value


class Router:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision

    async def route(self, router: str, request: RouteRequest) -> RouteDecision:
        return self.decision


class Routes:
    def __init__(self) -> None:
        self.records: list[RouteRecord] = []

    async def record_route(self, turn_id: TurnId, tenant_id: str, record: RouteRecord) -> None:
        self.records.append(record)


def _request() -> TurnRequest:
    return TurnRequest(
        session=SessionRef(session_id=SessionId("s-1"), tenant_id=TenantId("t")),
        caller=PERSON,
        profile_id=FRONT,
        input=UserInput(text="how much do we owe?"),
    )


def _route(
    decision: RouteDecision,
    *,
    policy: NarrowingPolicy | None = None,
    gate: Any = _gate,
    plans: dict[str, IntentPlan] | None = None,
) -> tuple[Any, Routes]:
    routes = Routes()
    use_case = RouteTurn(
        routers=Router(decision),
        audit=routes,
        profiles=_profiles(),
        plans=plans,
        peer_gate=gate,
        policy=policy if policy is not None else NarrowingPolicy(ASK_BILLING, PEER_READS),
    )
    return asyncio.run(use_case.execute(TurnId("turn-1"), _request())), routes


def _to(profile: str) -> RouteDecision:
    return RouteDecision(kind=RouteKind.ROUTE, profile_id=profile, label=profile, confidence=0.9)


# --- the identities ------------------------------------------------------------------------------


def test_the_delegated_identity_is_the_peer_identity_acting_on_behalf_of_the_person() -> None:
    delegated = delegated_identity(PERSON)

    assert (delegated.subject_id, delegated.channel, delegated.roles) == (
        PEER_SUBJECT_ID,
        PEER_CHANNEL,
        frozenset({PEER_ROLE}),
    )
    assert delegated.tenant_id == PERSON.tenant_id
    assert delegated.on_behalf_of == PERSON
    assert delegated.modality == "voice", "how the reply is consumed is the person's"
    assert reply_identity(delegated) == PERSON
    assert reply_identity(PERSON) == PERSON


def test_a_peer_turns_identity_is_unchanged_and_acts_on_behalf_of_nobody() -> None:
    from agent_core.adapters.driving.peers.worker import build_peer_identity

    peer = build_peer_identity(TenantId("t"))

    assert peer == peer_identity(TenantId("t"))
    assert peer == CallerIdentity(
        subject_id="peer-agent", channel="peer", tenant_id=TenantId("t"), roles=frozenset({"peer"})
    )
    assert peer.on_behalf_of is None


def test_a_delegated_identity_never_nests_and_a_peer_cannot_delegate_again() -> None:
    with pytest.raises(ValueError, match="on behalf of"):
        delegated_identity(delegated_identity(PERSON))


def test_an_identity_pickled_by_an_older_build_reads_back_acting_for_nobody() -> None:
    # A turn suspended before this build holds the five-field state in its operation log.
    old_state = ["manager-7", "shop", TenantId("t"), frozenset({"manager"}), "voice"]
    restored = CallerIdentity.__new__(CallerIdentity)
    restored.__setstate__(old_state)

    assert restored == PERSON and restored.on_behalf_of is None
    assert pickle.loads(pickle.dumps(delegated_identity(PERSON))) == delegated_identity(PERSON)


# --- a route runs delegated ----------------------------------------------------------------------


def test_a_route_runs_the_target_as_a_peer_turn_on_behalf_of_the_person() -> None:
    routed, routes = _route(_to(BILLING))

    request = routed.request
    assert request.profile_id == BILLING
    assert request.caller == delegated_identity(PERSON)
    assert request.hop == 1, "one hop deep, as the ask the entry profile would have made"
    assert request.owner is None, "the reply goes to the person, not to an asking agent"
    assert request.input == _request().input and request.session == _request().session
    record = routed.record
    assert (record.profile_id, record.fallback_reason) == (BILLING, None)
    assert (record.actor, record.on_behalf_of) == (PEER_SUBJECT_ID, "manager-7")
    assert routes.records == [record]


def test_a_delegated_route_reads_what_only_peers_may_read_and_the_person_still_cannot() -> None:
    policy = NarrowingPolicy(ASK_BILLING, PEER_READS)
    routed, _ = _route(_to(BILLING), policy=policy)

    delegated_rules = asyncio.run(policy.load_rules(routed.request.caller))
    person_rules = asyncio.run(policy.load_rules(PERSON))

    assert policy.decide(delegated_rules, "read_ledger", {}).effect is Effect.ALLOW
    assert policy.decide(person_rules, "read_ledger", {}).effect is Effect.DENY


def test_the_person_is_asked_about_ask_peer_to_the_target_never_the_delegated_identity() -> None:
    policy = NarrowingPolicy(ASK_BILLING, PEER_READS)

    _route(_to(BILLING), policy=policy)

    assert policy.load_rules_calls[0] == PERSON


def test_a_target_the_entry_profile_cannot_ask_is_refused_and_runs_the_person_as_before() -> None:
    routed, routes = _route(_to(STRANGER))

    assert routed.request == _request(), "the requested profile, as the person"
    record = routed.record
    assert record.profile_id == FRONT and not record.routed
    assert record.fallback_reason == f"{FALLBACK_PEER_REFUSED}:caller_does_not_allow_callee"
    assert (record.actor, record.on_behalf_of) == (None, None)
    assert routes.records == [record]


def test_a_target_that_does_not_accept_the_entry_profile_is_refused() -> None:
    routed, _ = _route(_to(SHY))

    assert routed.request == _request()
    assert routed.record.fallback_reason == f"{FALLBACK_PEER_REFUSED}:callee_does_not_allow_caller"


@pytest.mark.parametrize(
    ("rules", "effect"),
    [
        ((PEER_READS,), Effect.DENY),
        (
            (
                PolicyRule(
                    rule_id="ask-with-approval",
                    tool_pattern="ask_peer",
                    effect=Effect.NEEDS_APPROVAL,
                    reason="a person approves every ask",
                    subject_roles=frozenset({"manager"}),
                ),
            ),
            Effect.NEEDS_APPROVAL,
        ),
    ],
)
def test_a_person_whose_policy_does_not_allow_the_ask_is_not_routed(
    rules: tuple[PolicyRule, ...], effect: Effect
) -> None:
    routed, _ = _route(_to(BILLING), policy=NarrowingPolicy(*rules))

    assert routed.request == _request()
    assert routed.record.fallback_reason == f"{FALLBACK_ASK_PEER_NOT_ALLOWED}:{effect.value}"


def test_an_ask_rule_narrowed_to_another_peer_does_not_open_this_one() -> None:
    elsewhere = PolicyRule(
        rule_id="ask-only-shy",
        tool_pattern="ask_peer",
        effect=Effect.ALLOW,
        reason="only shy",
        subject_roles=frozenset({"manager"}),
        resource_pattern=SHY,
    )
    routed, _ = _route(_to(BILLING), policy=NarrowingPolicy(elsewhere))

    assert routed.request == _request()
    assert routed.record.fallback_reason == f"{FALLBACK_ASK_PEER_NOT_ALLOWED}:deny"


def test_without_a_peer_gate_a_route_is_never_applied() -> None:
    routed, _ = _route(_to(BILLING), gate=None)

    assert routed.request == _request()
    assert routed.record.fallback_reason == FALLBACK_DELEGATION_UNAVAILABLE


def test_a_pass_keeps_the_person_and_records_no_delegation() -> None:
    routed, _ = _route(RouteDecision(kind=RouteKind.PASS))

    assert routed.request == _request()
    assert (routed.record.actor, routed.record.on_behalf_of) == (None, None)


# --- an intent plan runs delegated ---------------------------------------------------------------

PLAN = IntentPlan(
    intent="balance", profile_id=BILLING, steps=(PlanStep(id="a", tool="read_ledger"),)
)
STRANGE_PLAN = IntentPlan(
    intent="gossip", profile_id=STRANGER, steps=(PlanStep(id="a", tool="read_ledger"),)
)


def test_an_intent_plan_runs_its_owner_delegated_on_behalf_of_the_person() -> None:
    routed, _ = _route(
        RouteDecision(kind=RouteKind.INTENT, intent="balance"), plans={"balance": PLAN}
    )

    assert routed.intent is not None and routed.intent.intent == "balance"
    assert routed.request.profile_id == BILLING
    assert routed.request.caller == delegated_identity(PERSON)
    assert (routed.record.actor, routed.record.on_behalf_of) == (PEER_SUBJECT_ID, "manager-7")


def test_an_intent_whose_owner_the_entry_profile_cannot_ask_runs_the_normal_path() -> None:
    routed, _ = _route(
        RouteDecision(kind=RouteKind.INTENT, intent="gossip"), plans={"gossip": STRANGE_PLAN}
    )

    assert routed.intent is None, "a refused plan never runs"
    assert routed.request == _request()
    assert routed.record.fallback_reason == f"{FALLBACK_PEER_REFUSED}:caller_does_not_allow_callee"


def test_a_peer_only_target_is_still_never_a_route_target() -> None:
    """The entry-point rule stays: delegation narrows where a route may land, never widens it."""
    profiles = _profiles()
    profiles[BILLING] = AgentProfile.from_mapping(
        {"id": BILLING, "persona": "p", "model": "m", "mode": AgentMode.PEER.value,
         "peers": _peers(FRONT)}
    )
    use_case = RouteTurn(
        routers=Router(_to(BILLING)),
        audit=Routes(),
        profiles=profiles,
        peer_gate=_gate,
        policy=NarrowingPolicy(ASK_BILLING),
    )

    routed = asyncio.run(use_case.execute(TurnId("turn-1"), _request()))

    assert routed.request == _request()
    assert routed.record.fallback_reason == "target_not_startable"


def test_the_route_guard_asks_about_the_same_tool_start_turn_offers() -> None:
    from agent_core.application import route_turn, start_turn

    assert route_turn.ASK_PEER_TOOL == start_turn.ASK_PEER_TOOL
