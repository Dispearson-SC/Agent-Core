"""Peer access rules.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-01, docs/TASKS.md#t-f9-07
Status:  t-f9-01 PINNED (PeerPolicy.may_ask). t-f9-07 PINNED (round trip through the
         queue is refused at the hop limit) - see the cases at the bottom of this file.

WHY THIS FILE EXISTS AT ALL
    `peers` is an allowlist, so the empty case is the case that matters: a profile that
    was never handed a peer must be able to page NOBODY, not everybody. The asymmetry
    against `PolicyRule.subject_roles`, where empty means "any subject", is deliberate and
    is the same reading as `KnowledgePolicy.can_read` and `MediaPolicy.accepts`: a
    permissive default is acceptable for a convenience feature and unacceptable when the
    thing being reached is a person's personal-assistant agent.

    The check is two-sided (the asker checks it may ask, the answerer checks it accepts),
    so a single inverted empty branch here weakens BOTH sides at once.

    Nothing in this module says anything about trusting what a peer answers. A peer's
    answer is untrusted content (CLAUDE.md, non-negotiable 10); `may_ask` decides only
    whether the question may leave, never whether the reply may be believed.

    The domain is sync and imports nothing external (CLAUDE.md, "Layer rules"), so these
    cases construct a policy and call a method. No database, no fixtures, no I/O.
"""

from __future__ import annotations

# Imported as a MODULE so a missing name fails inside the test that needs it rather than
# at collection, taking the whole file down with it.
import agent_core.domain.peers as peers_module

# Ids a caller might plausibly hold, including the ones somebody would guess their way
# into: the wildcard, the empty string, and the policy's own likely owner.
CANDIDATE_AGENTS = (
    "personal-assistant",
    "customer-service",
    "billing",
    "self",
    "",
    "*",
    "default",
)


def _agent(agent_id: str) -> peers_module.AgentId:
    return peers_module.AgentId(agent_id)


def _ref(agent_id: str) -> peers_module.AgentRef:
    return peers_module.AgentRef(agent_id=_agent(agent_id), display_name=agent_id)


def test_empty_allowlist_may_ask_nobody() -> None:
    """The anchor: an empty `peers` denies every peer, not just the unknown ones.

    This is the default-constructed policy, which is what every profile that never
    mentions peers gets.
    """
    policy = peers_module.PeerPolicy()

    assert policy.peers == ()
    for agent_id in CANDIDATE_AGENTS:
        assert policy.may_ask(_agent(agent_id)) is False, agent_id


def test_the_default_policy_fails_closed_on_all_three_of_its_switches() -> None:
    """The defaults an `AgentProfile` that never mentions peers inherits, pinned as values.

    `enabled` and `peers` are already covered above through behaviour. `max_hops` is NOT,
    and nothing else in the tree covers it either: at the wave-12 barrier, widening the
    default from 2 to 99 left all 434 unit tests green. Every other case in this file and
    in `test_hop_limit.py` passes `max_hops` explicitly, so the declared default is
    unreachable from any assertion - a number that governs a loop nobody was watching.

    It has to be a LITERAL rather than "some small number". A hop is not a loop iteration:
    it is a complete turn with model calls on both sides, so raising this default two
    notches multiplies the worst-case cost of one question by four, and the only place
    that shows up is the invoice. Pinning the value makes widening it an edit somebody has
    to sign for; leaving it unpinned makes it a default nobody chose.
    """
    default = peers_module.PeerPolicy()

    assert default.enabled is False
    assert default.peers == ()
    assert default.max_hops == 2, (
        "PeerPolicy.max_hops moved. Every hop is a full turn with model calls on BOTH "
        "sides, so this default is a cost ceiling as much as a loop bound - raise it "
        "deliberately, with the reason written down, or not at all."
    )


def test_empty_allowlist_denies_even_when_the_feature_is_enabled() -> None:
    """`enabled` turns the feature on; it never grants an identity.

    Without this case an implementation could read the empty tuple as "unconfigured, so
    allow", which is exactly the inversion the empty branch invites.
    """
    policy = peers_module.PeerPolicy(enabled=True, peers=())

    for agent_id in CANDIDATE_AGENTS:
        assert policy.may_ask(_agent(agent_id)) is False, agent_id


