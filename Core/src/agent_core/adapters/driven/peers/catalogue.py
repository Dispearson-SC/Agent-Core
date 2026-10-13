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

from agent_core.adapters.driven.peers.hop_limit import authorise_hop, authorise_spawn
from agent_core.adapters.driven.tools.context import PeerEntry
from agent_core.domain.peers import AgentId
from agent_core.domain.profile import AgentProfile

__all__ = ["build_peer_catalogue", "build_subagent_catalogue"]


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


def build_subagent_catalogue(
    spawner: AgentProfile,
    profiles: Mapping[str, AgentProfile],
    *,
    permits: Callable[[str], bool],
) -> tuple[PeerEntry, ...]:
    """The agents in `spawner`'s `subagents.agents` that it may spawn now, sorted by id. Pure.

    Listed only if the profile is loaded, the spawn gate would let it through (the spawner's
    switch and allowlist, the agent's mode) and `permits(agent id)` is true (the caller's policy
    does not DENY `spawn_subagent` for that agent; deny wins over any allow). The line is the
    agent's own `description`, else its id.
    """
    listed: list[PeerEntry] = []
    for agent_id in spawner.subagents.agents:
        callee = profiles.get(str(agent_id))
        if callee is None:
            continue
        decision = authorise_spawn(
            caller=AgentId(spawner.id),
            caller_policy=spawner.subagents,
            callee=agent_id,
            callee_mode=callee.mode,
            hop=0,
            depth=0,
            callee_subagents=callee.subagents,
        )
        if not decision.allowed or not permits(str(agent_id)):
            continue
        listed.append(
            PeerEntry(agent_id=str(agent_id), summary=callee.description or str(agent_id))
        )
    return tuple(sorted(listed, key=lambda entry: entry.agent_id))
