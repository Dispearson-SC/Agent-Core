"""An in-memory `AgentMailbox` with the answering side's queue verbs: the reference behaviour.

It implements the same contract as `PgAgentMailbox` (and passes the same conformance suite,
`tests/contract/mailbox_conformance.py`): idempotent enqueue per request key (per
`(turn, target, question)` when unkeyed),
oldest-first exclusive claims, the per-ask answer token, first-answer-wins, unknown ids as silent
no-ops, the `cancelled` end state and the redelivery lease with its generation. A test that needs
a mailbox and not a database uses this one; wrap it in `ControlledMailbox` to inject faults.

Time is injectable (`clock`) so a lease can be judged without sleeping.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent_core.adapters.driven.peers.mailbox import PeerAsk, wrap_peer_answer
from agent_core.domain.peers import (
    AgentId,
    AgentRef,
    BackgroundAsk,
    BackgroundState,
    PeerAnswerRefusedError,
    PeerContinuationRefusedError,
    PeerPolicy,
    derived_peer_session,
    names_a_session_of_this_case,
    new_answer_token,
    peer_ask_id,
    peer_root_origin,
)
from agent_core.domain.turn import CallerIdentity, SessionRef, TurnId


@dataclass
class _Row:
    correlation_id: str
    target: AgentId
    question: str
    from_session: SessionRef
    turn_id: str
    hop: int
    asker: AgentId | None
    answer_token: str
    seq: int
    state: str = "queued"  # queued | delivered | answered | cancelled
    answer: str | None = None
    delivered_at: datetime | None = None
    redeliveries: int = 0
    failures: int = 0
    max_concurrent: int | None = None
    answer_failed: bool = False
    cancel_reason: str | None = None
    peer_session_id: str | None = None
    root_turn_id: str | None = None
    background_caller: CallerIdentity | None = None
    enqueued_at: datetime | None = None
    finished_at: datetime | None = None


class InMemoryAgentMailbox:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self.rows: dict[str, _Row] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._seq = 0

    # --- the port ----------------------------------------------------------------------------

    async def discover(self, policy: PeerPolicy) -> tuple[AgentRef, ...]:
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
        root_turn_id: str | None = None,
        background_caller: CallerIdentity | None = None,
    ) -> str:
        if continue_session is not None:
            self._check_continuation(continue_session, target, from_session, asker)
        key = (str(turn_id), str(target), question)
        if request_key is None:
            if key in self._by_key:  # an unkeyed ask is idempotent per (turn, target, question)
                return self._by_key[key]
            correlation_id = secrets.token_urlsafe(32)
        else:
            correlation_id = peer_ask_id(str(turn_id), request_key, str(target), question)
            if correlation_id in self.rows:  # a keyed ask is idempotent per request key (C13)
                return correlation_id
        self._seq += 1
        self.rows[correlation_id] = _Row(
            correlation_id=correlation_id,
            target=target,
            question=question,
            from_session=from_session,
            turn_id=str(turn_id),
            hop=hop,
            asker=asker,
            answer_token=new_answer_token(),
            seq=self._seq,
            max_concurrent=policy.max_concurrent_asks,
            root_turn_id=root_turn_id if root_turn_id is not None else str(turn_id),
            background_caller=background_caller,
            enqueued_at=self._clock(),
            peer_session_id=(
                continue_session
                if continue_session is not None
                else derived_peer_session(str(from_session.session_id), correlation_id)
            ),
        )
        if request_key is None:
            self._by_key[key] = correlation_id
        return correlation_id

    def _check_continuation(
        self, session: str, target: AgentId, from_session: SessionRef, asker: AgentId | None
    ) -> None:
        """The same rule as the Postgres adapter: this asker's own session, with this target."""
        owned = asker is not None and names_a_session_of_this_case(
            session, str(from_session.session_id)
        )
        owned = owned and any(
            row.peer_session_id == session
            and row.target == target
            and row.from_session.tenant_id == from_session.tenant_id
            and row.asker == asker
            and peer_root_origin(str(row.from_session.session_id))
            == peer_root_origin(str(from_session.session_id))
            for row in self.rows.values()
        )
        if not owned:
            raise PeerContinuationRefusedError("that session cannot be continued")

    async def answer(
        self,
        correlation_id: str,
        answer: str,
        *,
        answer_token: str | None = None,
        failed: bool = False,
    ) -> None:
        row = self.rows.get(correlation_id)
        if row is None or row.state == "answered":
            return  # a stranger, or a retry: silent
        if row.state == "cancelled":
            raise PeerAnswerRefusedError(correlation_id, "the ask was cancelled")
        if answer_token is None or not secrets.compare_digest(
            row.answer_token.encode(), answer_token.encode()
        ):
            raise PeerAnswerRefusedError(correlation_id, "the answer token did not match")
        row.answer = answer
        row.answer_failed = failed
        row.state = "answered"
        row.finished_at = self._clock()

    async def answer_failed(self, correlation_id: str) -> bool:
        row = self.rows.get(correlation_id)
        return row is not None and row.state == "answered" and row.answer_failed

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        row = self.rows.get(correlation_id)
        if row is None or row.state not in ("queued", "delivered"):
            return False
        row.state = "cancelled"
        row.cancel_reason = reason
        row.finished_at = self._clock()
        return True

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        ended = sorted(
            row.correlation_id
            for row in self.rows.values()
            if row.turn_id == turn_id
            and row.state in ("queued", "delivered")
            and row.background_caller is None  # a background ask outlives its asking turn (C19)
        )
        for correlation_id in ended:
            await self.cancel(correlation_id, reason)
        return tuple(ended)

    async def cancel_background(self, session: SessionRef, reason: str) -> tuple[str, ...]:
        ended = sorted(
            row.correlation_id
            for row in self.rows.values()
            if row.background_caller is not None
            and row.from_session == session
            and row.state in ("queued", "delivered")
        )
        for correlation_id in ended:
            await self.cancel(correlation_id, reason)
        return tuple(ended)

    async def background_asks(self, session: SessionRef) -> tuple[BackgroundAsk, ...]:
        mine = sorted(
            (
                row
                for row in self.rows.values()
                if row.background_caller is not None and row.from_session == session
            ),
            key=lambda row: (-row.seq, row.correlation_id),
        )[:50]
        return tuple(
            BackgroundAsk(
                handle=row.correlation_id,
                target=str(row.target),
                question=row.question,
                asked_at=row.enqueued_at or self._clock(),
                state=_state_of(row),
                finished_at=row.finished_at,
                session=row.peer_session_id,
            )
            for row in mine
        )

    async def is_cancelled(self, correlation_id: str) -> bool:
        row = self.rows.get(correlation_id)
        return row is not None and row.state == "cancelled"

    async def redeliveries(self, correlation_id: str) -> int:
        row = self.rows.get(correlation_id)
        return 0 if row is None else row.redeliveries

    async def read_answer(self, correlation_id: str) -> str | None:
        row = self.rows.get(correlation_id)
        if row is None or row.answer is None:
            return None
        return wrap_peer_answer(row.answer)

    # --- the answering side's queue ------------------------------------------------------------

    async def claim_next(self, target: AgentId) -> PeerAsk | None:
        queued = sorted(
            (
                r
                for r in self.rows.values()
                if r.target == target and r.state == "queued" and not self._at_its_cap(r)
            ),
            key=lambda r: (r.seq, r.correlation_id),
        )
        if not queued:
            return None
        row = queued[0]
        row.state = "delivered"
        row.delivered_at = self._clock()
        return PeerAsk(
            correlation_id=row.correlation_id,
            target=row.target,
            question=row.question,
            from_session=row.from_session,
            turn_id=TurnId(row.turn_id),
            hop=row.hop,
            asker=row.asker,
            answer_token=row.answer_token,
            redeliveries=row.redeliveries,
            failures=row.failures,
            peer_session_id=row.peer_session_id,
            root_turn_id=row.root_turn_id,
            background_caller=row.background_caller,
        )

    def _at_its_cap(self, row: _Row) -> bool:
        """C11. The asking turn already has `max_concurrent` asks running (delivered, unanswered),
        across every target: this ask waits, queued. No cap (None) is never held."""
        if row.max_concurrent is None:
            return False
        running = sum(
            1 for r in self.rows.values() if r.turn_id == row.turn_id and r.state == "delivered"
        )
        return running >= row.max_concurrent

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
        moment = self._clock() if now is None else now
        stale = sorted(
            (
                r
                for r in self.rows.values()
                if r.target == target
                and r.state == "delivered"
                and r.delivered_at is not None
                and r.delivered_at < moment - lease
            ),
            key=lambda r: (r.delivered_at, r.correlation_id),
        )
        reclaimed: list[str] = []
        for row in stale:
            generation = row.redeliveries
            try:
                if await exists(row.correlation_id, generation):
                    continue
                callee_failed = failed is not None and await failed(row.correlation_id, generation)
                if retire is not None:
                    await retire(row.correlation_id, generation)
            except Exception:  # noqa: BLE001 - unknown is not "does not exist"
                continue
            if row.state == "delivered" and row.redeliveries == generation:
                row.state = "queued"
                row.delivered_at = None
                row.redeliveries += 1
                row.failures += int(callee_failed)
                reclaimed.append(row.correlation_id)
        return tuple(reclaimed)

    # --- test helpers --------------------------------------------------------------------------

    def token_of(self, correlation_id: str) -> str | None:
        row = self.rows.get(correlation_id)
        return None if row is None else row.answer_token

    def age(self, correlation_id: str, by: timedelta) -> None:
        row = self.rows[correlation_id]
        if row.delivered_at is not None:
            row.delivered_at -= by


def _state_of(row: _Row) -> BackgroundState:
    if row.state == "cancelled":
        return BackgroundState.CANCELLED
    if row.state == "answered":
        return BackgroundState.FAILED if row.answer_failed else BackgroundState.ANSWERED
    return BackgroundState.RUNNING if row.state == "delivered" else BackgroundState.QUEUED
