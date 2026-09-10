"""Integration tests for the Postgres-backed `HumanGateway`.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-04
Covers:  adapters/driven/human/gateway.py
         adapters/driven/persistence_pg/human_requests_migration.py

WHAT IS BEING DEFENDED

    1. IDEMPOTENCE PER (turn_id, tool_call_id). `publish` runs inside a DBOS step, and a
       step is re-executed after a crash. A second publish that writes a second
       correlation row also asks a real person the same question a second time - and two
       answers to one question are two conflicting approvals for one action, with no
       exception anywhere to say which one counts. One row, one channel message, however
       many times the step runs.

    2. AN UNGUESSABLE CORRELATION ID. The id is handed to a human over WhatsApp or
       Telegram and it is the ONLY thing `POST /decisions/{corr_id}` (t-f3-11) has to
       decide whether the reply is genuine. It is a bearer token. Anyone who can guess one
       approves somebody else's tool call, and nothing in the system would look wrong
       afterwards - the audit row would name a real turn and a real tool.

       So the test measures randomness rather than trusting the name of a function: it
       decodes the id back to bytes and asserts that each of the first 128 bit positions
       actually takes both values across a sample. A counter, a timestamp, a session id or
       a truncated hash all fail that; only real entropy passes it.

    3. NO RAW ARGUMENTS ON THE WIRE. Tool arguments carry credentials and personal data
       and the channel is less trusted than the database, so what leaves the process
       carries the ask and never the arguments.

The randomness test needs no infrastructure and always runs. The idempotence test needs a
real Postgres and skips cleanly without one, the same pattern as
test_conversation_repository.py and test_profile_snapshot.py.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import uuid

import psycopg
import pytest

from agent_core.adapters.driven.human.gateway import ChannelHumanGateway, new_correlation_id
from agent_core.adapters.driven.persistence_pg import human_requests_migration, migrations
from agent_core.adapters.driving.channels.registry import (
    ChannelRegistry,
    OutboundMessage,
)
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

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_CHANNEL_ID = "whatsapp"

# A value that must never reach the channel. Written into the tool arguments so the
# redaction assertion is about real data flow rather than about a formatting convention.
_SECRET_ARGUMENT = "sk-live-51H9nOtRealBearerToken"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


class _RecordingChannel:
    """A `Channel` that keeps what it was asked to send instead of calling a platform."""

    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


def _approval_request(tool_call_id: str) -> PendingRequest:
    return PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId(tool_call_id),
        tool_name="issue_refund",
        arguments={"amount_usd": "250.00", "api_key": _SECRET_ARGUMENT},
        reason="a refund over 200 USD needs a human",
    )


def _decode_correlation_id(correlation_id: str) -> bytes:
    """The id back as the bytes it was built from, so entropy can be measured on it."""
    padding = "=" * (-len(correlation_id) % 4)
    return base64.urlsafe_b64decode(correlation_id + padding)


def test_a_correlation_id_carries_at_least_128_bits_of_randomness() -> None:
    """A guessable handle lets a stranger approve someone else's tool call.

    Two independent things are asserted, because either one alone is passable by an id
    nobody should ship. Uniqueness alone is satisfied by a counter; length alone is
    satisfied by a padded session id. Only bit-level variation across a sample rules both
    out.
    """
    samples = [new_correlation_id() for _ in range(256)]

    assert len(set(samples)) == len(samples), (
        "correlation ids repeated inside a 256-id sample; the id is a bearer token and a "
        "collision hands one human's approval authority to another"
    )

    decoded = [_decode_correlation_id(sample) for sample in samples]
    assert all(len(raw) >= 16 for raw in decoded), (
        "a correlation id decodes to fewer than 16 bytes, so it cannot carry 128 bits "
        f"however it was generated; shortest was {min(len(raw) for raw in decoded)} bytes"
    )

    # Every one of the first 128 bit positions must take both values somewhere in the
    # sample. A counter varies only its lowest bits, a timestamp only its middle ones, and
    # a constant prefix - the shape a hand-rolled "unique enough" id usually has - varies
    # none of them. With real entropy the chance of a stuck bit here is 128 * 2**-255.
    for bit in range(128):
        observed = {(raw[bit // 8] >> (bit % 8)) & 1 for raw in decoded}
        assert observed == {0, 1}, (
            f"bit {bit} of the correlation id never varies across 256 samples: it is "
            "fixed, derived or counted, not random. The id is the only thing standing "
            "between a stranger and approving somebody else's tool call"
        )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_publishing_the_same_request_twice_creates_exactly_one_row() -> None:
    """A retried DBOS step must not ask a real person the same question twice."""
    app_db = "agent_core_human_gateway_test"
    dbos_db = "agent_core_human_gateway_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(human_requests_migration.apply_human_requests_migration(app_conninfo))

    channel = _RecordingChannel()
    gateway = ChannelHumanGateway(
        app_conninfo,
        ChannelRegistry([(_CHANNEL_ID, channel)]),
        channel_id=_CHANNEL_ID,
    )

    session = SessionRef(session_id=SessionId(f"s-{uuid.uuid4()}"), tenant_id=TenantId("t-1"))
    turn_id = TurnId(str(uuid.uuid4()))
    requests = (_approval_request("call-1"),)

    asyncio.run(gateway.publish(turn_id, session, requests))
    # The step crashed after publishing and DBOS re-executed it. Same turn, same tool call.
    asyncio.run(gateway.publish(turn_id, session, requests))

    with psycopg.connect(app_conninfo) as conn:
        rows = conn.execute(
            "SELECT correlation_id FROM human_requests WHERE turn_id = %s AND tool_call_id = %s",
            (turn_id, "call-1"),
        ).fetchall()

    assert len(rows) == 1, (
        f"{len(rows)} correlation rows for one (turn_id, tool_call_id); a retried step "
        "produced a second handle, so one action now has two ways to be approved"
    )
    assert len(channel.sent) == 1, (
        f"the human was asked {len(channel.sent)} times for one approval; two answers to "
        "one question are two conflicting approvals with nothing to say which counts"
    )

    correlation_id = rows[0][0]
    resolved = asyncio.run(gateway.correlate(correlation_id))
    assert resolved == (turn_id, ToolCallId("call-1")), (
        "the stored handle did not resolve back to the turn and tool call it was minted "
        f"for; got {resolved!r}"
    )
    assert asyncio.run(gateway.correlate(new_correlation_id())) is None, (
        "an unknown handle must resolve to None, never raise and never guess - strangers "
        "POST at the decision route"
    )

    _, message = channel.sent[0]
    assert _SECRET_ARGUMENT not in message.text, (
        "a raw tool argument reached the channel; arguments carry credentials and the "
        "channel is less trusted than the database"
    )
