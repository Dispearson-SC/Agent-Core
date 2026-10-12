"""Migration 0034: an ask records the session its callee runs in (C12, D45).

Phase:   Agent-comms v2, C12
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_migrations.py

`peer_messages.peer_session_id` is the conversation the callee's turn is filed under. For a fresh
ask it is the name derived from the correlation id (`peer~<root>~uuid5(correlation)`); an ask that
CONTINUES an earlier one carries that earlier ask's name instead, so the callee sees the history of
the exchange. It is stored (and not only derived) because a continuation must be checked against the
rows that created the session: same tenant, same case, same asking agent, same target.

NULL means the row was written before this migration: its session is the derived one.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_SESSION_MIGRATION = Migration(
    id="0034_peer_messages_peer_session",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS peer_session_id TEXT;
    CREATE INDEX IF NOT EXISTS ix_peer_messages_peer_session
        ON peer_messages (peer_session_id)
        WHERE peer_session_id IS NOT NULL;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_SESSION_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_SESSION_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_SESSION_MIGRATION.id,))


async def apply_peer_session_migration(app_conninfo: str) -> None:
    """Apply `PEER_SESSION_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
