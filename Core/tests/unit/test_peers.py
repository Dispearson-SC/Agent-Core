"""Peer access rules.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-01
Status:  t-f9-01 PINNED (PeerPolicy.may_ask). t-f9-07 still owes this module the
         hop-limit test (A -> B -> A refused) - do not fold it into the cases below.

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
