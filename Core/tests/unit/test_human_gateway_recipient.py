"""C1n: the human gateway resolves WHO to ask before it records that it asked.

Subject: adapters/driven/human/gateway.py (`ChannelHumanGateway.publish`, `_recipient`).

The correlation row is the idempotency key of a retried publish step: a row that exists means "the
person was asked". If the recipient is resolved after the row is written and resolving raises, the
retry finds the row, stays silent, and the person is never asked.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from agent_core.adapters.driven.human.gateway import ChannelHumanGateway
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _Gateway(ChannelHumanGateway):
    """The Postgres write is replaced by its one observable rule: ON CONFLICT DO NOTHING per
    (turn, tool call). `fail_recipient` makes the first recipient lookup raise."""

    def __init__(self, channel: _Channel, *, fail_recipient: int = 0) -> None:
        super().__init__(
            "unused", ChannelRegistry([("wa", channel)]), channel_id="wa", ttl=timedelta(hours=1)
        )
        self.rows: set[tuple[str, str]] = set()
        self.fail_recipient = fail_recipient

    def _record_sync(self, turn_id, request, owner):  # type: ignore[no-untyped-def,override]
        key = (str(turn_id), str(request.tool_call_id))
        if key in self.rows:
            return None
        self.rows.add(key)
        return "corr-1"

    def _recipient(self, session: SessionRef) -> CallerIdentity:
        if self.fail_recipient > 0:
            self.fail_recipient -= 1
            raise RuntimeError("recipient lookup failed")
        return super()._recipient(session)


_REQUEST = PendingRequest(
    tool_call_id=ToolCallId("c1"),
    tool_name="refund",
    arguments={},
    kind=PendingKind.APPROVAL,
    reason="refund 10",
)


def _session(session_id: str) -> SessionRef:
    return SessionRef(session_id=SessionId(session_id), tenant_id=TenantId("t"))


def test_a_failed_recipient_lookup_leaves_no_row_so_the_retry_still_asks() -> None:
    channel = _Channel()
    gateway = _Gateway(channel, fail_recipient=1)

    try:
        asyncio.run(gateway.publish(TurnId("turn"), _session("case-1"), (_REQUEST,)))
    except RuntimeError:
        pass
    assert gateway.rows == set(), "no correlation row may exist for a person nobody could address"

    asyncio.run(gateway.publish(TurnId("turn"), _session("case-1"), (_REQUEST,)))
    assert len(channel.sent) == 1, "the retried step must ask the person exactly once"


def test_a_session_id_with_a_tilde_that_is_not_a_peer_session_is_still_addressed() -> None:
    channel = _Channel()
    gateway = _Gateway(channel)

    asyncio.run(gateway.publish(TurnId("turn"), _session("odd~id"), (_REQUEST,)))
    asyncio.run(gateway.publish(TurnId("turn"), _session("odd~id"), (_REQUEST,)))

    assert [caller.subject_id for caller, _ in channel.sent] == ["odd~id"]
