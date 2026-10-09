"""A profile naming the agents it may ask - the seat every A2A mechanism was built for.

Phase:   F11
Tasks:   docs/TASKS.md#t-f11-14 (the loader), docs/TASKS.md#t-f11-15 (the shipped pair)
Subject: domain/profile.py, Core/profiles/support_triage.yaml,
         Core/profiles/billing_specialist.yaml

WHAT IS BEING PROVED HERE
    `PeerPolicy.may_ask` is an ALLOWLIST and `t-f9-01` pinned that empty means NOBODY.
    `_build_peer_policy` then excluded `peers` from the keys a profile may carry, so the
    allowlist could never be populated and every hop in the tree - the mailbox, the gate,
    `ask_peer`, the A2A adapter - was reachable only from a test. These cases are the
    configuration route: a mapping names peers, and the policy that comes out is the one
    `hop_limit.authorise_hop` already knows how to enforce.

    TWO-SIDED IS ASSERTED THROUGH THE GATE, NOT RESTATED AS A RULE. `may_ask` answers for
    one side only, so a test that called it twice would be asserting its own arithmetic.
    The cases below hand both loaded policies to the real gate and assert the asymmetric
    pair in both directions: B allowing A is not enough when A does not allow B.

    The shipped pair (t-f11-15) is then the claim F11 makes out loud - connecting one agent
    to another is a YAML change and nothing else. So those cases read the real files off
    disk rather than building a mapping that resembles them.

WHAT THIS SAYS NOTHING ABOUT
    Whether a peer's answer may be believed. It may not: a peer's reply is untrusted
    content whatever this allowlist said (CLAUDE.md, non-negotiable 10), and
    `mailbox.wrap_peer_answer` owns that boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Imported as MODULES so a missing name fails inside the test that needs it rather than at
# collection, taking the whole file down and reporting nothing.
import agent_core.adapters.driven.peers.hop_limit as hop_limit_module
import agent_core.domain.profile as profile_module
from agent_core.adapters.driven.profiles_fs.loader import load_profile_sync
from agent_core.domain.peers import AgentId

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

SUPPORT = AgentId("support_triage")
BILLING = AgentId("billing_specialist")


def _mapping(profile_id: str, peers: dict[str, Any] | None) -> dict[str, Any]:
    """The smallest profile that parses, plus whatever peer block a case is about."""
    data: dict[str, Any] = {
        "id": profile_id,
        "persona": "A test agent.",
        "model": "minimax/MiniMax-M3",
        "toolsets": ["delivery"],
    }
    if peers is not None:
        data["peers"] = peers
    return data


def _peer_entry(agent_id: str, **extra: Any) -> dict[str, Any]:
    return {"agent_id": agent_id, "display_name": agent_id.replace("_", " "), **extra}


def _profile(mapping: dict[str, Any]) -> Any:
    """`AgentProfile.from_mapping`, or an assertion naming the anchor that owes it."""
    try:
        return profile_module.AgentProfile.from_mapping(mapping)
    except profile_module.ProfileValidationError as error:
        raise AssertionError(
            "a profile cannot name a peer (docs/TASKS.md#t-f11-14): "
            f"`_build_peer_policy` refused the `peers` key - {error}. "
            "PeerPolicy.peers is therefore always empty, and empty means NOBODY, so the "
            "mailbox, the hop gate, `ask_peer` and the A2A adapter are all unreachable "
            "from configuration."
        ) from error


def _shipped(profile_id: str) -> Any:
    """One of the two profiles t-f11-15 ships, read from disk like production reads it."""
    path = PROFILES_DIR / f"{profile_id}.yaml"
    assert path.is_file(), (
        f"{path} does not exist (docs/TASKS.md#t-f11-15): nothing ships a relationship, "
        "so 'one agent orchestrating another is a YAML change' is an untested claim."
    )
    return load_profile_sync(path)


def _gate(*, caller: Any, callee: Any, hop: int) -> Any:
    """The real two-sided gate, handed two loaded profiles' policies."""
    return hop_limit_module.authorise_hop(
        caller=AgentId(caller.id),
        caller_policy=caller.peers,
        callee=AgentId(callee.id),
        callee_policy=callee.peers,
        hop=hop,
    )


def _refusal(name: str) -> Any:
    return getattr(hop_limit_module.HopRefusal, name)


# ---------------------------------------------------------------------------------------
# t-f11-14 - the loader
# ---------------------------------------------------------------------------------------


def test_a_profile_can_name_the_agents_it_may_ask() -> None:
    """The anchor: every field of an `AgentRef` survives the trip from configuration.

    `capabilities` and `endpoint` are carried because the A2A adapter reads both - a
    peer with no `endpoint` is an in-process peer reached through the durable mailbox,
    which is a different arrangement, not a missing value.
    """
    profile = _profile(
        _mapping(
            "asker",
            {
                "enabled": True,
                "peers": [
                    _peer_entry(
                        "billing_specialist",
                        capabilities=["billing.explain_charge"],
                        endpoint="https://billing.internal",
                    ),
                    _peer_entry("personal_assistant"),
                ],
            },
        )
    )

    policy = profile.peers
    assert policy.enabled is True
    assert [peer.agent_id for peer in policy.peers] == [
        "billing_specialist",
        "personal_assistant",
    ]
    assert policy.peers[0].display_name == "billing specialist"
    assert policy.peers[0].capabilities == ("billing.explain_charge",)
    assert policy.peers[0].endpoint == "https://billing.internal"
    assert policy.peers[1].capabilities == ()
    assert policy.peers[1].endpoint is None, "an unnamed endpoint must not be invented"

    assert policy.may_ask(BILLING) is True
    assert policy.may_ask(AgentId("personal_assistant")) is True


