"""Driven adapter: HumanGateway - publish asks, correlate replies.

Phase:   F3 (approvals) / F7 (evidence, unchanged)
Tasks:   docs/TASKS.md#t-f3-04
Status:  DONE - correlation table 0010, unguessable handles, idempotent publish
Tests:   Core/tests/integration/test_human_gateway.py
Implements: ports/human_gateway.py

NO WAITING HAPPENS HERE
    This adapter publishes and correlates. The durable wait is DBOS.recv() in the workflow.
    A sleep, a poll loop or a thread join in this file is a bug: it will silently lose the
    turn on the next deploy.

CORRELATION TABLE
    human_requests (correlation_id pk, turn_id, tool_call_id, kind, expires_at, answered_at)

    Created by migration `0010`, which lives in
    adapters/driven/persistence_pg/human_requests_migration.py so that `migrations.py`
    stays a file no anchor shares.

    `correlation_id` is handed to a human over a channel. Make it unguessable - anyone who
    can guess one can approve an action. Treat it like a bearer token, because it is one.

IDEMPOTENCY
    publish() must be idempotent per (turn_id, tool_call_id). A DBOS step retries after a
    crash, and re-publishing asks a real person the same question twice - which is how you
    end up with two conflicting approvals for one action.

    It is enforced by the table's UNIQUE constraint and an `ON CONFLICT DO NOTHING`, not by
    reading first and then writing: two step attempts can both read "not yet published"
    before either writes. The insert also decides whether the channel send happens at all,
    so the second attempt is silent on the wire as well as in the table - a duplicate row
    and a duplicate WhatsApp message are the same defect seen from two sides.

REDACTION
    Never put raw tool arguments in a channel message without redacting. Arguments carry
    credentials and personal data, and the channel is almost always less trusted than the
    database.

    So the rendered ask carries the tool name, the reason and the argument NAMES, and no
    argument value ever leaves this process. The names are what make the ask
    understandable - "issue_refund(amount_usd, api_key)" tells a human what is about to
    happen - and the values are what make it dangerous.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta

import psycopg

from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.domain.turn import (
    CallerIdentity,
    PendingKind,
    PendingRequest,
    SessionRef,
    ToolCallId,
    TurnId,
)

# 32 bytes, so the handle carries 256 bits - comfortably past the 128 the test measures.
# The margin is deliberate: this is a bearer token with a seven-day life and no rate limit
# in front of it on day 1, and the only cost of the extra bytes is a longer URL.
_CORRELATION_ID_BYTES = 32

# How long a handle stays answerable. Long enough that a human can answer tomorrow - F3's
# whole criterion is an approval surviving a redeploy and a night - and short enough that
# a leaked handle stops working. Configurable per deployment, never per request.
_DEFAULT_TTL = timedelta(days=7)

_INSERT_REQUEST_SQL = """
    INSERT INTO human_requests
        (correlation_id, turn_id, tool_call_id, kind, expires_at)
    VALUES (%s, %s, %s, %s, now() + %s)
    ON CONFLICT ON CONSTRAINT uq_human_requests_turn_tool DO NOTHING
    RETURNING correlation_id
"""

# `expires_at` is checked in SQL rather than in Python so the clock that decides is the
# database's, the same one that wrote the row. Two processes with drifting clocks must not
# disagree about whether a handle is still good.
_CORRELATE_SQL = """
    SELECT turn_id, tool_call_id
    FROM human_requests
    WHERE correlation_id = %s AND expires_at > now()
