"""Migration 0037: the progress events of a turn tree (C21, D52).

Phase:   Agent-comms v2, C21
Tests:   Core/tests/integration/test_turn_events.py, Core/tests/integration/test_migrations.py

`turn_events` is the append-only log behind the event feed (C22): one row per fact, `seq` the total
order a subscriber resumes from, `event_id` the unique key that makes publishing idempotent. A
`NOTIFY turn_events, '<seq>'` is sent with each new row; the payload is only the sequence number, so
it is always far below the 8000-byte NOTIFY limit and a listener reads the row itself.

Retention is not automatic (OPERATIONS: "Progress events").
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

TURN_EVENTS_MIGRATION = Migration(
    id="0037_turn_events",
    sql="""
    CREATE TABLE IF NOT EXISTS turn_events (
        seq          BIGSERIAL PRIMARY KEY,
        event_id     TEXT NOT NULL UNIQUE,
        tenant_id    TEXT NOT NULL,
        session_id   TEXT NOT NULL,
        turn_id      TEXT NOT NULL,
        root_turn_id TEXT NOT NULL,
        kind         TEXT NOT NULL,
        agent        TEXT NOT NULL,
        asker_turn_id TEXT,
        asker        TEXT,
        call         TEXT,
        tool         TEXT,
        elapsed_ms   INTEGER,
        detail       TEXT,
        at           TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_turn_events_root
        ON turn_events (tenant_id, root_turn_id, seq);
    CREATE INDEX IF NOT EXISTS ix_turn_events_turn ON turn_events (turn_id, kind);
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (TURN_EVENTS_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(TURN_EVENTS_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (TURN_EVENTS_MIGRATION.id,))


async def apply_turn_events_migration(app_conninfo: str) -> None:
    """Apply `TURN_EVENTS_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
