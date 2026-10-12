"""An in-memory `AgentMailbox` with the answering side's queue verbs: the reference behaviour.

It implements the same contract as `PgAgentMailbox` (and passes the same conformance suite,
`tests/contract/mailbox_conformance.py`): idempotent enqueue per `(turn, target, question)`,
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
    PeerAnswerRefusedError,
    PeerPolicy,
    new_answer_token,
    peer_ask_id,
)
from agent_core.domain.turn import SessionRef, TurnId


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
    cancel_reason: str | None = None


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
    ) -> str:
        key = (str(turn_id), str(target), question)
        if key in self._by_key:  # the unique constraint: a replay names the first row
            return self._by_key[key]
        correlation_id = (
            secrets.token_urlsafe(32)
            if request_key is None
            else peer_ask_id(str(turn_id), request_key, str(target), question)
        )
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
        )
        self._by_key[key] = correlation_id
        return correlation_id

    async def answer(
        self, correlation_id: str, answer: str, *, answer_token: str | None = None
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
        row.state = "answered"

    async def cancel(self, correlation_id: str, reason: str) -> bool:
        row = self.rows.get(correlation_id)
        if row is None or row.state not in ("queued", "delivered"):
            return False
        row.state = "cancelled"
        row.cancel_reason = reason
        return True

    async def cancel_open(self, turn_id: str, reason: str) -> tuple[str, ...]:
        ended = sorted(
            row.correlation_id
            for row in self.rows.values()
            if row.turn_id == turn_id and row.state in ("queued", "delivered")
        )
        for correlation_id in ended:
            await self.cancel(correlation_id, reason)
        return tuple(ended)

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
            (r for r in self.rows.values() if r.target == target and r.state == "queued"),
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
        )

    async def reclaim_stale(
        self,
        target: AgentId,
        *,
        lease: timedelta,
        exists: Callable[[str, int], Awaitable[bool]],
        retire: Callable[[str, int], Awaitable[None]] | None = None,
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
                if retire is not None:
                    await retire(row.correlation_id, generation)
            except Exception:  # noqa: BLE001 - unknown is not "does not exist"
                continue
            if row.state == "delivered" and row.redeliveries == generation:
                row.state = "queued"
                row.delivered_at = None
                row.redeliveries += 1
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
