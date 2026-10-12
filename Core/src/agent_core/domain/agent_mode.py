"""What an agent is FOR in the topology: its mode (C27, D61). Pure.

A profile declares one mode; the default is `all`, so a profile that never says is unchanged.

    primary  - an entry point: a person or a channel starts it. It cannot be asked as a peer or
               spawned as a subagent.
    peer     - a specialist other agents ask (`ask_peer`). No channel starts it, and it is not
               spawned.
    subagent - a worker its parent spawns (`spawn_subagent`). No channel starts it, and it is not
               offered as a peer.
    all      - every role above (the behaviour before modes existed).

The mode answers WHO MAY REACH the agent. It never grants a reach: asking still needs both
sides' allowlists, and spawning needs the parent's `subagents` allowlist.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["AgentMode"]


class AgentMode(StrEnum):
    PRIMARY = "primary"
    PEER = "peer"
    SUBAGENT = "subagent"
    ALL = "all"

    @property
    def startable_by_channel(self) -> bool:
        """A person, a channel, the scheduler or the HTTP door may start a turn of this agent."""
        return self in (AgentMode.PRIMARY, AgentMode.ALL)

    @property
    def askable(self) -> bool:
        """Another agent may ask it (`ask_peer`), and the catalogue may list it."""
        return self in (AgentMode.PEER, AgentMode.ALL)

    @property
    def spawnable(self) -> bool:
        """A parent turn may spawn it as a subagent (`spawn_subagent`, C28)."""
        return self in (AgentMode.SUBAGENT, AgentMode.ALL)


class DelegationKind(StrEnum):
    """Which tool made a delegation: `ask_peer` (a PEER) or `spawn_subagent` (a SUBAGENT, C28).

    Both run the callee as a durable child workflow owned by the asking turn and resolve to the
    same C8 envelope; the kind decides the gate (allowlists and modes), the depth rules and how
    the ownership tree labels the child.
    """

    PEER = "peer"
    SUBAGENT = "subagent"
