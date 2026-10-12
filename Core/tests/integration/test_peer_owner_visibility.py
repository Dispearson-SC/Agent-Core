"""A specialist's own approval shows up under the conversation the person is in (C16, D46).

Covers: adapters/driven/human/gateway.py (owner columns, the recipient),
adapters/driven/persistence_pg/peer_owner_migration.py (`transcript_entries`,
`transcript_conversations`), adapters/driven/persistence_pg/transcript_repository.py.

The root turn `R` (session `case-1`) asked a specialist, which started its own turn `C` (session
`peer~case-1~key`) and suspended on a person. The person's inbox is the transcript of `case-1`:
the specialist's request must be in it, with the chain that owns it; the user's projection must
still say only THAT something is pending (#11); another tenant's view of the same session id must
see nothing.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import timedelta

import psycopg
import pytest

from agent_core.adapters.driven.human.gateway import ChannelHumanGateway
from agent_core.adapters.driven.persistence_pg import migrations, transcript_repository
from agent_core.adapters.driving.channels.registry import ChannelRegistry, OutboundMessage
from agent_core.domain.transcript import Audience, EntryKind
from agent_core.domain.turn import (
    CallerIdentity,
    PeerOwner,
    PendingKind,
    PendingRequest,
    SessionId,
    SessionRef,
    TenantId,
    ToolCallId,
    TurnId,
)

_ADMIN = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DB = "agent_core_peer_owner_visibility_test"
_TENANT = TenantId("t-owner")


def _reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no reachable Postgres instance")


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[CallerIdentity, OutboundMessage]] = []

    async def send(self, caller: CallerIdentity, message: OutboundMessage) -> None:
        self.sent.append((caller, message))


class _Connect:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def __call__(self) -> psycopg.Connection:  # type: ignore[type-arg]
        return psycopg.connect(self._conninfo)


def _insert_turn(conn: psycopg.Connection, turn_id: str, session_id: str, tenant: str) -> None:  # type: ignore[type-arg]
    conn.execute(
        "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
        "created_at, profile_version, profile_snapshot) "
        "VALUES (%s, %s, %s, %s, 'started', now(), 1, '{}')",
        (turn_id, session_id, tenant, "p"),
    )


def test_a_specialists_approval_is_listed_under_the_root_conversation_with_its_chain() -> None:
    asyncio.run(migrations.ensure_databases(_ADMIN, app_database=_DB, dbos_database=f"{_DB}_dbos"))
    conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_DB}\1", _ADMIN)
    asyncio.run(migrations.apply_all_migrations(conninfo))

    root_turn, callee_turn = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM human_requests")
        conn.execute("DELETE FROM turns WHERE tenant_id IN (%s, 't-other')", (str(_TENANT),))
        _insert_turn(conn, root_turn, "case-1", str(_TENANT))
        _insert_turn(conn, callee_turn, "peer~case-1~key", str(_TENANT))

    channel = _Channel()
    gateway = ChannelHumanGateway(
        conninfo,
        ChannelRegistry([("whatsapp", channel)]),
        channel_id="whatsapp",
        ttl=timedelta(days=1),
    )
    approval = PendingRequest(
        kind=PendingKind.APPROVAL,
        tool_call_id=ToolCallId("tc-refund"),
        tool_name="issue_refund",
        arguments={"amount": 10},
        reason="a person must agree",
    )
    owner = PeerOwner(
        root_turn_id=root_turn, asker_turn_id=root_turn, asker="support_triage", call="corr-9"
    )
    asyncio.run(
        gateway.publish(
            TurnId(callee_turn),
            SessionRef(session_id=SessionId("peer~case-1~key"), tenant_id=_TENANT),
            (approval,),
            owner=owner,
        )
    )

    reader = transcript_repository.PgTranscriptReader(_Connect(conninfo))
    root = SessionRef(session_id=SessionId("case-1"), tenant_id=_TENANT)
    other = SessionRef(session_id=SessionId("case-1"), tenant_id=TenantId("t-other"))
    admin = asyncio.run(reader.page(root, Audience.ADMIN))
    user = asyncio.run(reader.page(root, Audience.USER))
    elsewhere = asyncio.run(reader.page(other, Audience.ADMIN))
    inbox, _ = asyncio.run(reader.list_conversations(_TENANT, Audience.ADMIN, suspended_only=True))
    own_session = asyncio.run(
        reader.page(
            SessionRef(session_id=SessionId("peer~case-1~key"), tenant_id=_TENANT), Audience.ADMIN
        )
    )

    (message_to_the_person,) = [m for _, m in channel.sent]
    assert channel.sent[0][0].subject_id == "case-1", "the person in the case is the one told"
    assert "issue_refund" not in message_to_the_person.text

    pending = [e for e in admin.entries if e.kind is EntryKind.PENDING_REQUEST]
    assert len(pending) == 1, "the specialist's request is listed under the root conversation"
    assert pending[0].payload["owner"] == {
        "root_turn_id": root_turn,
        "asker_turn_id": root_turn,
        "asker": "support_triage",
        "call": "corr-9",
    }
    assert [e.kind for e in user.entries if e.kind is not EntryKind.USER_MESSAGE] == [
        EntryKind.PENDING_PLACEHOLDER
    ], "a user is told THAT something is pending and nothing about whose or which"
    assert set(user.entries[-1].payload) == {"since"}
    assert elsewhere.entries == (), "another tenant asking for the same session id sees nothing"
    assert [str(row.session.session_id) for row in inbox] == ["case-1"], (
        "the root conversation is suspended"
    )
    assert not [e for e in own_session.entries if e.kind is EntryKind.PENDING_REQUEST], (
        "and the request is no longer buried in the specialist's own session"
    )
