"""What a profile may spawn as a subagent (C28, D62). Pure.

A subagent is an agent the current turn starts to do one task and report back: a durable child
workflow owned by the turn, run as the subagent's own profile with the tools that profile has.
It shares the machinery of a peer ask (the mailbox row, the collect round, the timeouts, the
cancel cascade, the progress events and the C8 envelope); what differs is the gate:

  * the SPAWNER lists the agents it may spawn (`agents`; empty means nobody, never everybody);
  * the SPAWNED agent's `mode` must allow it (`AgentMode.spawnable`);
  * the SPAWNED agent must list the spawner in its own `accept_from` (both sides consent, as for a
    peer ask; empty means nobody);
  * `depth` bounds how many subagent links a chain may hold (C29).

`enabled` is the feature switch, off by default, like `PeerPolicy.enabled`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agent_core.domain.peers import AgentId

__all__ = ["DEFAULT_SUBAGENT_DEPTH", "SPAWN_SUBAGENT_TOOL", "SubagentPolicy"]

# The one level a chain may go down unless a profile says otherwise: a turn may spawn a subagent,
# and that subagent may not spawn another.
DEFAULT_SUBAGENT_DEPTH: Final[int] = 1

# The tool name, spelled in one place for the layers that must not import an adapter.
SPAWN_SUBAGENT_TOOL: Final[str] = "spawn_subagent"


@dataclass(frozen=True, slots=True)
class SubagentPolicy:
    """Per-profile subagent rules. Lives on `AgentProfile`. DISABLED BY DEFAULT."""

    enabled: bool = False
    agents: tuple[AgentId, ...] = ()
    # C29. The most subagent links a chain through this profile may hold (the stricter of the
    # spawner's and the spawned profile's number wins).
    depth: int = DEFAULT_SUBAGENT_DEPTH
    # R1 (C1r). Who may spawn THIS profile: the spawned agent's own consent, the counterpart of the
    # spawner's `agents`. Independent of `enabled` (a worker that spawns nothing still names its
    # spawners). Empty means nobody.
    accept_from: tuple[AgentId, ...] = ()

    def accepts(self, spawner: AgentId) -> bool:
        """Membership is the whole rule, as in `may_spawn`: nobody listed, nobody accepted."""
        return spawner in self.accept_from

    def may_spawn(self, agent_id: AgentId) -> bool:
        """Membership is the whole rule: an allowlist nobody was added to denies everything by
        construction, so there is no empty branch a later edit could invert into a wildcard."""
        return agent_id in self.agents