def test_allowlisted_peer_is_the_only_one_that_may_be_asked() -> None:
    """Membership is the whole rule, so a listed peer passes and its neighbours do not.

    Pins the positive half too: a `may_ask` that always denied would satisfy the two cases
    above while making the feature unimplementable.
    """
    policy = peers_module.PeerPolicy(
        enabled=True,
        peers=(_ref("personal-assistant"), _ref("billing")),
    )

    assert policy.may_ask(_agent("personal-assistant")) is True
    assert policy.may_ask(_agent("billing")) is True
    for agent_id in ("customer-service", "", "*", "personal-assistant "):
        assert policy.may_ask(_agent(agent_id)) is False, agent_id


# ---------------------------------------------------------------------------------------
# t-f9-07: A -> B -> A is refused at the hop limit, through the ROUND TRIP.
#
# adapters/driven/peers/hop_limit.py (t-f9-05) and its own test_hop_limit.py already prove
# the mechanism: `authorise_hop` refuses a hop count that arrives at the limit. What is
# NOT proven there is that the count actually travels with the ask across a hop - the
# hop_limit module's own docstring says so and names this file as the anchor that owes it.
#
# So this section adds a minimal in-memory stand-in for the queue row that
# adapters/driven/peers/mailbox.py persists `hop` on (PgAgentMailbox.ask / claim_next).
# It is not PgAgentMailbox and it opens no connection - proving the ROUND TRIP is proving
# that `hop` read back off a row is the same `hop` an allowed decision handed out, not that
# Postgres is durable (test_peer_mailbox.py owns that). This is a test-only anchor: the
# fake below models the persisted shape and touches no production adapter.
# ---------------------------------------------------------------------------------------

from typing import Any  # noqa: E402

import agent_core.adapters.driven.peers.hop_limit as hop_limit_module  # noqa: E402
from agent_core.domain.peers import AgentRef, PeerPolicy  # noqa: E402
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId  # noqa: E402

_A = peers_module.AgentId("customer-service")
_B = peers_module.AgentId("personal-assistant")

_FROM_SESSION = SessionRef(session_id=SessionId("sess-1"), tenant_id=TenantId("tenant-1"))
_TURN_ID = TurnId("turn-1")


def _peer_policy(*, allows: tuple[peers_module.AgentId, ...], max_hops: int) -> PeerPolicy:
    return PeerPolicy(
        enabled=True,
        peers=tuple(AgentRef(agent_id=agent_id, display_name=str(agent_id)) for agent_id in allows),
        max_hops=max_hops,
    )


def _authorise_hop() -> Any:
    """`authorise_hop`, or an assertion naming the anchor this file is waiting on.

    Typed `Any` on purpose, matching test_hop_limit.py's own `_gate` helper: this file
    calls it with keyword arguments the real signature defines, and giving it a narrower
    type here would make mypy check this test against a signature it does not import.
    """
    gate = getattr(hop_limit_module, "authorise_hop", None)
    assert gate is not None, (
        "adapters/driven/peers/hop_limit.py exposes no `authorise_hop` (docs/TASKS.md#"
        "t-f9-05): t-f9-07 has nothing to prove the round trip against."
    )
    return gate


class _QueueRow:
    """What a persisted `peer_messages` row carries for this test's purposes: `hop` and
    nothing computed from it. Mirrors `adapters/driven/peers/mailbox.PeerAsk` in shape so
    the fake below reads back exactly what a real claim would - no local recomputation."""

    __slots__ = ("correlation_id", "target", "hop")

    def __init__(self, correlation_id: str, target: peers_module.AgentId, hop: int) -> None:
        self.correlation_id = correlation_id
        self.target = target
        self.hop = hop


