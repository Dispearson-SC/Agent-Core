"""Migration 0036: an ask can be a background ask (C19, D49).

Phase:   Agent-comms v2, C19
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_migrations.py

`peer_messages.background` is NULL for an ordinary ask. For a BACKGROUND ask it holds the identity
the answer is delivered to (subject, channel, roles, modality; the tenant and the conversation are
the row's own `from_tenant_id` and `from_session_id`). It lives on the row, not in the asking
workflow, because the asking turn has ended by the time the answer exists: the return address has
to survive the asker and a restart.

The partial index serves the one question asked of it, "the background asks of this conversation".
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_BACKGROUND_MIGRATION = Migration(
    id="0036_peer_messages_background",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS background JSONB;
    CREATE INDEX IF NOT EXISTS ix_peer_messages_background_conversation
        ON peer_messages (from_tenant_id, from_session_id)
        WHERE background IS NOT NULL;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_BACKGROUND_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_BACKGROUND_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_BACKGROUND_MIGRATION.id,)
        )


async def apply_peer_background_migration(app_conninfo: str) -> None:
    """Apply `PEER_BACKGROUND_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
