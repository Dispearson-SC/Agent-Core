"""Migration 0019 - the refused-decision audit table.

Phase:   F3 - Deferred human interaction
Tasks:   docs/TASKS.md#t-f3-14
Status:  DONE - the table `PgAuditSink.record_rejected_decision` had no home in
Decision: docs/DECISIONS.md#d25
Tests:   Core/tests/integration/test_audit_repository.py

WHY THIS IS NOT IN migrations.py
    Seven anchors create a table. If each reached for the next free id in
    `migrations.py`, that module would be a file seven anchors write and the scheduler
    would have to serialise all seven, in every wave they appear in, over a list.
    `docs/TASKS.md` pre-allocates the ids instead and each anchor defines its own
    `Migration` in its own module, following `PROFILE_SNAPSHOT_MIGRATION`. `0019` is
    this one's, spent the moment it was written down.

    The object is shaped so the owner of `migrations.py` can append it to
    `APP_MIGRATIONS` verbatim if the two ever converge: it is tracked in
    `schema_migrations` under its own id, so doing so is a no-op on any database that
    already ran it through `apply_audit_rejected_migration`.

WHY THE TABLE IS ITS OWN AND NOT A COLUMN ON audit_human_decisions
    `audit_human_decisions.approved` is NOT NULL, because that member means "a human
    decided" and a decision without a verdict is not one. A refused attempt has no
    verdict to put there: nothing was decided, somebody was stopped. Whichever value
    went into that column would assert a decision the human never made - which is the
    false record D25 chose silence over, and the whole reason `record_rejected_decision`
    is a second member rather than a flag on the first.

    So the shape differs from `audit_human_decisions` in exactly two places, and both
    are the point: there is no `approved`, and `reason` is NOT NULL. The row is read
    once, months later, by someone asking why an approval did not take effect, and
    "refused" with no grounds cannot be told apart from a clerical error.

APPEND-ONLY (CLAUDE.md non-negotiable #6)
    No UPDATE and no DELETE path exists for this table anywhere in the tree. It is the
    ONLY evidence the four-eyes control fired, and a control nobody can show fired is
    indistinguishable from one that was never wired.

    The index leads on `turn_id` because that is the question the table is read for:
    "what was attempted against this turn, and who by?". `at` follows it so a reader
    gets the attempts in the order they happened without a sort.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

AUDIT_REJECTED_DECISIONS_MIGRATION = Migration(
    id="0019_audit_rejected_decisions",
    sql="""
    CREATE TABLE IF NOT EXISTS audit_rejected_decisions (
        id BIGSERIAL PRIMARY KEY,
        turn_id UUID NOT NULL,
        tool_call_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_audit_rejected_decisions_turn
        ON audit_rejected_decisions (turn_id, at);
    """,
)


def _apply_audit_rejected_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (AUDIT_REJECTED_DECISIONS_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(AUDIT_REJECTED_DECISIONS_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (AUDIT_REJECTED_DECISIONS_MIGRATION.id,),
        )


async def apply_audit_rejected_migration(app_conninfo: str) -> None:
    """Apply `AUDIT_REJECTED_DECISIONS_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the `schema_migrations`
    tracking table this reads. Idempotent by that tracking and by `IF NOT EXISTS`, and
    forward-only: it creates a table and an index, and never drops or rewrites one.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_audit_rejected_migration_sync, app_conninfo)