def test_a_peer_the_profile_did_not_name_is_refused() -> None:
    """An allowlist that admits an agent nobody wrote down is not an allowlist."""
    asker = _profile(
        _mapping("asker", {"enabled": True, "peers": [_peer_entry("billing_specialist")]})
    )
    stranger = _profile(
        _mapping("personal_assistant", {"enabled": True, "peers": [_peer_entry("asker")]})
    )

    assert asker.peers.may_ask(AgentId("personal_assistant")) is False

    decision = _gate(caller=asker, callee=stranger, hop=0)
    assert decision.allowed is False
    assert decision.reason is _refusal("CALLER_DOES_NOT_ALLOW_CALLEE")
    assert decision.next_hop is None


def test_the_allowlist_a_profile_produces_is_two_sided_in_practice() -> None:
    """B allowing A is not enough if A does not allow B - and the mirror case too.

    Both profiles are `enabled` and the hop count is 0, so the only thing left that can
    refuse either direction is whose file named whom. One-sided would mean an agent's
    profile is authorised by somebody else's file.
    """
    names_nobody = _profile(_mapping("support_triage", {"enabled": True, "peers": []}))
    names_support = _profile(
        _mapping("billing_specialist", {"enabled": True, "peers": [_peer_entry("support_triage")]})
    )

    # The callee's file says yes; the caller's file never said anything.
    outbound = _gate(caller=names_nobody, callee=names_support, hop=0)
    assert outbound.allowed is False
    assert outbound.reason is _refusal("CALLER_DOES_NOT_ALLOW_CALLEE")

    # The mirror: the caller's file says yes and the callee's never did.
    inbound = _gate(caller=names_support, callee=names_nobody, hop=0)
    assert inbound.allowed is False
    assert inbound.reason is _refusal("CALLEE_DOES_NOT_ALLOW_CALLER")


def test_an_unknown_key_inside_a_peer_entry_still_fails_loudly() -> None:
    """The discipline the exclusion was protecting has to survive the loader landing.

    A peer entry is an authorisation record. `agent_ids:` instead of `agent_id:` must not
    be absorbed into an empty allowlist that then silently refuses every hop.
    """
    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(
            _mapping(
                "asker",
                {"enabled": True, "peers": [{"agent_ids": "billing", "display_name": "b"}]},
            )
        )
    assert "agent_ids" in str(raised.value)


def test_a_peer_entry_missing_its_identity_fails_loudly() -> None:
    """A nameless peer is a typo, and it must not parse into an allowlist entry."""
    with pytest.raises(profile_module.ProfileValidationError) as raised:
        profile_module.AgentProfile.from_mapping(
            _mapping("asker", {"enabled": True, "peers": [{"display_name": "billing"}]})
        )
    assert "agent_id" in str(raised.value)


def test_the_peer_list_moves_the_content_hash() -> None:
    """D20: two profiles differing only in who they may ask are not the same profile.

    `content_hash` is what `ProfileVersionRegistry` assigns a version from, and a turn
    records that version. If the peer list were outside the hash, adding an agent to
    somebody's allowlist would be an unversioned change to what an agent may do.
    """
    without = _profile(_mapping("asker", {"enabled": True, "peers": []}))
    with_peer = _profile(
        _mapping("asker", {"enabled": True, "peers": [_peer_entry("billing_specialist")]})
    )

    assert without.content_hash != with_peer.content_hash


# ---------------------------------------------------------------------------------------
# t-f11-15 - the shipped relationship
# ---------------------------------------------------------------------------------------


def test_the_two_shipped_profiles_genuinely_ask_each_other() -> None:
    """The anchor: connecting one agent to another is a YAML change and nothing else.

    Read off disk through the production loader, so what is asserted is the file an
    operator would edit - not a mapping that resembles it.
    """
    support = _shipped("support_triage")
    billing = _shipped("billing_specialist")

    assert support.peers.enabled is True
    assert billing.peers.enabled is True
    assert support.peers.may_ask(BILLING) is True
    assert billing.peers.may_ask(SUPPORT) is True, (
        "the answer leg needs the callee's own allowlist; one-sided config cannot hop"
    )

    outbound = _gate(caller=support, callee=billing, hop=0)
    assert outbound.allowed is True
    assert outbound.reason is None
    assert outbound.next_hop == 1


def test_the_shipped_hop_limit_refuses_the_return_leg() -> None:
    """The limit is in force in the shipped files, not left at whatever the default is.

    A hop is a complete turn with model calls on both sides, so the escalation is one
    leg deep on purpose. The billing agent answers the question it was given; it cannot
    ask back and start a cycle.
    """
    support = _shipped("support_triage")
    billing = _shipped("billing_specialist")

    back = _gate(caller=billing, callee=support, hop=1)
    assert back.allowed is False
    assert back.reason is _refusal("HOP_LIMIT_REACHED")
    assert back.next_hop is None


def test_no_shipped_profile_names_a_peer_that_does_not_ship() -> None:
    """A dangling peer id is configuration that can never work, and nothing else says so."""
    shipped = {load_profile_sync(path).id for path in sorted(PROFILES_DIR.glob("*.yaml"))}
    assert shipped, f"no profile found under {PROFILES_DIR} - the walker is pointed at nothing"

    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        profile = load_profile_sync(path)
        for peer in profile.peers.peers:
            assert peer.agent_id in shipped, (
                f"{path.name} may ask {peer.agent_id!r}, and no profile defines it. "
                f"Shipped: {sorted(shipped)}"
            )
