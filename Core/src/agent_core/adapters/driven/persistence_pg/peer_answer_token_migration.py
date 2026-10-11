"""Migration 0026: a per-ask secret on `peer_messages`.

Phase:   Agent-comms v2, C1c
Tests:   Core/tests/integration/test_peer_mailbox.py

WHY
    The correlation id of a peer ask is derived (`domain.peers.peer_ask_id`, uuid5) from the
    asking turn, the tool call, the target and the question. Anyone who knows those inputs can
    compute it, and an inbound answer was matched by that id alone. `answer_token` is a random
    256-bit secret minted by `PgAgentMailbox.ask` the first time the row is inserted (a replayed
    insert keeps it) and handed only to the claimed `PeerAsk`. `answer` requires it.

ROWS WITHOUT A TOKEN
    Rows enqueued before this migration have a NULL token. They are answered with a NULL token
    (`IS NOT DISTINCT FROM`), so asks in flight across the upgrade still complete. That is the
    only window in which an id alone answers a row, and it closes as those rows drain.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_ANSWER_TOKEN_MIGRATION = Migration(
    id="0026_peer_messages_answer_token",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS answer_token TEXT;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (PEER_ANSWER_TOKEN_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_ANSWER_TOKEN_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_ANSWER_TOKEN_MIGRATION.id,)
        )


async def apply_peer_answer_token_migration(app_conninfo: str) -> None:
    """Apply `PEER_ANSWER_TOKEN_MIGRATION` once. Production uses `apply_all_migrations`; this
    is for a test that needs exactly this one. Sync psycopg in a thread (D13)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
