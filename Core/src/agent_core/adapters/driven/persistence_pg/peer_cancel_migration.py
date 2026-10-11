"""Migration 0028: a `cancelled` end state for peer asks.

Phase:   Agent-comms v2, C4
Tests:   Core/tests/integration/test_peer_mailbox.py

`peer_messages.state` moves queued -> delivered -> answered (D14) and now also ends in
`cancelled`: the asker gave up (the ask timed out in a round, the asking turn expired or was
cancelled). `state` is plain TEXT with no CHECK constraint, so the new value needs no DDL; what
is added is the reason and the time, which an operator reads when asking "why did nobody
answer this?", and a partial index so the open-asks-of-a-turn scan (`cancel_open`) does not read
the whole history.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_CANCEL_MIGRATION = Migration(
    id="0028_peer_messages_cancel",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS cancel_reason TEXT;
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
    CREATE INDEX IF NOT EXISTS ix_peer_messages_open_by_turn
        ON peer_messages (turn_id)
        WHERE state IN ('queued', 'delivered');
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_CANCEL_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_CANCEL_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_CANCEL_MIGRATION.id,))


async def apply_peer_cancel_migration(app_conninfo: str) -> None:
    """Apply `PEER_CANCEL_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
