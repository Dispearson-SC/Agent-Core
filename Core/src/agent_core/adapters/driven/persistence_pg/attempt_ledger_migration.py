"""Migration 0030: the workflow attempt ledger.

Phase:   Agent-comms v2, C5
Tests:   Core/tests/contract/test_attempt_ledger.py, Core/tests/integration/test_poison_budget.py

One row per workflow that is being counted: how many times its body was entered in a row without
the previous entry having been parked on a wait (`attempts`), and whether the last entry was
parked when it ended (`parked`). See `turn_workflow.AttemptLedger` and DECISIONS D32. The row is
deleted when the workflow ends on its own terms; a poisoned workflow keeps its row as evidence.

Not part of DBOS' own tables on purpose: the count has to be written by the workflow BODY on
every entry, outside the operation log, and DBOS' own `recovery_attempts` counts every recovery,
including the restart of a turn that was merely waiting for a person.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

ATTEMPT_LEDGER_MIGRATION = Migration(
    id="0030_workflow_attempts",
    sql="""
    CREATE TABLE IF NOT EXISTS workflow_attempts (
        workflow_id TEXT PRIMARY KEY,
        attempts INTEGER NOT NULL,
        parked BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (ATTEMPT_LEDGER_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(ATTEMPT_LEDGER_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (ATTEMPT_LEDGER_MIGRATION.id,)
        )


async def apply_attempt_ledger_migration(app_conninfo: str) -> None:
    """Apply `ATTEMPT_LEDGER_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
