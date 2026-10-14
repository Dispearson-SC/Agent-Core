"""A delegation seat for `RouteTurn` tests that are not about delegation (S8).

`RouteTurn` applies a route only when the requested profile could itself ask the target: a peer
gate and the person's policy for `ask_peer`. Tests of OTHER routing rules pass these permissive
seats so a route lands; tests/unit/test_delegated_routing.py owns the refusals.
"""

from __future__ import annotations

from typing import Any

from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import CallerIdentity, TenantId
from tests.fakes.ports import FakeToolPolicy


def open_gate(entry: AgentProfile, target: AgentProfile, hop: int) -> tuple[int | None, str | None]:
    """A peer gate that lets every hop through, one deeper."""
    return hop + 1, None


ASK_ANYONE = PolicyRule(
    rule_id="test-ask-anyone",
    tool_pattern="ask_peer",
    effect=Effect.ALLOW,
    reason="the test's person may ask every peer",
)


def allowing_policy() -> FakeToolPolicy:
    return FakeToolPolicy(
        RuleSet.for_caller(CallerIdentity("u", "http", TenantId("t")), (ASK_ANYONE,))
    )


def delegation() -> dict[str, Any]:
    """The `RouteTurn` keyword arguments that let every route land delegated."""
    return {"peer_gate": open_gate, "policy": allowing_policy()}
