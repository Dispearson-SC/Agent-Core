"""Migration 0031: a peer ask is unique by its request key, not by its question (C13, D36).

Phase:   Agent-comms v2, C13
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_peer_mailbox.py

`peer_messages` was unique per `(turn_id, target_agent_id, question)`. That made `ask` idempotent
for a replayed step, and it also folded a legitimate repeat of the same question in a LATER round
into the first row, with the first answer (or a `cancelled` end state) for ever.

Since C2 the correlation id of an ask is derived from its request key, and since C13 that key names
the round and the position (`r<round>.s<position>.<tool call id>`), so the primary key already makes
a replay idempotent. The question constraint is therefore dropped for keyed asks:

* `request_key` records the key of the ask (NULL for a row written before this migration, or by a
  caller that gave none);
* the old uniqueness survives as a PARTIAL unique index over the rows with no key, so the random-id
  path (no `request_key`) is still idempotent per `(turn, target, question)` exactly as before.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_REQUEST_KEY_MIGRATION = Migration(
    id="0031_peer_messages_request_key",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS request_key TEXT;
    ALTER TABLE peer_messages DROP CONSTRAINT IF EXISTS uq_peer_messages_turn_target_question;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_peer_messages_unkeyed_turn_target_question
        ON peer_messages (turn_id, target_agent_id, question)
        WHERE request_key IS NULL;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_REQUEST_KEY_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_REQUEST_KEY_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_REQUEST_KEY_MIGRATION.id,)
        )


async def apply_peer_request_key_migration(app_conninfo: str) -> None:
    """Apply `PEER_REQUEST_KEY_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