class _InMemoryQueue:
    """A stand-in for PgAgentMailbox that stores exactly one field on purpose: `hop`.

    `enqueue` never derives the count it stores - it stores whatever `authorise_hop` handed
    back as `next_hop`, the same contract PgAgentMailbox.ask has (the `hop` keyword arg is
    persisted verbatim, mailbox.py line: "so `hop` is an argument"). `claim` hands the row
    back untouched. If a caller of this fake ever reset `hop` between enqueue and claim, the
    round trip below would stop proving anything - which is exactly the bug this test
    exists to catch in the real adapter.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _QueueRow] = {}
        self._next_id = 0

    def enqueue(self, target: peers_module.AgentId, hop: int) -> str:
        self._next_id += 1
        correlation_id = f"cid-{self._next_id}"
        self._rows[correlation_id] = _QueueRow(correlation_id, target, hop)
        return correlation_id

    def claim(self, correlation_id: str) -> _QueueRow:
        return self._rows[correlation_id]


def test_a_asks_b_then_b_asks_a_round_trip_is_refused_at_the_hop_limit() -> None:
    """The round trip: A's allowed ask enqueues `next_hop`, B claims that same row and
    tries to ask A back with the hop IT read off the row - and the cycle dies there.

    Both allowlists let the other party through and both share `max_hops=1`, so the ONLY
    thing that can refuse the return leg is the hop count that travelled with the row.
    """
    gate = _authorise_hop()
    a_policy = _peer_policy(allows=(_B,), max_hops=1)
    b_policy = _peer_policy(allows=(_A,), max_hops=1)
    queue = _InMemoryQueue()

    # A human turn starts the conversation: A -> B at hop 0.
    outbound = gate(caller=_A, caller_policy=a_policy, callee=_B, callee_policy=b_policy, hop=0)
    assert outbound.allowed is True
    assert outbound.next_hop == 1

    correlation_id = queue.enqueue(_B, outbound.next_hop)
    claimed = queue.claim(correlation_id)
    assert claimed.hop == 1, "the row must carry the count the gate handed out, not a fresh 0"

    # B, having claimed that ask, now tries to ask A back - using the hop OFF THE ROW,
    # never a hop it invents locally. That is the cycle: A -> B -> A.
    back = gate(
        caller=_B, caller_policy=b_policy, callee=_A, callee_policy=a_policy, hop=claimed.hop
    )

    assert back.allowed is False
    assert back.next_hop is None
    # The refusal must NAME the limit - a typed, identifiable reason a caller can branch
    # on and stop retrying - rather than reading like a transport failure (an exception, a
    # None, a bare "denied") that a retry policy would treat as worth trying again. `is`
    # against the specific enum MEMBER already rules out every one of its four siblings
    # (feature-disabled on either side, either allowlist refusal): a `StrEnum` has exactly
    # one member equal to itself, so this one assertion is the complete distinction.
    assert back.reason is hop_limit_module.HopRefusal.HOP_LIMIT_REACHED
    assert back.reason.value == "hop_limit_reached"


def test_the_round_trip_is_only_caught_because_the_row_carries_the_travelled_hop() -> None:
    """Contrast case: if the return ask used a LOCALLY recomputed hop (as if the queue row
    had reset the counter, e.g. by starting a "new" conversation from B's point of view)
    instead of the one claimed off the row, the very same cycle would be allowed forever.

    This is why `hop` has to be a field on the persisted row and not something either side
    derives - the hop_limit module's own docstring calls this out under "THE COUNT HAS TO
    TRAVEL", and this case is the negative space that makes the assertion above meaningful.
    """
    gate = _authorise_hop()
    a_policy = _peer_policy(allows=(_B,), max_hops=1)
    b_policy = _peer_policy(allows=(_A,), max_hops=1)

    locally_reset_hop = 0  # what B would see if it treated the inbound ask as fresh
    naive_return = gate(
        caller=_B, caller_policy=b_policy, callee=_A, callee_policy=a_policy, hop=locally_reset_hop
    )

    assert naive_return.allowed is True, (
        "a hop that does not travel with the row never reaches the limit - proving the "
        "positive case above actually depends on the row carrying the real count"
    )
