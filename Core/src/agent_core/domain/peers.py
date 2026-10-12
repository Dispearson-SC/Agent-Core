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
from datetime import datetime
from enum import StrEnum
from typing import Final, NewType
from uuid import UUID, uuid5

from agent_core.domain.answer_cache import AnswerCacheScope

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


# C8 (D35). Default of `PeerPolicy.max_answer_chars`: what the asking model may read of one peer
# answer (Pi's tool-output cap). The stored row keeps the answer verbatim.
DEFAULT_MAX_ANSWER_CHARS: Final[int] = 50_000

# C18 (D47). Default of `PeerPolicy.max_context_chars`: how much of the asker's conversation a
# `summary` or `full` peer is told (about a thousand tokens).
DEFAULT_MAX_CONTEXT_CHARS: Final[int] = 4_000

# C11 (D40). Defaults of `PeerPolicy.max_parallel_asks` and `max_concurrent_asks`: Pi's numbers. How
# many peer asks one model step may make, and how many of one asking turn run at the same time.
DEFAULT_MAX_PARALLEL_ASKS: Final[int] = 8
DEFAULT_MAX_CONCURRENT_ASKS: Final[int] = 4

# C19 (D49). Default of `PeerPolicy.max_background_asks`: how many background asks one
# conversation may have running at once.
DEFAULT_MAX_BACKGROUND_ASKS: Final[int] = 4

# C1o (D51). How many of a conversation's background asks `AgentMailbox.background_asks` returns at
# most (the newest). A listing is bounded; a COUNT or a lookup by handle is not, so a long-lived
# conversation never loses an open ask to the window.
BACKGROUND_LIST_LIMIT: Final[int] = 50

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

    `turn_id` is the asking workflow's id, `request_key` what names the ask inside the turn (since
    C13, D36: `r<round>.s<position>.<tool call id>`, so a repeat in a later round is a fresh ask).
    Target and question are part of the name too: if a provider ever reused a tool call id for a
    different question the two keys must not collide on the primary key. The fields are
    JSON-encoded as a list, so no choice of values can shift characters from one field into the
    next.

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


def peer_root_origin(session_id: str) -> str:
    """The case a session id stands for: itself, or the root inside a peer session id.

    Raises `ValueError` for an id containing `~` that is not `peer~<origin>~<uuid>`.
    """
    if "~" not in session_id:
        return session_id
    parts = session_id.split("~")
    if len(parts) == 3 and parts[0] == "peer" and parts[1] and parts[2]:
        return parts[1]
    raise ValueError(
        f"session id {session_id!r} contains '~' but is not a peer~<origin>~<uuid> id; "
        "'~' is reserved as the peer origin separator"
    )


def peer_session_name(root_origin: str, key: str) -> str:
    """The id of the conversation an ask's callee runs in: `peer~<root origin>~<key>`. Pure.

    The root origin is the case the ask belongs to (`peer_root_origin` of the asking session) and
    `key` is `peer_session_key(correlation_id)`, or the key of an earlier session when an ask
    CONTINUES one (C12).
    """
    if "~" in key:
        raise ValueError(f"a peer session key must not contain '~': {key!r}")
    return f"peer~{root_origin}~{key}"


def derived_peer_session(asking_session_id: str, correlation_id: str) -> str:
    """The session a FRESH ask's callee runs in: the asking case's root, plus the ask's own key."""
    return peer_session_name(peer_root_origin(asking_session_id), peer_session_key(correlation_id))


def names_a_session_of_this_case(session_name: str, asking_session_id: str) -> bool:
    """Whether `session_name` is a peer session id (`peer~<root>~<key>`) of the case the asking
    session belongs to. Pure and only about the NAME: that the session exists, and is this asker's
    own, is for the mailbox to check against the rows that made it (C12)."""
    try:
        root = peer_root_origin(asking_session_id)
    except ValueError:
        return False
    parts = session_name.split("~")
    return len(parts) == 3 and parts[0] == "peer" and parts[1] == root and bool(parts[2])


