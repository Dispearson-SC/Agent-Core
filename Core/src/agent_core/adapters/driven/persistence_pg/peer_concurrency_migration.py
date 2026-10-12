"""Migration 0033: an ask carries the concurrency cap of the turn that made it (C11, D40).

Phase:   Agent-comms v2, C11
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_migrations.py

`peer_messages.max_concurrent` is the asking profile's `peers.max_concurrent_asks` at the moment
the ask was enqueued. The answering side's claim leaves a queued ask alone while its asking turn
already has that many asks running (`delivered`, unanswered). It is stored on the row, not looked
up, because the claiming worker does not hold the asker's profile and the row is the only thing
both sides share.

NULL means no cap: every row written before this migration (an older build keeps working on a
migrated table, and its asks run as they always did).
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_CONCURRENCY_MIGRATION = Migration(
    id="0033_peer_messages_max_concurrent",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS max_concurrent INTEGER;
    CREATE INDEX IF NOT EXISTS ix_peer_messages_running_by_turn
        ON peer_messages (turn_id)
        WHERE state = 'delivered' AND answered_at IS NULL;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_CONCURRENCY_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_CONCURRENCY_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_CONCURRENCY_MIGRATION.id,)
        )


async def apply_peer_concurrency_migration(app_conninfo: str) -> None:
    """Apply `PEER_CONCURRENCY_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
