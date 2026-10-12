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

    `read_answer` is ON THE PORT (docs/TASKS.md#t-f9-09), not an extra this class happened
    to grow. It was an extra once, and so was `A2AAgentMailbox`'s, which meant application
    code had to name a concrete adapter to redeem a handle `ask` had given it - the port
    cut leaking. The wrapping is part of that contract rather than a detail of this file:
    `ports/agent_mailbox.py` requires every implementation to return the answer wrapped,
    so no caller typed on the port can be handed raw peer bytes.

    The stored row keeps the peer's bytes verbatim and the wrapper is applied on read.
    Wrapping on write would make the audit trail disagree with what the peer actually
    said, and it would double-wrap the day a second reader is added.

    Any delimiter the peer's own text contains is removed before wrapping, case
    insensitively. A peer that ends its answer with a closing tag would otherwise close
    the boundary early, and everything it wrote afterwards would reach the model as
    trusted instructions - which is the entire attack.

WHO ASKED TRAVELS TOO, AND THAT IS THE SECOND LOCK (t-f11-48)
    A claimed row used to carry the asking SESSION, the turn and the hop - and no asking
    `AgentId`. So `hop_limit.authorise_hop`'s callee-side check,
    `callee_policy.may_ask(caller)`, could not be re-run on arrival: the answering agent
    took the question on trust because the asking side said it was allowed. The asking
    side's gate is real and still applies, so that was never an open door; it is defence
    in depth, and a two-sided check enforced on one side is a one-sided check with extra
    words. CLAUDE.md non-negotiable #10 is the reason to want the second lock: a peer that
    was itself misled is a confused deputy, and what it sends arrives with friendly
    provenance.

    So `ask` accepts `asker` and persists it in `peer_messages.from_agent_id` (migration
    `0025`, adapters/driven/persistence_pg/peer_asker_migration.py), and `claim_next`
    hands it back as `PeerAsk.asker`.

    `from_session` is not that identity under another name. A session names a
    conversation, not the profile the asking turn ran under, and deriving one from the
    other would assert an identity nobody recorded.

    `asker` IS OPTIONAL AT THE SIGNATURE AND REQUIRED IN MEANING. `ports/agent_mailbox.py`
    cannot carry it yet, so a caller typed on the port omits it and the row records NULL -
    which is honest about what is known, and logged at WARNING because a peer ask whose
    caller is unknown is a security check that cannot run. NULL is an ABSENCE: it never
    means "anyone may ask", and nothing anywhere backfills it.

SCOPE - THIS ADAPTER IS NOT THE GATE
    `PeerPolicy.may_ask` and `policy.max_hops` are NOT enforced here. They are
    docs/TASKS.md#t-f9-05, which owns adapters/driven/peers/hop_limit.py and applies both
    checks on both sides before a question ever becomes a hop. What this module owes that
    anchor is the evidence to decide on, so `hop` and `asker` are persisted with the row
    rather than being dropped as unused arguments. Recording who asked is not checking it:
    the answering side runs `authorise_hop` with what this row carries.

    `policy.visibility` needs no redaction at this layer for the same structural reason:
    `ask` is handed a question and nothing else, so there is no conversation context here
    to leak. Whatever assembles the question is where SUMMARY and FULL are decided.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import psycopg

from agent_core.adapters.driven.agent_pydantic.runner import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from agent_core.domain.peers import (
    AgentId,
    AgentRef,
    PeerAnswerRefusedError,
    PeerContinuationRefusedError,
    PeerPolicy,
    derived_peer_session,
    names_a_session_of_this_case,
    new_answer_token,
    peer_ask_id,
    peer_root_origin,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

# 32 bytes, so the handle carries 256 bits. A correlation id is what an inbound answer
# arrives with, and an answer routed into a turn is content the model will act on - the
# same bearer-token reasoning as `human/gateway.py`, and `secrets` for the same reason:
# never `random`, never a uuid4 rendered to look unguessable.
_CORRELATION_ID_BYTES: Final[int] = 32

_LOG = logging.getLogger(__name__)

# Both delimiters, matched case insensitively so a differently-cased tag cannot forge the
# boundary. The runner's own constants say the comparison has to be case insensitive; a
# peer that has read a hostile page will try exactly that.
_DELIMITER_RE: Final[re.Pattern[str]] = re.compile(
    # The `<peer>` envelope tags too (C8): a peer must not close the tag its answer sits in.
    f"{re.escape(UNTRUSTED_OPEN)}|{re.escape(UNTRUSTED_CLOSE)}|</?peer\\b",
    re.IGNORECASE,
)

# What a forged delimiter is replaced with. Visible rather than blank, so a reader of the
# transcript can tell the peer tried it - a silently stripped attack looks like an
# ordinary answer.
_FORGED_DELIMITER: Final[str] = "[delimiter removed]"

# A KEYED ask (C13) is idempotent through its primary key: the correlation id is derived from the
# request key, which names the round and position of the ask, so a replay hits the same row while
# the same question in a later round is a new one. An UNKEYED ask has a random id, and stays
# idempotent per (turn, target, question) through the partial unique index of migration 0031.
_ENQUEUE_COLUMNS = """
    INSERT INTO peer_messages
        (correlation_id, target_agent_id, from_session_id, from_tenant_id,
         turn_id, hop, question, from_agent_id, answer_token, request_key, max_concurrent,
         peer_session_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_ENQUEUE_KEYED_SQL = (
    _ENQUEUE_COLUMNS
    + """
    ON CONFLICT (correlation_id) DO UPDATE
        SET target_agent_id = peer_messages.target_agent_id
    RETURNING correlation_id
"""
)
_ENQUEUE_UNKEYED_SQL = (
    _ENQUEUE_COLUMNS
    + """
    ON CONFLICT (turn_id, target_agent_id, question) WHERE request_key IS NULL DO UPDATE
        SET target_agent_id = peer_messages.target_agent_id
    RETURNING correlation_id
"""
)

# THE CLAIM (C11, D40, C1m). Candidates are the oldest queued asks for the target, read without a
# lock; each is then claimed by ONE guarded UPDATE (`state = 'queued'` in its WHERE), so two workers
# can never both take the same ask: the loser blocks on the row lock, re-reads the row as delivered
# and matches nothing. An ask whose asking turn already has `max_concurrent` asks RUNNING
# (delivered, unanswered, across every target) is not claimable and stays queued, so the cap is a
# queue and not a refusal.
#
# The candidate query already leaves out the asks of a turn that is at its cap, so a backlog of
# capped asks can never fill a scan window and hide an uncapped turn's ask behind it. That count
# is read without a lock and can be stale, so it is repeated under a per-turn advisory lock before
# the ask is taken: two workers claiming different asks of one turn at the same moment cannot both
# see "one slot free". The lock is taken for the ONE ask the transaction means to claim and for no
# other: a transaction that held the locks of every turn it scanned, skipped ones included, could
# wait for a turn's lock while a second worker (another target, the same two turns the other way
# round) waits for its own, and Postgres would abort one of them with a deadlock. When the recount
# under the lock finds the turn at its cap after all, the transaction ends (releasing the lock),
# that turn is excluded, and the claim starts again. NULL `max_concurrent` (a row written before
# migration 0033) is never held. The tie-break on correlation_id keeps the order total, so replay
# does not depend on which of two rows Postgres happened to write first.
_CLAIM_CANDIDATE_SQL = """
    SELECT m.correlation_id, m.turn_id, m.max_concurrent
    FROM peer_messages m
    WHERE m.target_agent_id = %s AND m.state = 'queued'
      AND m.turn_id <> ALL(%s)
      AND (m.max_concurrent IS NULL OR m.max_concurrent > (
            SELECT count(*) FROM peer_messages r
            WHERE r.turn_id = m.turn_id AND r.state = 'delivered' AND r.answered_at IS NULL))
    ORDER BY m.enqueued_at, m.correlation_id
    LIMIT 1
"""
_TURN_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_RUNNING_SQL = """
    SELECT count(*) FROM peer_messages
    WHERE turn_id = %s AND state = 'delivered' AND answered_at IS NULL
"""
_CLAIM_ONE_SQL = """
    UPDATE peer_messages
    SET state = 'delivered', delivered_at = now()
    WHERE correlation_id = %s AND state = 'queued'
    RETURNING correlation_id, target_agent_id, from_session_id, from_tenant_id,
              turn_id, hop, question, from_agent_id, answer_token, redeliveries, callee_failures,
              peer_session_id
"""
# `answered_at IS NULL` is the idempotency guard and it lives in the WHERE clause rather
# than in a preceding SELECT, for the reason every other guard in this module does: two
# deliveries can both read "unanswered" before either writes. A retried delivery matches
# no row and changes nothing - it does not raise, because a retry is normal.
#
# C1c: the delivery must present the row's `answer_token` (`IS NOT DISTINCT FROM`, so a legacy
# row with a NULL token is answered with None and a forged None against a real token matches
# nothing). The guard is in the WHERE clause for the same race reason as `answered_at`.
_ANSWER_SQL = """
    UPDATE peer_messages
    SET answer = %s, answer_failed = %s, state = 'answered', answered_at = now()
    WHERE correlation_id = %s AND answered_at IS NULL AND state <> 'cancelled'
      AND answer_token IS NOT DISTINCT FROM %s
"""

# Why a delivery that changed no row changed none: nothing there (a stranger), already
# answered (a retry), or still open and refused (a forged token). Only the last raises.
_ANSWER_STATE_SQL = (
    "SELECT answered_at IS NOT NULL, state FROM peer_messages WHERE correlation_id = %s"
)

# C4. Open (queued or delivered, unanswered) -> cancelled. One guarded statement, like the
# answer: an answer that wins the race leaves nothing for the cancel to change, and a cancel
# that wins leaves nothing for the answer (its WHERE excludes `cancelled`).
_CANCEL_SQL = """
    UPDATE peer_messages
    SET state = 'cancelled', cancel_reason = %s, cancelled_at = now()
    WHERE correlation_id = %s AND state IN ('queued', 'delivered') AND answered_at IS NULL
    RETURNING correlation_id
"""

_CANCEL_OPEN_SQL = """
    UPDATE peer_messages
    SET state = 'cancelled', cancel_reason = %s, cancelled_at = now()
    WHERE turn_id = %s AND state IN ('queued', 'delivered') AND answered_at IS NULL
    RETURNING correlation_id
"""

_IS_CANCELLED_SQL = "SELECT state = 'cancelled' FROM peer_messages WHERE correlation_id = %s"
_REDELIVERIES_SQL = "SELECT redeliveries FROM peer_messages WHERE correlation_id = %s"
_ANSWER_FAILED_SQL = (
    "SELECT answer_failed FROM peer_messages WHERE correlation_id = %s AND answered_at IS NOT NULL"
)

# C3. Candidates: delivered, unanswered, past the lease (`delivered_at + lease < now`). The
# database's clock decides unless a test passes `now`. Oldest first, bounded so one pass never
# loads the whole backlog.
_STALE_SQL = """
    SELECT correlation_id, redeliveries
    FROM peer_messages
    WHERE target_agent_id = %s AND state = 'delivered' AND answered_at IS NULL
      AND delivered_at < COALESCE(%s::timestamptz, now()) - %s
    ORDER BY delivered_at, correlation_id
    LIMIT %s
"""

# The requeue repeats every guard of the candidate query: between the listing and this UPDATE
# the row may have been answered, or reclaimed by another worker. Only the statement that
# changes the row decides, so two workers can never both reclaim the same delivery. The listing's
# `redeliveries` is part of the guard: it names the generation of the workflow that was judged
# dead, and a row already requeued (and so a generation further) is not requeued again on a
# stale verdict.
_REQUEUE_SQL = """
    UPDATE peer_messages
    SET state = 'queued', delivered_at = NULL, redeliveries = redeliveries + 1,
        callee_failures = callee_failures + %s
    WHERE correlation_id = %s AND redeliveries = %s AND state = 'delivered'
      AND answered_at IS NULL
      AND delivered_at < COALESCE(%s::timestamptz, now()) - %s
    RETURNING correlation_id
"""

# C12. The rows that created a peer session, for the ownership check of a continuation.
_SESSION_OWNERS_SQL = """
    SELECT from_session_id, from_agent_id FROM peer_messages
    WHERE peer_session_id = %s AND target_agent_id = %s AND from_tenant_id = %s
"""
_CONTINUATION_REFUSED: Final[str] = (
    "that session cannot be continued: it is not one this agent started with this peer"
)


def _owns(
    row_session_id: object, row_asker: object, asking_session_id: str, asker: str
) -> bool:
    """Whether an earlier ask (its asking session and agent) belongs to the same case and the same
    asking agent as the one that wants to continue it."""
    if row_asker is None or str(row_asker) != asker:
        return False
    try:
        return peer_root_origin(str(row_session_id)) == peer_root_origin(asking_session_id)
    except ValueError:
        return False


_RECLAIM_BATCH: Final[int] = 50

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

    `asker` is the same argument, for the other half of that gate (t-f11-48): without the
    caller's `AgentId` the answering side cannot re-run `callee_policy.may_ask(caller)`
    and accepts the question because the asking side said it was allowed.

    `asker is None` MEANS UNKNOWN, NEVER "ANYONE". The column is nullable because rows
    enqueued before migration `0025` never recorded one, and nothing backfills them - a
    manufactured identity in an audit-adjacent table is a claim nobody made. An answering
    side holding None has no caller to check and therefore no basis to admit the question:
    fail closed, exactly as `PeerPolicy.may_ask` does on an empty allowlist. It may not
    substitute `from_session`, which names a conversation and not the profile the asking
    turn ran under.
    """

    correlation_id: str
    target: AgentId
    question: str
    from_session: SessionRef
    turn_id: TurnId
    hop: int
    asker: AgentId | None = None
    # C1c. The per-ask secret the answering side must present to `answer`. repr=False so a
    # log line or traceback of the ask never prints it.
    answer_token: str | None = field(default=None, repr=False)
    # C1g. How many times the lease put this ask back on the queue. It is the GENERATION of the
    # callee workflow: generation n runs `peer_workflow_id(<id>, n)`.
    redeliveries: int = 0
    # C1l. How many of those requeues followed a callee that FAILED (a workflow that ended in
    # ERROR). The redelivery cap counts these only: a worker crash or a retire during a deploy is
    # infrastructure, and must not abandon the ask.
    failures: int = 0
    # C12. The session the callee runs in when it is NOT the one derived from the correlation id:
    # the ask continues an earlier one. None for a legacy row; for a fresh ask it equals the derived
    # name.
    peer_session_id: str | None = None


def wrap_peer_answer(answer: str) -> str:
    """A peer's answer as the model must see it: inside the untrusted delimiters.

    Exported rather than private because t-f9-04's `ask_peer` resumes a turn with this
    text and must not re-derive the wrapping - two implementations of one boundary is how
    a boundary stops being one.
    """
    return f"{UNTRUSTED_OPEN}\n{_DELIMITER_RE.sub(_FORGED_DELIMITER, answer)}\n{UNTRUSTED_CLOSE}"


@dataclass(frozen=True, slots=True)
class CappedAnswer:
    """A wrapped answer after the asker's size cap (C8).

    `wrapped` is what the model reads, `inner` the (possibly cut) peer text without the
    delimiters, `truncated` whether anything was cut."""

    wrapped: str
    inner: str
    truncated: bool


def cap_wrapped_answer(wrapped: str, max_chars: int) -> CappedAnswer:
    """Cut a WRAPPED answer to `max_chars` characters of the peer's text. Pure.

    The wrapper is ours and is built only by `wrap_peer_answer`, so it is taken apart by its exact
    shape (and its inside has already had every delimiter neutralised): this is not a guess about
    whether a string "is already wrapped". The cut is re-wrapped, so the boundary is always whole.
    A string that does not have the wrapper's shape (a test double that skipped it) is cut as it is.
    The stored row is never touched: it keeps the peer's bytes verbatim.
    """
    head, tail = f"{UNTRUSTED_OPEN}\n", f"\n{UNTRUSTED_CLOSE}"
    shaped = (
        wrapped.startswith(head)
        and wrapped.endswith(tail)
        and len(wrapped) >= len(head + tail)
    )
    inner = wrapped[len(head) : len(wrapped) - len(tail)] if shaped else wrapped
    if len(inner) <= max_chars:
        return CappedAnswer(wrapped=wrapped, inner=inner, truncated=False)
    inner = inner[:max_chars]
    return CappedAnswer(
        wrapped=f"{head}{inner}{tail}" if shaped else inner, inner=inner, truncated=True
    )


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
        asker: AgentId | None = None,
        request_key: str | None = None,
        continue_session: str | None = None,
    ) -> str:
        """Enqueue durably and return the correlation id the answer will arrive with.

        Idempotent per request key (C13): a re-executed DBOS step carries the same key, derives
        the same correlation id and returns the handle it already minted instead of asking the
        peer a second time, while the same question under another key (a later round) is a fresh
        ask. Without a key the id is random and the ask is idempotent per (turn_id, target,
        question), as before. The conflicting
        insert leaves every column of the surviving row alone, `from_agent_id` included: a
        retry cannot rewrite who asked after the fact. See the module docstring for why
        `policy` is carried but not enforced here (docs/TASKS.md#t-f9-05 is the gate) and
        why nothing needs redacting at this layer.

        `asker` is the asking agent's own id, persisted so the answering side can re-run
        its half of the allowlist - WHO ASKED TRAVELS TOO, above. It is an argument BEYOND
        `ports.agent_mailbox.AgentMailbox.ask`, which is why it has a default: a caller
        typed on the port cannot pass it yet. Omitting it stores NULL, records that at
        WARNING, and leaves the answering side with no caller to check - which is the
        truth about that row, and the reason the line is loud.

        `request_key` (C2) derives the correlation id from `(turn_id, request_key, target,
        question)` with `domain.peers.peer_ask_id`; omitted, the id is random as before. A
        replayed step with the same key lands on the same primary key AND the same
        `(turn, target, question)` constraint, so the second INSERT is the no-op `DO UPDATE`
        and returns the first row's id: one row, one claim, one peer run.
        """
        if asker is None:
            _LOG.warning(
                "a peer ask is being enqueued for %r with no asking agent id, so the "
                "answering side cannot re-run callee_policy.may_ask(caller) and accepts "
                "the question on the asking side's word alone. docs/TASKS.md#t-f11-48",
                target,
            )
        return await asyncio.to_thread(
            self._enqueue_sync,
            target,
            question,
            from_session,
            turn_id,
            hop,
            asker,
            request_key,
            policy.max_concurrent_asks,
            continue_session,
        )

    def _enqueue_sync(
        self,
        target: AgentId,
        question: str,
        from_session: SessionRef,
        turn_id: TurnId,
        hop: int,
        asker: AgentId | None,
        request_key: str | None,
        max_concurrent: int,
        continue_session: str | None,
    ) -> str:
        correlation_id = (
            secrets.token_urlsafe(_CORRELATION_ID_BYTES)
            if request_key is None
            else peer_ask_id(str(turn_id), request_key, str(target), question)
        )
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            if continue_session is not None:
                self._check_continuation(conn, continue_session, target, from_session, asker)
            row = conn.execute(
                _ENQUEUE_UNKEYED_SQL if request_key is None else _ENQUEUE_KEYED_SQL,
                (
                    correlation_id,
                    target,
                    from_session.session_id,
                    from_session.tenant_id,
                    turn_id,
                    hop,
                    question,
                    asker,
                    # Minted per call but only kept by the first insert: a replayed ask hits
                    # the `DO UPDATE` branch, which leaves every column of the row alone.
                    new_answer_token(),
                    request_key,
                    max_concurrent,
                    continue_session
                    if continue_session is not None
                    else derived_peer_session(str(from_session.session_id), correlation_id),
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - DO UPDATE always returns the surviving row
            raise RuntimeError("peer ask was neither inserted nor found; the queue is unusable")
        return str(row[0])

    @staticmethod
    def _check_continuation(
        conn: psycopg.Connection[Any],
        continue_session: str,
        target: AgentId,
        from_session: SessionRef,
        asker: AgentId | None,
    ) -> None:
        """Refuse a continuation the asker has no claim to (C12, D45). Every reason reads the same.

        The session must be named for the asker's own case, and a row that CREATED it must have
        the same tenant, the same target and the same asking agent. An asker of unknown identity
        (`None`) owns nothing. The rows are the proof: a session id alone is a string anybody can
        write.
        """
        if (
            asker is None
            or not names_a_session_of_this_case(continue_session, str(from_session.session_id))
            or not any(
                _owns(from_session_id, from_agent, str(from_session.session_id), str(asker))
                for from_session_id, from_agent in conn.execute(
                    _SESSION_OWNERS_SQL, (continue_session, target, from_session.tenant_id)
                ).fetchall()
            )
        ):
            raise PeerContinuationRefusedError(_CONTINUATION_REFUSED)

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        """Take the oldest queued ask for `target`, or None when the queue is empty.

        Not part of `AgentMailbox`: the port describes the ASKING side, and this is the
        answering side of the same table. It stays on the adapter because the wire
        protocol decides it - D2's A2A adapter (docs/TASKS.md#t-d2-04) receives an HTTP
        task instead of polling a queue, and nothing above the port should have to change
        when it does.

        Claiming marks the row delivered. A worker that crashes between claiming and starting
        the workflow leaves it that way; `reclaim_stale` (C3) is the redelivery policy that puts
        such a row back after the lease, only when its workflow does not exist.
        """
        return await asyncio.to_thread(self._claim_sync, target)

    def _claim_sync(self, target: AgentId) -> PeerAsk | None:
        row: tuple[Any, ...] | None = None
        capped: list[str] = []  # turns the recount under their lock found at their cap
        with psycopg.connect(self._conninfo) as conn:
            while row is None:
                skipped_turn: str | None = None
                with conn.transaction():
                    candidate = conn.execute(_CLAIM_CANDIDATE_SQL, (target, capped)).fetchone()
                    if candidate is None:
                        return None
                    correlation_id, turn_id, max_concurrent = candidate
                    if max_concurrent is not None:
                        conn.execute(_TURN_LOCK_SQL, (str(turn_id),))
                        running = conn.execute(_RUNNING_SQL, (str(turn_id),)).fetchone()
                        if running is not None and int(running[0]) >= int(max_concurrent):
                            skipped_turn = str(turn_id)  # full after all: its asks wait, queued
                    if skipped_turn is None:
                        row = conn.execute(_CLAIM_ONE_SQL, (correlation_id,)).fetchone()
                if skipped_turn is not None:
                    capped.append(skipped_turn)
        if row is None:
            return None
        # `turn_id` is TEXT (migration 0029); the domain type is the string every other table
        # and every use case keys on.
        #
        # `from_agent_id` is read as None-or-a-value and NEVER coerced through `str()`:
        # `str(None)` is the string "None", an AgentId no profile has, and an allowlist
        # check against it would refuse for the wrong reason - reporting a stranger where
        # the truth is that nobody recorded a caller at all (t-f11-48).
        return PeerAsk(
            correlation_id=str(row[0]),
            target=AgentId(str(row[1])),
            from_session=SessionRef(
                session_id=SessionId(str(row[2])), tenant_id=TenantId(str(row[3]))
            ),
            turn_id=TurnId(str(row[4])),
            hop=int(row[5]),
            question=str(row[6]),
            asker=None if row[7] is None else AgentId(str(row[7])),
            answer_token=None if row[8] is None else str(row[8]),
            redeliveries=int(row[9]),
            failures=int(row[10]),
            peer_session_id=None if row[11] is None else str(row[11]),
        )

    async def reclaim_stale(
        self,
        target: AgentId,
        *,
        lease: timedelta,
        exists: Callable[[str, int], Awaitable[bool]],
        retire: Callable[[str, int], Awaitable[None]] | None = None,
        failed: Callable[[str, int], Awaitable[bool]] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Put delivered-but-unanswered asks of `target` back on the queue. C3, C1g / D31, D32.

        A claim marks the row `delivered` and nothing ran after it if the worker died between
        the claim and the workflow start. A row is RECLAIMABLE when it is delivered, unanswered
        and `delivered_at` is older than `lease`, AND `exists(correlation_id, generation)` says
        no workflow will answer it. The generation is the row's `redeliveries`: the workflow of
        the current attempt is `peer_workflow_id(<id>, n)`. A row whose workflow
        exists is never touched (it is running, waiting for its human, or finished: the workflow
        answers it). "Exists" is the starter's judgement and EXCLUDES a workflow that is still
        open under another application version: the new build never recovers it (D32).

        `retire(correlation_id, generation)`, when given, runs before the requeue for every row
        judged dead. It cancels what such a workflow left behind (the stranded run and the asks it
        made), because that run still holds the queue slot of its peer session and would block
        the new generation. A `retire` that raises leaves the row alone for the next pass.
        Redelivery is safe either way: the new generation has a new workflow id, and the old one
        is cancelled.

        `failed(correlation_id, generation)`, when given, is asked for a row judged dead, BEFORE
        `retire` clears the evidence, and says whether that generation's workflow ended in a callee
        FAILURE. Only then does the requeue add to the row's `callee_failures` (C1l): the
        redelivery cap counts failures, not the worker crashes and deploy retirements that also
        end a generation. Without it nothing is counted as a failure. A `failed` that raises
        leaves the row alone like the other callbacks.

        Both callbacks are asked per candidate, outside any database transaction. If `exists`
        raises the answer is UNKNOWN and the row is left alone (unknown is not "does not exist").
        The requeue is one guarded UPDATE, so concurrent reclaimers cannot both win the same row.
        Returns the reclaimed correlation ids, oldest first. `now` defaults to the database's
        clock.
        """
        candidates = await asyncio.to_thread(self._stale_sync, target, lease, now)
        reclaimed: list[str] = []
        for correlation_id, generation in candidates:
            try:
                if await exists(correlation_id, generation):
                    continue
                callee_failed = failed is not None and await failed(correlation_id, generation)
                if retire is not None:
                    await retire(correlation_id, generation)
            except Exception:
                _LOG.warning(
                    "could not settle the workflow for peer ask %s (generation %d); "
                    "not reclaiming",
                    correlation_id,
                    generation,
                    exc_info=True,
                )
                continue
            if await asyncio.to_thread(
                self._requeue_sync, correlation_id, generation, lease, now, callee_failed
            ):
                reclaimed.append(correlation_id)
        return tuple(reclaimed)

    def _stale_sync(
        self, target: AgentId, lease: timedelta, now: datetime | None
    ) -> list[tuple[str, int]]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(_STALE_SQL, (target, now, lease, _RECLAIM_BATCH)).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def _requeue_sync(
        self,
        correlation_id: str,
        generation: int,
        lease: timedelta,
        now: datetime | None,
        callee_failed: bool,
    ) -> bool:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            row = conn.execute(
                _REQUEUE_SQL, (int(callee_failed), correlation_id, generation, now, lease)
            ).fetchone()
        return row is not None

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        """Record the peer's answer against its correlation id. Idempotent.

        `failed` stores the outcome beside the text (C1l): the row says whether this is OUR fixed
        failure sentence, and `answer_failed` reads it back. It is part of the first answer, like
        the text: a retry cannot flip it.

        REQUIRES THE ROW'S `answer_token` (C1c). The correlation id is derived and so is an
        address, not a credential; a delivery without the token minted for this ask raises
        `PeerAnswerRefusedError` and writes nothing (the ask stays open for the real callee).
        An answered row stays a silent no-op for anyone, so a retry never errors.

        The first answer wins and later ones are no-ops rather than errors: a delivery is
        retried by the network and by DBOS alike, and a second attempt that OVERWROTE the
        answer would be a way to change what a suspended turn resumes with after the fact.

        An unknown correlation id is also a no-op. Handles expire and strangers deliver at
        whatever route fronts this; guessing which turn a stray answer belongs to resumes
        a conversation with content nobody asked for.

        Recording is all this does. Waking the workflow is `DBOS.send()`, in the workflow
        layer - see NO WAITING HAPPENS HERE at the top of this module.
        """
        await asyncio.to_thread(self._answer_sync, correlation_id, answer, answer_token, failed)

    def _answer_sync(
        self, correlation_id: str, answer: str, answer_token: str | None, failed: bool
    ) -> None:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            cursor = conn.execute(_ANSWER_SQL, (answer, failed, correlation_id, answer_token))
            if cursor.rowcount == 1:
                return
            row = conn.execute(_ANSWER_STATE_SQL, (correlation_id,)).fetchone()
        if row is not None and not row[0]:
            if row[1] == "cancelled":
                raise PeerAnswerRefusedError(correlation_id, "the ask was cancelled")
            raise PeerAnswerRefusedError(correlation_id, "the answer token did not match")

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        """Mark one open ask `cancelled` (C4). True if this call did it. See the port."""
        return await asyncio.to_thread(self._cancel_sync, correlation_id, reason)

    def _cancel_sync(self, correlation_id: str, reason: str) -> bool:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            return conn.execute(_CANCEL_SQL, (reason, correlation_id)).fetchone() is not None

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        """Cancel every open ask of the asking turn; the ids, sorted. See the port.

        `turn_id` is text (migration 0029): an ordinary turn's UUID and any other id are both
        valid (since D33 a callee's workflow id is a UUID too; the column stays TEXT, 0029 being
        shipped), so a callee's own asks can be cancelled too.
        """
        return await asyncio.to_thread(self._cancel_open_sync, turn_id, reason)

    def _cancel_open_sync(self, turn_id: str, reason: str) -> tuple[str, ...]:
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            rows = conn.execute(_CANCEL_OPEN_SQL, (reason, turn_id)).fetchall()
        return tuple(sorted(str(row[0]) for row in rows))

    async def is_cancelled(self, correlation_id: str) -> bool:
        return await asyncio.to_thread(self._is_cancelled_sync, correlation_id)

    def _is_cancelled_sync(self, correlation_id: str) -> bool:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_IS_CANCELLED_SQL, (correlation_id,)).fetchone()
        return row is not None and bool(row[0])

    async def answer_failed(self, correlation_id: str) -> bool:
        """Whether the stored answer was recorded as a callee failure (C1l). See the port."""
        return await asyncio.to_thread(self._answer_failed_sync, correlation_id)

    def _answer_failed_sync(self, correlation_id: str) -> bool:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_ANSWER_FAILED_SQL, (correlation_id,)).fetchone()
        return row is not None and bool(row[0])

    async def redeliveries(self, correlation_id: str) -> int:
        """The ask's generation (how often the lease requeued it); 0 if unknown. See the port."""
        return await asyncio.to_thread(self._redeliveries_sync, correlation_id)

    def _redeliveries_sync(self, correlation_id: str) -> int:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_REDELIVERIES_SQL, (correlation_id,)).fetchone()
        return 0 if row is None else int(row[0])

    async def read_answer(self, correlation_id: str) -> str | None:
        """The peer's answer, WRAPPED as untrusted content, or None if it has not answered.

        `ports.agent_mailbox.AgentMailbox.read_answer` - the other half of `ask`, and the
        port is where the wrapping is required rather than here (t-f9-09).

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