class PeerContinuationRefusedError(Exception):
    """An ask asked to continue a peer session it may not (C12, D45).

    Raised by a mailbox when the session is unknown, or belongs to another tenant, case, asking
    agent or target, or when the transport cannot continue one. The workflow turns it into a
    refused result the model can read; the reason never says which of those it was, so a guess
    learns nothing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PeerBackgroundRefusedError(Exception):
    """An ask asked to run in the background where it cannot (C19, D49).

    Raised by a mailbox whose transport cannot deliver a late answer to a conversation that has
    moved on (the A2A client). The workflow turns it into a refused result the model can read.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


PEER_WORKFLOW_NAMESPACE: Final[UUID] = UUID("5b0f6a3e-2c1d-5e47-9a68-7d3c4f1b8e20")
"""The fixed namespace of peer workflow ids. NEVER change it: it is part of every id already
issued, and a changed namespace names every in-flight peer workflow differently."""


def peer_workflow_id(correlation_id: str, generation: int = 0) -> str:
    """The durable id of the workflow that answers one ask. Pure.

    DETERMINISTIC ON PURPOSE (C1): starting it twice - a worker that claimed the row, died
    and claimed it again, or two workers racing - names the same workflow, and DBOS returns
    the existing one instead of running the callee's turn twice. It is also the callee's
    TURN ID (`_step_new_turn_id` reads the workflow id), so an approval for the callee's
    human is addressed to it.

    A UUID, ON PURPOSE (C1i, D33). The callee's turn id is persisted in `turns`, `human_requests`,
    the `audit_*` tables and `turn_reasoning`, all UUID columns, so a readable `peer-<id>` id (the
    C1 scheme) made its first `INSERT INTO turns` fail on real Postgres. The id is
    `uuid5(namespace, "<id>:<generation>")`.
    It is not reversible: nothing may recognise a peer workflow by its spelling, ask this function
    for the id instead.

    `generation` (C1g, D32) is the row's `redeliveries`. Once the lease has put the ask back
    because its workflow was stranded by a version bump, the next attempt has the next generation:
    the stranded one keeps its id (cancelled, with its history), and the new build starts a
    workflow that does not collide with it.
    """
    return str(uuid5(PEER_WORKFLOW_NAMESPACE, f"{correlation_id}:{generation}"))


def legacy_peer_workflow_id(correlation_id: str, generation: int = 0) -> str:
    """The id a `.4` build gave the workflow of this attempt: `peer-<id>` or `peer-<id>-r<n>`. Pure.

    Kept ONLY so a deploy of `.5` can still see (`DurablePeerTurnStarter.exists`), retire and
    cascade-cancel a callee a live `.4` executor started (C1k, D34). Nothing starts a workflow
    under this spelling any more (it cannot be persisted: D33).
    """
    return f"peer-{correlation_id}" if generation == 0 else f"peer-{correlation_id}-r{generation}"


def background_followup_turn_id(correlation_id: str) -> str:
    """The id of the turn that carries a background ask's answer back to its conversation. Pure.

    DETERMINISTIC ON PURPOSE (C19): the follow-up is enqueued under this workflow id, so a callee
    workflow that is replayed, or a second one that races it, names the same turn and DBOS runs it
    once. A UUID because it is a turn id (the turn tables are UUID columns, D33).
    """
    return str(uuid5(_PEER_ID_NAMESPACE, f"background:{correlation_id}"))


class BackgroundState(StrEnum):
    """Where a background ask is, from its owner conversation's point of view (C19, C20).

    QUEUED    - waiting for a worker (or for the asking turn's concurrency cap).
    RUNNING   - claimed; the callee is working or waiting for a person.
    ANSWERED  - the peer answered; the answer was (or is being) delivered as a follow-up.
    FAILED    - the peer's turn failed or was closed.
    CANCELLED - stopped by the owner, or the conversation was closed.
    """

    QUEUED = "queued"
    RUNNING = "running"
    ANSWERED = "answered"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BackgroundAsk:
    """One background ask as the conversation that owns it may see it (C20).

    `handle` is the correlation id. `question` is what was asked (the model wrote it, so it is the
    owner's own text), never the answer: an answer reaches the model only through the follow-up
    turn, inside the untrusted wrapper.
    """

    handle: str
    target: str
    state: BackgroundState
    question: str
    asked_at: datetime
    finished_at: datetime | None = None
    # The peer session the callee runs in: what a message to it continues (C20). None if unknown.
    session: str | None = None
    # The depth the ask travelled at (its asker's hop + 1) and the turn a person started that it
    # hangs from: what a message to the same child carries, so no verb needs the asking turn's own.
    hop: int = 0
    root_turn_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in (BackgroundState.QUEUED, BackgroundState.RUNNING)


