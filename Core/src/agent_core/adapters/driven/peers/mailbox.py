"""Driven adapter: AgentMailbox over Postgres - the durable peer queue.

Phase:   F9 (foundations) / D2 (full A2A wire protocol)
Tasks:   docs/TASKS.md#t-f9-03
Status:  DONE - durable enqueue, exactly-once claim, idempotent answer, untrusted wrap
Tests:   Core/tests/integration/test_peer_mailbox.py
Implements: ports/agent_mailbox.py

NO WAITING HAPPENS HERE
    This adapter enqueues, claims and correlates. The durable wait is `DBOS.recv()` in the
    workflow, exactly as it is for `HumanGateway` - `ask_peer` is the third user of one
    suspension mechanism, not a new one. A sleep, a poll loop or a thread join in this
    file is a bug: it will silently lose the turn on the next deploy.

WHY THE QUEUE IS A TABLE
    An enqueued ask is a turn suspended on `DBOS.recv()`. An in-memory queue loses every
    pending ask on the next deploy, and a lost ask is a conversation that never resumes:
    nothing errors, nothing is logged, the agent simply waits forever. The table
    (migration `0014`, in adapters/driven/persistence_pg/peers_migration.py, so
    `migrations.py` stays a file no anchor shares) is what carries an ask across a
    restart.

EXACTLY ONCE HAS TWO HALVES, AND THEY FAIL DIFFERENTLY
    NOT ZERO  - durability, above.
    NOT TWICE - two independent duplication paths, each closed in a different place:

      A RETRIED `ask`. `ask` runs inside a DBOS step and a step is re-executed after a
      crash. Idempotence is the table's UNIQUE constraint plus `ON CONFLICT ... DO UPDATE
      ... RETURNING`, never a SELECT followed by an INSERT: two step attempts can both
      read "not enqueued" before either writes. The conflicting insert returns the
      correlation id already minted, so the retry is silent on the wire as well as in the
      table - the asking turn keeps waiting on the handle it already has.

      A CONCURRENT CLAIM. Several answering workers polling one queue. `claim_next` is a
      single `UPDATE ... WHERE correlation_id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT
      1) RETURNING`, so selecting the row, locking it and marking it delivered commit
      together. Split into a SELECT then an UPDATE, two workers read the same queued row
      before either writes it, and one question is handed to two peers - each of which
      runs a full turn with model calls and bills for it.

THE ANSWER IS UNTRUSTED CONTENT - CLAUDE.md non-negotiable 10
    A peer is a third party. It may have read a hostile page, or been told something by a
    person who was lying to it, and "it is our own agent" is not a trust argument. So
    `read_answer` returns the answer inside the SAME delimiters an `mcp_*` result gets -
    imported from the runner rather than re-typed here, because a delimiter that drifts
    between two modules is a boundary the model cannot see.

    The stored row keeps the peer's bytes verbatim and the wrapper is applied on read.
    Wrapping on write would make the audit trail disagree with what the peer actually
    said, and it would double-wrap the day a second reader is added.

    Any delimiter the peer's own text contains is removed before wrapping, case
    insensitively. A peer that ends its answer with a closing tag would otherwise close
    the boundary early, and everything it wrote afterwards would reach the model as
    trusted instructions - which is the entire attack.

SCOPE - THIS ADAPTER IS NOT THE GATE
    `PeerPolicy.may_ask` and `policy.max_hops` are NOT enforced here. They are
    docs/TASKS.md#t-f9-05, which owns adapters/driven/peers/hop_limit.py and applies both
    checks on both sides before a question ever becomes a hop. What this module owes that
    anchor is the evidence to decide on, so `hop` is persisted with the row rather than
    being dropped as an unused argument.

    `policy.visibility` needs no redaction at this layer for the same structural reason:
    `ask` is handed a question and nothing else, so there is no conversation context here
    to leak. Whatever assembles the question is where SUMMARY and FULL are decided.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from agent_core.adapters.driven.agent_pydantic.runner import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from agent_core.domain.peers import AgentId, AgentRef, PeerPolicy
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

# 32 bytes, so the handle carries 256 bits. A correlation id is what an inbound answer
# arrives with, and an answer routed into a turn is content the model will act on - the
# same bearer-token reasoning as `human/gateway.py`, and `secrets` for the same reason:
# never `random`, never a uuid4 rendered to look unguessable.
_CORRELATION_ID_BYTES: Final[int] = 32

# Both delimiters, matched case insensitively so a differently-cased tag cannot forge the
# boundary. The runner's own constants say the comparison has to be case insensitive; a
# peer that has read a hostile page will try exactly that.
_DELIMITER_RE: Final[re.Pattern[str]] = re.compile(
    f"{re.escape(UNTRUSTED_OPEN)}|{re.escape(UNTRUSTED_CLOSE)}", re.IGNORECASE
)

# What a forged delimiter is replaced with. Visible rather than blank, so a reader of the
# transcript can tell the peer tried it - a silently stripped attack looks like an
# ordinary answer.
_FORGED_DELIMITER: Final[str] = "[delimiter removed]"

_ENQUEUE_SQL = """
    INSERT INTO peer_messages
        (correlation_id, target_agent_id, from_session_id, from_tenant_id,
         turn_id, hop, question)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT uq_peer_messages_turn_target_question DO UPDATE
        SET target_agent_id = peer_messages.target_agent_id
    RETURNING correlation_id
