"""Migration 0010 - the human-decision correlation table.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-04
Status:  DONE - table and its uniqueness constraint; applied by t-f3-06 wiring
Tests:   Core/tests/integration/test_human_gateway.py

WHY THIS IS NOT IN migrations.py
    Seven upcoming anchors create a table. If each reached for the next free id in
    `migrations.py`, that module would be a file seven anchors write and the scheduler
    would have to serialise all seven in every wave they appear in - over a list.
    `docs/TASKS.md` pre-allocates the ids instead, and each anchor defines its own
    `Migration` in its own module, following `PROFILE_SNAPSHOT_MIGRATION`. `0010` is this
    one's, spent the moment it was written down.

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on any database that already ran it through
    `apply_human_requests_migration`.

WHY THE UNIQUE CONSTRAINT IS THE POINT OF THIS TABLE
    `HumanGateway.publish` must be idempotent per (turn_id, tool_call_id): it runs inside
    a DBOS step, and a step is re-executed after a crash. A second row means a second
    correlation handle, which means a real person is asked the same question twice - and
    two answers to one question are two conflicting approvals for one action, with nothing
    to say which one counts.

    That rule lives here as a constraint rather than in the adapter as a SELECT-then-
    INSERT, because a check in application code is a race: two step attempts can both read
    "not published" before either writes. The database is the only place where the
    question is settled once.

    `correlation_id` is the primary key because it is what an inbound reply carries, and
    the uniqueness that actually protects the human is the secondary one.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

# `answered_at` is recorded but never filtered on by `correlate`. Resolving the same
# request twice is a no-op (docs/TASKS.md#t-f3-12), and a handle that 404s on the second
# POST is not a no-op - it is an error the human sees. Whether the answer is already in is
# `ResumeTurn`'s question (t-f3-02), and this column is what lets it be asked.
HUMAN_REQUESTS_MIGRATION = Migration(
    id="0010_human_requests",
    sql="""
    CREATE TABLE IF NOT EXISTS human_requests (
        correlation_id TEXT PRIMARY KEY,
        turn_id UUID NOT NULL,
        tool_call_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        answered_at TIMESTAMPTZ,
        CONSTRAINT uq_human_requests_turn_tool UNIQUE (turn_id, tool_call_id)
    );
    """,
)


def _apply_human_requests_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (HUMAN_REQUESTS_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(HUMAN_REQUESTS_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (HUMAN_REQUESTS_MIGRATION.id,)
        )


async def apply_human_requests_migration(app_conninfo: str) -> None:
    """Apply `HUMAN_REQUESTS_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the `schema_migrations`
    tracking table this reads. Idempotent by that tracking and by `IF NOT EXISTS`, and
    forward-only: it creates a table and never drops or rewrites one.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_human_requests_migration_sync, app_conninfo)
