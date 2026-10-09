"""The gate that turns a question into a hop.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-05
Subject: adapters/driven/peers/hop_limit.py

WHAT IS BEING PROVED HERE
    Two refusals, and each one is bypassable if the other module is trusted to do it.

    THE COUNTER TRAVELS. A -> B -> A is not caught by counting anything either side
    holds locally: from B's point of view an ask arriving from A is the first ask it
    has seen. The only thing that can distinguish "A started a conversation" from
    "A is being asked back by the agent it just asked" is a number carried on the
    message itself, which is why `PeerAsk.hop` is persisted with the queue row
    (adapters/driven/peers/mailbox.py). A gate that recomputed the hop, or accepted a
    hop the caller re-derived, would let the cycle reset at every hop and run until
    somebody reads the bill - every hop being a full turn with model calls on both
    sides.

    BOTH ALLOWLISTS ARE CONSULTED. `PeerPolicy.may_ask` is one side of the answer.
    Asking only the callee's policy lets any agent that decided to trust B conscript
    B; asking only the caller's lets whoever writes A's profile page anybody. So the
    test asserts the asymmetric cases in both directions: one side allowing is never
    enough on its own.

    t-f9-07 owns the round trip through the mailbox in test_peers.py. This module
    proves the mechanism, not the journey, so nothing here touches a database.

    The subject is imported as a MODULE and its entry point is fetched through a
    guarded helper, so a missing implementation fails inside the test that needs it
    with a sentence saying what is missing - not as a collection error that takes the
    whole file down and reports nothing.
"""

from __future__ import annotations

from typing import Any

import agent_core.adapters.driven.peers.hop_limit as hop_limit_module
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy

A = AgentId("customer-service")
B = AgentId("personal-assistant")


def _ref(agent_id: AgentId) -> AgentRef:
    return AgentRef(agent_id=agent_id, display_name=str(agent_id))


def _policy(*, allows: tuple[AgentId, ...], max_hops: int = 2, enabled: bool = True) -> PeerPolicy:
    return PeerPolicy(
        enabled=enabled,
        peers=tuple(_ref(agent_id) for agent_id in allows),
        max_hops=max_hops,
    )


def _gate() -> Any:
    """`authorise_hop`, or an assertion naming the anchor that owes it."""
    gate = getattr(hop_limit_module, "authorise_hop", None)
    assert gate is not None, (
        "adapters/driven/peers/hop_limit.py exposes no `authorise_hop` "
        "(docs/TASKS.md#t-f9-05): nothing enforces the hop limit or the two-sided "
        "allowlist, so every peer ask is currently ungated."
    )
    return gate


def _refusal(name: str) -> Any:
    return getattr(hop_limit_module.HopRefusal, name)


def test_a_asks_b_then_b_asks_a_is_refused_at_the_hop_limit() -> None:
    """The anchor: the cycle dies at the limit, and the surviving hop is handed back.

    Both sides allow each other and both are enabled, so the ONLY thing that can
    refuse the second leg is the hop count arriving with it.
    """
    gate = _gate()
    a_policy = _policy(allows=(B,), max_hops=1)
    b_policy = _policy(allows=(A,), max_hops=1)

    outbound = gate(caller=A, caller_policy=a_policy, callee=B, callee_policy=b_policy, hop=0)
    assert outbound.allowed is True
    assert outbound.reason is None
    assert outbound.next_hop == 1, (
        "an allowed hop has to hand back the count the ask must travel with; "
        "without it the caller has nothing to attach to the message"
    )

    back = gate(
        caller=B, caller_policy=b_policy, callee=A, callee_policy=a_policy, hop=outbound.next_hop
    )
    assert back.allowed is False
    assert back.reason is _refusal("HOP_LIMIT_REACHED")
    assert back.next_hop is None


def test_the_same_return_ask_is_allowed_when_the_count_does_not_travel() -> None:
    """Why `hop` is an argument and not something this module could infer.

    Identical policies, identical pair, identical direction as the refused leg above -
    the only difference is a hop count of 0 instead of 1. A gate that reset the counter
    at each hop, or recomputed it locally, would take exactly this branch forever.
    """
    gate = _gate()
    a_policy = _policy(allows=(B,), max_hops=1)
    b_policy = _policy(allows=(A,), max_hops=1)

    assert gate(caller=B, caller_policy=b_policy, callee=A, callee_policy=a_policy, hop=0).allowed


def test_the_stricter_side_owns_the_hop_limit() -> None:
    """A caller cannot buy itself more hops by raising its own `max_hops`.

    The limit is two-sided for the same reason the allowlist is: whoever controls one
    profile must not be able to decide how deep a chain runs through the other.
    """
    gate = _gate()
    generous = _policy(allows=(B,), max_hops=99)
    strict = _policy(allows=(A,), max_hops=1)

    decision = gate(caller=A, caller_policy=generous, callee=B, callee_policy=strict, hop=1)
    assert decision.allowed is False
    assert decision.reason is _refusal("HOP_LIMIT_REACHED")


def test_the_callee_allowing_the_caller_is_not_enough() -> None:
    """B trusting A does not let A ask B. The asker's own allowlist has to say so too.

    This is the one-sided check that looks correct from B's side: B decided A may
    reach it. If that were the whole rule, an agent's profile would be authorised by
    somebody else's file.
    """
    gate = _gate()
    a_policy = _policy(allows=())  # A allowlists nobody - empty means nobody.
    b_policy = _policy(allows=(A,))

    decision = gate(caller=A, caller_policy=a_policy, callee=B, callee_policy=b_policy, hop=0)
    assert decision.allowed is False
    assert decision.reason is _refusal("CALLER_DOES_NOT_ALLOW_CALLEE")


def test_the_caller_allowing_the_callee_is_not_enough() -> None:
    """The mirror: A trusting B does not conscript B into answering.

    Without this half, any agent that put a person's personal assistant on its own
    list could page it.
    """
    gate = _gate()
    a_policy = _policy(allows=(B,))
    b_policy = _policy(allows=())

    decision = gate(caller=A, caller_policy=a_policy, callee=B, callee_policy=b_policy, hop=0)
    assert decision.allowed is False
    assert decision.reason is _refusal("CALLEE_DOES_NOT_ALLOW_CALLER")


def test_both_allowlists_together_are_what_opens_the_gate() -> None:
    """The positive case, so the refusals above are not passing for a shared reason."""
    gate = _gate()
    decision = gate(
        caller=A,
        caller_policy=_policy(allows=(B,)),
        callee=B,
        callee_policy=_policy(allows=(A,)),
        hop=0,
    )
    assert decision.allowed is True
    assert decision.next_hop == 1


def test_the_feature_switch_is_enforced_on_both_sides() -> None:
    """`may_ask` deliberately ignores `enabled`; this gate is where it is read.

    domain/peers.py says so in as many words, and names this anchor. Both defaults
    already fail closed, and either side switching peers off closes the gate.
    """
    gate = _gate()
    enabled_a = _policy(allows=(B,))
    enabled_b = _policy(allows=(A,))
    off_a = _policy(allows=(B,), enabled=False)
    off_b = _policy(allows=(A,), enabled=False)

    caller_off = gate(caller=A, caller_policy=off_a, callee=B, callee_policy=enabled_b, hop=0)
    assert caller_off.allowed is False
    assert caller_off.reason is _refusal("CALLER_PEERS_DISABLED")

    callee_off = gate(caller=A, caller_policy=enabled_a, callee=B, callee_policy=off_b, hop=0)
    assert callee_off.allowed is False
    assert callee_off.reason is _refusal("CALLEE_PEERS_DISABLED")