"""

# One statement: select, lock and mark delivered commit together, so two workers can
# never both see the same row as queued. SKIP LOCKED lets a second worker take the NEXT
# ask instead of blocking behind the first - the queue is drained in parallel without
# ever handing one ask out twice. The tie-break on correlation_id makes the order total,
# so replay does not depend on which of two rows Postgres happened to write first.
_CLAIM_SQL = """
    UPDATE peer_messages
    SET state = 'delivered', delivered_at = now()
    WHERE correlation_id = (
        SELECT correlation_id
        FROM peer_messages
        WHERE target_agent_id = %s AND state = 'queued'
        ORDER BY enqueued_at, correlation_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING correlation_id, target_agent_id, from_session_id, from_tenant_id,
              turn_id, hop, question
"""

# `answered_at IS NULL` is the idempotency guard and it lives in the WHERE clause rather
# than in a preceding SELECT, for the reason every other guard in this module does: two
# deliveries can both read "unanswered" before either writes. A retried delivery matches
# no row and changes nothing - it does not raise, because a retry is normal.
_ANSWER_SQL = """
    UPDATE peer_messages
    SET answer = %s, state = 'answered', answered_at = now()
    WHERE correlation_id = %s AND answered_at IS NULL
"""

_READ_ANSWER_SQL = """
    SELECT answer
    FROM peer_messages
    WHERE correlation_id = %s AND answered_at IS NOT NULL
"""


@dataclass(frozen=True, slots=True)
class PeerAsk:
    """One ask claimed off the queue by the answering side.

    Carries `turn_id` and `from_session` because the answer has to find its way back to
    the turn that suspended; an ask that loses them is undeliverable however durable the
    row was. `hop` travels with it so t-f9-05's gate can refuse A -> B -> A on the far
    side too - a one-sided check is bypassable by whoever controls the other side.
    """

    correlation_id: str
    target: AgentId
    question: str
    from_session: SessionRef
    turn_id: TurnId
    hop: int


def wrap_peer_answer(answer: str) -> str:
    """A peer's answer as the model must see it: inside the untrusted delimiters.

    Exported rather than private because t-f9-04's `ask_peer` resumes a turn with this
    text and must not re-derive the wrapping - two implementations of one boundary is how
    a boundary stops being one.
    """
    return f"{UNTRUSTED_OPEN}\n{_DELIMITER_RE.sub(_FORGED_DELIMITER, answer)}\n{UNTRUSTED_CLOSE}"


class PgAgentMailbox:
    """Postgres adapter for `ports.agent_mailbox.AgentMailbox`.

    D13: every member is async and the psycopg calls underneath are synchronous (psycopg
    has no coroutine transaction support), wrapped in `asyncio.to_thread` exactly as
    `PgConversationStore` and `ChannelHumanGateway` do. The thread is also what makes the
    concurrent-claim path real: eight workers claiming at once are eight connections
    racing on `FOR UPDATE SKIP LOCKED`, which is what production looks like.
    """

    __slots__ = ("_conninfo",)

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
        """The peers this profile may talk to.

        The answer is the profile's own allowlist, not a registry lookup: a peer this
        profile was never given has nothing to advertise to it. Capability claims on the
        refs are HINTS for routing and never authorisation - a peer advertising
        `calendar.read` does not thereby gain the right to read a calendar, which is
        decided on its own side by its own policy.
        """
        return policy.peers

    async def ask(
        self,
        policy: PeerPolicy,
        target: AgentId,
        question: str,
        *,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
    ) -> str:
        """Enqueue durably and return the correlation id the answer will arrive with.

        Idempotent per (turn_id, target, question), so a re-executed DBOS step returns the
        handle it already minted instead of asking the peer a second time. See the module
        docstring for why `policy` is carried but not enforced here (docs/TASKS.md#t-f9-05
        is the gate) and why nothing needs redacting at this layer.
        """
        return await asyncio.to_thread(
            self._enqueue_sync, target, question, from_session, turn_id, hop
        )

    def _enqueue_sync(
        self,
        target: AgentId,
        question: str,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
    ) -> str:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            row = conn.execute(
                _ENQUEUE_SQL,
                (
                    secrets.token_urlsafe(_CORRELATION_ID_BYTES),
                    target,
                    from_session.session_id,
                    from_session.tenant_id,
                    turn_id,
                    hop,
                    question,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - DO UPDATE always returns the surviving row
            raise RuntimeError("peer ask was neither inserted nor found; the queue is unusable")
        return str(row[0])

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        """Take the oldest queued ask for `target`, or None when the queue is empty.

        Not part of `AgentMailbox`: the port describes the ASKING side, and this is the
        answering side of the same table. It stays on the adapter because the wire
        protocol decides it - D2's A2A adapter (docs/TASKS.md#t-d2-04) receives an HTTP
        task instead of polling a queue, and nothing above the port should have to change
        when it does.

        Claiming marks the row delivered. A worker that crashes between claiming and
        answering leaves it that way; recovering those is deliberately not built here,
        because a redelivery policy is a decision about duplicate model spend and belongs
        with the anchor that owns the retry story, not with the queue primitive.
        """
        return await asyncio.to_thread(self._claim_sync, target)

    def _claim_sync(self, target: AgentId) -> PeerAsk | None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            row: tuple[Any, ...] | None = conn.execute(_CLAIM_SQL, (target,)).fetchone()
        if row is None:
            return None
        # `turn_id` comes back as uuid.UUID from a UUID column; the domain type is the
        # string form every other table and every use case keys on.
        return PeerAsk(
            correlation_id=str(row[0]),
            target=AgentId(str(row[1])),
            from_session=SessionRef(
                session_id=SessionId(str(row[2])), tenant_id=TenantId(str(row[3]))
            ),
            turn_id=TurnId(str(row[4])),
            hop=int(row[5]),
            question=str(row[6]),
        )

    async def answer(self, correlation_id: str, answer: str) -> None:
        """Record the peer's answer against its correlation id. Idempotent.

        The first answer wins and later ones are no-ops rather than errors: a delivery is
        retried by the network and by DBOS alike, and a second attempt that OVERWROTE the
        answer would be a way to change what a suspended turn resumes with after the fact.

        An unknown correlation id is also a no-op. Handles expire and strangers deliver at
        whatever route fronts this; guessing which turn a stray answer belongs to resumes
        a conversation with content nobody asked for.

        Recording is all this does. Waking the workflow is `DBOS.send()`, in the workflow
        layer - see NO WAITING HAPPENS HERE at the top of this module.
        """
        await asyncio.to_thread(self._answer_sync, correlation_id, answer)

    def _answer_sync(self, correlation_id: str, answer: str) -> None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            conn.execute(_ANSWER_SQL, (answer, correlation_id))

    async def read_answer(self, correlation_id: str) -> str | None:
        """The peer's answer, WRAPPED as untrusted content, or None if it has not answered.

        Never returns the raw bytes. The stored row keeps them verbatim for the audit
        trail; every path that leads to a model goes through `wrap_peer_answer`.
        """
        raw = await asyncio.to_thread(self._read_answer_sync, correlation_id)
        if raw is None:
            return None
        return wrap_peer_answer(raw)

    def _read_answer_sync(self, correlation_id: str) -> str | None:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_READ_ANSWER_SQL, (correlation_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])
