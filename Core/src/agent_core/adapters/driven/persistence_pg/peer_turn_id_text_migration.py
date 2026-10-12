"""Migration 0029: `peer_messages.turn_id` is TEXT, not UUID.

Phase:   Agent-comms v2, C1f
Tests:   Core/tests/integration/test_peer_mailbox.py, test_peer_nested_asks.py

The asking turn of an ask is a workflow id. For a turn a person started that is a UUID, but a
callee's turn is the workflow `peer-<correlation id>` (D29), and that callee may itself ask a
third agent (A -> B -> C). Its ask was written with `turn_id = 'peer-<id>'`, which a UUID
column refuses (`InvalidTextRepresentation`), and `cancel_open` could not even be asked about
such an id. The column becomes TEXT, holding the id verbatim.

`ALTER COLUMN ... TYPE` rebuilds what depends on the column on its own: the unique constraint
`uq_peer_messages_turn_target_question (turn_id, target_agent_id, question)` that makes `ask`
idempotent and the partial index `ix_peer_messages_open_by_turn` keep their definitions, now over
the text values. Existing rows keep the canonical lower-case hyphenated text of their UUID.

The `transcript_entries` view reads `peer_messages.turn_id`, and Postgres will not retype a column
a view depends on, so the view is dropped and re-created inside this migration from the same
definition 0016 uses (`TRANSCRIPT_ENTRIES_VIEW_SQL`, whose `turn_id` is TEXT in every branch).
Nothing else reads the view's old UUID typing: its consumer stringifies the id.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.adapters.driven.persistence_pg.transcript_migration import (
    TRANSCRIPT_ENTRIES_VIEW_SQL,
)

PEER_TURN_ID_TEXT_MIGRATION = Migration(
    id="0029_peer_messages_turn_id_text",
    sql=f"""
    DROP VIEW IF EXISTS transcript_entries;
    ALTER TABLE peer_messages ALTER COLUMN turn_id TYPE TEXT USING turn_id::text;
    {TRANSCRIPT_ENTRIES_VIEW_SQL};
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_TURN_ID_TEXT_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_TURN_ID_TEXT_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_TURN_ID_TEXT_MIGRATION.id,)
        )


async def apply_peer_turn_id_text_migration(app_conninfo: str) -> None:
    """Apply `PEER_TURN_ID_TEXT_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