"""


def new_correlation_id() -> str:
    """A fresh unguessable handle for one pending request.

    `secrets`, never `random` and never `uuid4` formatting: this value is the only thing
    standing between a stranger and approving somebody else's tool call. It is
    URL-safe because it travels in `POST /decisions/{corr_id}` (t-f3-11) and in a chat
    message, and both mangle anything else.
    """
    return secrets.token_urlsafe(_CORRELATION_ID_BYTES)


class ChannelHumanGateway:
    """Postgres + channel adapter for `ports.human_gateway.HumanGateway`.

    D13: both port members are async; the psycopg calls underneath are synchronous
    (psycopg has no coroutine transaction support) and are wrapped in `asyncio.to_thread`,
    exactly as `PgConversationStore` does.

    The channel is configuration, not a per-vertical concern: the registry is D23's shared
    lookup table (`adapters/driving/channels/registry.py`), the same one the workflow's
    delivery step uses, and `channel_id` names the entry this deployment asks humans on.
    """

    __slots__ = ("_channel_id", "_conninfo", "_registry", "_ttl")

    def __init__(
        self,
        conninfo: str,
        registry: ChannelRegistry,
        *,
        channel_id: str,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        self._conninfo = conninfo
        self._registry = registry
        self._channel_id = channel_id
        self._ttl = ttl

    async def publish(
        self, turn_id: TurnId, session: SessionRef, requests: tuple[PendingRequest, ...]
    ) -> None:
        """Record a correlation row per request, then ask the human on the channel.

        The tuple is walked in order and one request at a time. Not `asyncio.gather`: this
        runs inside a DBOS step, and concurrent sends would put the asks on the wire in
        whatever order the network resolved them - a different order on replay, which is
        the non-determinism rule in CLAUDE.md.

        The write comes before the send, deliberately. A row with no message asks nobody
        and is recoverable; a message with no row is a handle a human holds that the
        system cannot resolve, and it looks to them like the system lost their answer.
        """
        for request in requests:
            correlation_id = await asyncio.to_thread(self._record_sync, turn_id, request)
            if correlation_id is None:
                # Already published for this (turn_id, tool_call_id) - a retried step.
                # Staying silent here is the whole idempotency guarantee: the human keeps
                # the handle they already have and is not asked a second time.
                continue
            await self._registry.get(self._channel_id).send(
                self._recipient(session), self._render(request, correlation_id)
            )

    def _record_sync(self, turn_id: TurnId, request: PendingRequest) -> str | None:
        """The new handle, or None when this request was already published.

        Autocommit and a single statement: the uniqueness decision belongs to the database
        and there is nothing else in this transaction to keep it company.
        """
        with psycopg.connect(self._conninfo, autocommit=True) as conn:
            row = conn.execute(
                _INSERT_REQUEST_SQL,
                (
                    new_correlation_id(),
                    turn_id,
                    request.tool_call_id,
                    request.kind.value,
                    self._ttl,
                ),
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _recipient(self, session: SessionRef) -> CallerIdentity:
        """Who to put the ask in front of, expressed as the registry's addressing type.

        `Channel.send` takes a `CallerIdentity` and `HumanGateway.publish` is handed a
        `SessionRef`, so the two halves of D23's shared registry do not address a human
        the same way. What a session unambiguously provides is the conversation and the
        tenant; the configured `channel_id` supplies the third field, because this gateway
        publishes on exactly one channel by configuration.

        `roles` is left empty on purpose. This identity exists to route an outbound
        message and is never an authorisation subject - the approver's identity arrives
        with the reply and is `DecideApproval`'s input (t-f3-03, t-f3-05), not this one.
        """
        return CallerIdentity(
            subject_id=session.session_id,
            channel=self._channel_id,
            tenant_id=session.tenant_id,
        )

    def _render(self, request: PendingRequest, correlation_id: str) -> OutboundMessage:
        """The ask as a human reads it. Argument NAMES only - see REDACTION above."""
        arguments = ", ".join(sorted(request.arguments))
        if request.kind is PendingKind.APPROVAL:
            ask = f"Approve {request.tool_name}({arguments})?"
        else:
            ask = f"{request.tool_name}({arguments}) needs something from you."
        return OutboundMessage(text=f"{ask} {request.reason} [ref {correlation_id}]")

    async def correlate(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        """Resolve an inbound reply back to (turn_id, tool_call_id), or None.

        None is the NORMAL answer for an unknown or expired handle, not an error: handles
        expire and strangers POST at the decision route. The HTTP adapter turns it into a
        404 and never guesses - routing a stray reply into the wrong turn approves an
        action nobody approved.
        """
        return await asyncio.to_thread(self._correlate_sync, correlation_id)

    def _correlate_sync(self, correlation_id: str) -> tuple[TurnId, ToolCallId] | None:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_CORRELATE_SQL, (correlation_id,)).fetchone()
        if row is None:
            return None
        # `turn_id` comes back as a uuid.UUID from a UUID column; the domain type is the
        # string form every other table and every use case keys on.
        return TurnId(str(row[0])), ToolCallId(str(row[1]))
