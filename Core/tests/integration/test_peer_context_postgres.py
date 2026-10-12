"""`peers.visibility: full` reads the asker's real conversation (C18, D47).

Covers: adapters/driven/peers/asker_context.py over `PgTranscriptReader` and the
`transcript_entries` view. The pure behaviour (cap, order, summary) is in
tests/unit/test_peer_context.py; here the reader is the real one, so what is proven is the
audience: the messages a person and the agent exchanged come through, an operator's tool call and
another tenant's conversation do not.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agent_core.adapters.driven.peers.asker_context import TranscriptAskerContext
from agent_core.adapters.driven.persistence_pg import migrations, transcript_repository
from agent_core.domain.peers import PeerVisibility
from agent_core.domain.turn import SessionId, SessionRef, TenantId

_ADMIN = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_DB = "agent_core_peer_context_test"


def _reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no reachable Postgres instance")


class _Connect:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def __call__(self) -> psycopg.Connection:  # type: ignore[type-arg]
        return psycopg.connect(self._conninfo)


def _message(kind: str, part_kind: str, text: str) -> str:
    """One serialised Pydantic AI message, as the conversation store writes it."""
    return json.dumps({"kind": kind, "parts": [{"part_kind": part_kind, "content": text}]})


def test_full_reads_the_messages_of_the_askers_own_session_and_nothing_else() -> None:
    asyncio.run(migrations.ensure_databases(_ADMIN, app_database=_DB, dbos_database=f"{_DB}_dbos"))
    conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_DB}\1", _ADMIN)
    asyncio.run(migrations.apply_all_migrations(conninfo))
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    turn = str(uuid.uuid4())
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM messages WHERE session_id IN ('ctx-1', 'ctx-other')")
        conn.execute("DELETE FROM turns WHERE session_id IN ('ctx-1', 'ctx-other')")
        for session, tenant, tid in (
            ("ctx-1", "t-ctx", turn),
            ("ctx-other", "t-ctx-other", str(uuid.uuid4())),
        ):
            conn.execute(
                "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
                "created_at, profile_version, profile_snapshot) "
                "VALUES (%s, %s, %s, 'p', 'started', %s, 1, '{}')",
                (tid, session, tenant, base),
            )
        rows = [
            ("ctx-1", 1, "user", _message("request", "user-prompt", "my order 42 never arrived")),
            ("ctx-1", 2, "assistant", _message("response", "text", "I am checking the delivery")),
            (
                "ctx-other",
                1,
                "user",
                _message("request", "user-prompt", "another customer's secret"),
            ),
        ]
        for n, (session, seq, role, content) in enumerate(rows):
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session, seq, role, content, base + timedelta(seconds=n + 1)),
            )
        conn.execute(
            "INSERT INTO audit_tool_calls (turn_id, tool, caller, arguments, effect, rule_id, at) "
            "VALUES (%s, 'lookup_delivery_code', 'u', '{}', 'allow', 'r', %s)",
            (turn, base + timedelta(seconds=5)),
        )

    source = TranscriptAskerContext(
        transcript_repository.PgTranscriptReader(_Connect(conninfo)), None
    )
    session = SessionRef(session_id=SessionId("ctx-1"), tenant_id=TenantId("t-ctx"))

    text = asyncio.run(source.render(session, PeerVisibility.FULL, 1000))

    assert text == "User: my order 42 never arrived\nAssistant: I am checking the delivery"
    other = asyncio.run(
        source.render(
            SessionRef(session_id=SessionId("ctx-1"), tenant_id=TenantId("t-someone-else")),
            PeerVisibility.FULL,
            1000,
        )
    )
    assert other == "", "the same session id under another tenant reads nothing"
