"""The peers an agent may ask THIS turn, for the `ask_peer` tool's description and enum (C14).

Phase:   Agent-comms v2, wave 2
Tests:   Core/tests/unit/test_peer_catalogue.py

Hard-coded lists of agents in a persona drift: a new specialist needs a prompt edit, and a peer
the policy forbids is still named. The catalogue is derived instead, by the same gate a call goes
through (`hop_limit.authorise_hop`, both sides' allowlists and switches) plus the caller's policy
rules for the tool, so what the model is offered is what it can actually do.

The hop COUNT is not consulted (`hop=0`): a resumed turn does not carry it, and the gate still
refuses a too-deep ask at call time with a reason the model can read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from agent_core.adapters.driven.peers.hop_limit import authorise_hop
from agent_core.adapters.driven.tools.context import PeerEntry
from agent_core.domain.peers import AgentId
from agent_core.domain.profile import AgentProfile

__all__ = ["build_peer_catalogue"]


def build_peer_catalogue(
    asker: AgentProfile,
    profiles: Mapping[str, AgentProfile],
    *,
    permits: Callable[[str], bool],
) -> tuple[PeerEntry, ...]:
    """The peers in `asker`'s allowlist that it may ask now, sorted by id. Pure.

    A peer is listed only if its profile is loaded, the two-sided gate would let the ask through,
    and `permits(peer id)` is true (the caller's policy does not DENY the tool for that target).
    The line is the callee's own `description`, else the display name the asker's allowlist gave
    it, plus the capabilities the allowlist advertises as hints.
    """
    listed: list[PeerEntry] = []
    for ref in asker.peers.peers:
        callee = profiles.get(str(ref.agent_id))
        if callee is None:
            continue
        decision = authorise_hop(
            caller=AgentId(asker.id),
            caller_policy=asker.peers,
            callee=ref.agent_id,
            callee_policy=callee.peers,
            hop=0,
            callee_mode=callee.mode,
        )
        if not decision.allowed or not permits(str(ref.agent_id)):
            continue
        summary = callee.description or ref.display_name
        if ref.capabilities:
            summary = f"{summary} (capabilities: {', '.join(ref.capabilities)})"
        listed.append(PeerEntry(agent_id=str(ref.agent_id), summary=summary))
    return tuple(sorted(listed, key=lambda entry: entry.agent_id))