@dataclass(frozen=True, slots=True)
class BackgroundKeys:
    """Asks one turn already keyed, to leave out of a count (C1o, D51).

    A step that re-runs after a crash finds the rows its first attempt wrote. Counted against the
    cap they would refuse the very asks that wrote them, so the count leaves them out and the step
    adds them back as its own. `keys` are the `request_key`s the step derives for its asks.
    """

    turn_id: str
    keys: tuple[str, ...]


class FanOutPolicy(StrEnum):
    """What one failed ask does to the other asks made in the same model step (C10, D39).

    ALL_SETTLED - every ask runs to its own end (answer, error or timeout) and the turn resumes
                  once with all of them. Default, and the behaviour before C10.
    FAIL_FAST   - the first ask that ENDS IN ERROR cancels its siblings still out (the C4 cancel)
                  and the turn resumes once, without waiting for them.
    """

    ALL_SETTLED = "all_settled"
    FAIL_FAST = "fail_fast"


class Topology(StrEnum):
    """The shape of who may ask whom, declared ONCE and checked at startup (C17, D42).

    STAR - the declaring profile is the hub; the profiles it may ask are spokes and a spoke may talk
           to nobody but the hub.
    TREE - the declaring profile is the root; every other profile has exactly one parent (the one
           that reached it) and may ask only its parent and its own children.
    MESH - any profile may ask any other, but every allowlist entry must be mutual.
    """

    STAR = "star"
    TREE = "tree"
    MESH = "mesh"


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
    # C8. The most characters of ONE peer answer the asking model is shown; the rest is cut and
    # the result says `truncated="true"`. Per asking profile, because it protects the asker's
    # context window.
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS
    # C10 (D39). What a failed ask does to its siblings in the same step. Part of the content hash.
    fan_out: FanOutPolicy = FanOutPolicy.ALL_SETTLED
    # C17 (D42). The topology this profile declares for the agents reachable from it, or None (no
    # check, the behaviour before C17). Declared by ONE profile: the hub or root. Part of the
    # content hash.
    topology: Topology | None = None
    # C11 (D40). Asks one model step may make; the rest are REFUSED with a result the model can
    # read. And asks of one asking turn that may RUN at once; the rest wait queued until one
    # answers. Both per asking profile, both part of the content hash.
    max_parallel_asks: int = DEFAULT_MAX_PARALLEL_ASKS
    max_concurrent_asks: int = DEFAULT_MAX_CONCURRENT_ASKS
    # C12 (D45). Whether this profile's `ask_peer` may CONTINUE a peer session it started earlier
    # (`continue_session`), so a follow-up sees the prior exchange. Off by default; per asking
    # profile; part of the content hash.
    allow_continuation: bool = False
    # C18 (D47). With `visibility` `summary` or `full`, the most characters of the asking
    # conversation a peer is told. Part of the content hash.
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    # C19 (D49). Whether this profile's `ask_peer` may run in the BACKGROUND (`background=true`):
    # the call returns a handle at once and the answer comes back later as a follow-up turn of the
    # same conversation. Off by default; part of the content hash. And how many such asks one
    # conversation may have running at once.
    allow_background: bool = False
    max_background_asks: int = DEFAULT_MAX_BACKGROUND_ASKS
    # C25 (D57). How long, in seconds, an `ok` answer THIS agent gives to a peer may be remembered
    # and handed to the next identical question instead of running it again; 0 (the default) is
    # off. It is the ANSWERING profile that opts in, because only it knows whether its answers
    # can be reused. `answer_cache_scope` says who may share an entry: the asking subject
    # (default) or any asker with the same roles. Both are part of the content hash.
    answer_cache_seconds: int = 0
    answer_cache_scope: AnswerCacheScope = AnswerCacheScope.SUBJECT

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
