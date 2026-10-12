"""Migration 0032: a peer answer carries its outcome as data (C1l, D38).

Phase:   Agent-comms v2, C1l
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_migrations.py

Two additive columns on `peer_messages`, both with a default, so an older build keeps working on a
migrated table and every row written before this migration reads as "not a failure, no failures":

* `answer_failed` - the stored answer is OUR fixed failure sentence for a callee that raised or was
  closed (`AgentMailbox.answer(failed=True)`), not text a peer wrote. The asker's `<peer status>`
  reads it instead of comparing the text with the fixed sentences, which a peer could also write
  and which a short `max_answer_chars` could cut.
* `callee_failures` - how many of the lease's requeues followed a callee that FAILED. The
  redelivery cap counts these, not every requeue, so a worker crash or a deploy that retires a
  workflow does not abandon an ask (`PeerAsk.failures`).
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_OUTCOME_MIGRATION = Migration(
    id="0032_peer_messages_outcome",
    sql="""
    ALTER TABLE peer_messages
        ADD COLUMN IF NOT EXISTS answer_failed BOOLEAN NOT NULL DEFAULT false;
    ALTER TABLE peer_messages
        ADD COLUMN IF NOT EXISTS callee_failures INTEGER NOT NULL DEFAULT 0;
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_OUTCOME_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_OUTCOME_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_OUTCOME_MIGRATION.id,))


async def apply_peer_outcome_migration(app_conninfo: str) -> None:
    """Apply `PEER_OUTCOME_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
