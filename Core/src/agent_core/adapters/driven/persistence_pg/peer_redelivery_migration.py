"""Migration 0027: the redelivery lease on `peer_messages`.

Phase:   Agent-comms v2, C3
Tests:   Core/tests/integration/test_peer_mailbox.py

`delivered_at` has been written by every claim since `0014`; the lease reads it. A row that is
`delivered`, unanswered and older than the lease whose workflow `peer-<correlation_id>` does not
exist is put back to `queued` (`PgAgentMailbox.reclaim_stale`) and `redeliveries` counts how
often that happened (observability now, the budget a poison-ask limit can read later).

The partial index keeps the reclaim scan on the delivered tail only, ordered by age, instead of
reading the whole history of answered asks.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_REDELIVERY_MIGRATION = Migration(
    id="0027_peer_messages_redelivery",
    sql="""
    ALTER TABLE peer_messages
        ADD COLUMN IF NOT EXISTS redeliveries INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS ix_peer_messages_delivered
        ON peer_messages (target_agent_id, delivered_at, correlation_id)
        WHERE state = 'delivered';
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_REDELIVERY_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_REDELIVERY_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_REDELIVERY_MIGRATION.id,)
        )


async def apply_peer_redelivery_migration(app_conninfo: str) -> None:
    """Apply `PEER_REDELIVERY_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
