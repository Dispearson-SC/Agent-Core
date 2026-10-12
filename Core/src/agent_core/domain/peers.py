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

import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType
from uuid import UUID, uuid5

AgentId = NewType("AgentId", str)

# The namespace of every id DERIVED for a peer ask (C2). A constant of the source: changing it
# changes every id, so it is never edited. Generated once with uuid4().
_PEER_ID_NAMESPACE: Final[UUID] = UUID("5b1c1f0e-6f6d-4d53-9a52-0d2c3f6f9a11")


# The per-ask answer secret (C1c): 32 random bytes, so the token carries 256 bits. One constant for
# every adapter, so the strength of the secret cannot drift between the Postgres queue and the A2A
# wire. It is a bearer token: `secrets`, never `random`, never a uuid4 rendered to look unguessable.
ANSWER_TOKEN_BYTES: Final[int] = 32


def new_answer_token() -> str:
    """A fresh, unguessable per-ask answer secret (URL-safe text of `ANSWER_TOKEN_BYTES` bytes)."""
    return secrets.token_urlsafe(ANSWER_TOKEN_BYTES)


# What a peer ask is answered with when the answering turn could not produce an answer. FIXED
# text: the exception message stays out of what the asking model reads, and a person's
# non-answer is not turned into a sentence an agent supposedly wrote.
PEER_ASK_FAILED_ANSWER: Final[str] = (
    "The question could not be answered: the answering agent failed."
)
PEER_ASK_CLOSED_ANSWER: Final[str] = (
    "The question could not be answered: the answering agent's turn was closed before it "
    "finished (nobody answered what it was waiting for)."
)


class PeerAnswerRefusedError(Exception):
    """An answer was NOT recorded: the row refused it.

    Raised by `AgentMailbox.answer` when the delivery does not hold the per-ask `answer_token`
    the row was minted with (the correlation id is derived and so is an address, not a
    credential, C1c) or when the ask was cancelled (C4). Nothing was written, so nothing may be
    woken. The message names the correlation id only: never the token, never the answer.
    """

    def __init__(self, correlation_id: str, reason: str = "the answer was refused") -> None:
        super().__init__(f"{reason} for peer ask {correlation_id!r}")
        self.correlation_id = correlation_id
        self.reason = reason


def peer_ask_id(turn_id: str, request_key: str, target: str, question: str) -> str:
    """The correlation id of one ask, derived from what identifies it. Pure.

    `turn_id` is the asking workflow's id, `request_key` the tool call that asked. Target and
    question are part of the name because the mailbox is also idempotent per
    `(turn, target, question)` (a word-for-word re-ask in a later round is the same ask, D28):
    if a provider ever reused a tool call id for a different question the two keys must not
    collide on the primary key. The fields are JSON-encoded as a list, so no choice of values
    can shift characters from one field into the next.

    A DBOS replay of the ask step recomputes the same id and so names the same row; nothing
    here needs a random source (CLAUDE.md non-negotiable #2).
    """
    return str(uuid5(_PEER_ID_NAMESPACE, json.dumps([turn_id, request_key, target, question])))


def peer_session_key(correlation_id: str) -> str:
    """The stable suffix of the session an ask's answering turn runs in. Pure.

    Derived from the correlation id, so a worker that picks the same row up again (after a
    crash) files the callee's turn under the same session, not a fresh one.
    """
    return str(uuid5(_PEER_ID_NAMESPACE, correlation_id))


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
