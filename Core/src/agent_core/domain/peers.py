"""Peer vocabulary - one agent asking another.

Phase:   F9 (foundations) / D2 (full A2A)
Tasks:   docs/TASKS.md#t-f9-01
Status:  TYPES DEFINED / PeerPolicy.may_ask DONE (t-f9-01)

THE SCENARIO THIS EXISTS FOR
    A customer-service agent cannot answer from its Skills or KnowledgeBase. It asks the
    person's personal-assistant agent. That agent suspends its OWN turn to ask the human,
    and the answer flows back.

    Two nested suspensions, and NO new suspension machinery: `ask_peer` is an
    externally-executed deferred tool, exactly like `request_evidence` and exactly like an
    approval. Third user of DeferredToolRequests + DBOS.recv().

A2A ALIGNMENT - SHAPE NOW, PROTOCOL LATER
    Agent2Agent reached v1.0 under the Linux Foundation, backed by Google, Microsoft and
    Salesforce, but adoption is measured rather than universal. So: model these types on
    A2A's concepts - capability discovery, tasks, modality negotiation - WITHOUT
    implementing the wire protocol on day 1. D2 completes it.

    Hermes' own A2A plugin is 2,345 lines. Aligning the shape costs nothing now; adopting
    the protocol later then costs an adapter instead of a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

AgentId = NewType("AgentId", str)


class PeerVisibility(StrEnum):
    """How much of a conversation a peer may be told about.

    NONE      - the peer gets the question only. Default.
    SUMMARY   - plus a summary of why it is being asked.
    FULL      - plus conversation context. Rarely correct across a trust boundary.
    """

    NONE = "none"
    SUMMARY = "summary"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class AgentRef:
    """A reachable peer. `capabilities` is A2A-shaped: what this peer claims it can answer.

    Claims are HINTS, never authorisation. A peer advertising `calendar.read` does not gain
    the right to read a calendar; that is decided on the peer's own side by its own policy."""

    agent_id: AgentId
    display_name: str
    capabilities: tuple[str, ...] = ()
    endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class PeerPolicy:
    """Per-profile peer rules. Lives on AgentProfile. DISABLED BY DEFAULT.

    `max_hops` bounds the agent-to-agent version of an infinite loop: A asks B, B asks A,
    forever. It is expensive in a way a normal loop is not, because every hop is a complete
    turn with model calls on both sides.

    `visibility` is a PRIVACY boundary. A customer-service agent must not be able to ask a
    personal assistant "what does this person have scheduled next month" and go fishing.
    Default NONE means the peer receives the question and nothing else."""

    enabled: bool = False
    peers: tuple[AgentRef, ...] = ()
    max_hops: int = 2
    visibility: PeerVisibility = PeerVisibility.NONE
    reply_timeout_seconds: int = 3600

    def may_ask(self, agent_id: AgentId) -> bool:
        """Whether this profile may address `agent_id`. Empty `peers` means nobody.

        Not every agent should be able to page a person's personal assistant. This is an
        allowlist, and it is checked on BOTH sides: the asker checks it may ask, the
        answerer checks it accepts being asked. One-sided checks are bypassable by
        whoever controls the other side.

        Empty means NOTHING, not everything - the same reading as
        KnowledgePolicy.can_read and MediaPolicy.accepts, and the opposite of
        PolicyRule.subject_roles. The asymmetry is deliberate and it is tested: a
        permissive default is acceptable for a convenience feature and unacceptable when
        the thing being reached is somebody's personal-assistant agent.

        Membership is the whole rule, and it is written as membership rather than as a
        special case for the empty tuple: an allowlist nobody was added to denies
        everything by construction, so there is no empty branch a later edit could invert
        into a wildcard.

        `enabled` is NOT consulted here, matching KnowledgePolicy.can_read. It is the
        feature switch, not an identity rule, and the caller that turns a question into a
        hop enforces it alongside the hop limit (docs/TASKS.md#t-f9-05). Both defaults
        already fail closed: disabled, and nobody allowlisted.

        Says nothing about the reply. A peer's answer is untrusted content regardless of
        how the peer got onto this list (CLAUDE.md, non-negotiable 10).
        """
        return any(peer.agent_id == agent_id for peer in self.peers)
