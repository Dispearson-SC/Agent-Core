"""Migration 0022 - what the winning rule actually SAID.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-07
Status:  DONE - the column `PolicyDecision.reason` had no home in
Tests:   Core/tests/integration/test_audit_reader.py

WHAT WAS MISSING, AND WHY IT IS NOT RECOVERABLE FROM ANYTHING ELSE
    `0005_audit_tool_calls` gave the table turn_id, caller, tool, arguments, effect,
    rule_id and at. So the trail could say WHICH rule fired and never WHAT IT SAID.

    `PolicyDecision.reason` is not decoration and has two audiences, both named in
    `domain/policy.py`: on DENY it is handed back to the model as the tool result, so the
    agent adapts instead of retrying blindly; on NEEDS_APPROVAL it is the sentence a human
    is asked. It is therefore the only record of why a refusal made sense AT THE TIME.

    `rule_id` does not substitute for it. A rule's text changes - that is what `policy_rules`
    is for - so joining the id back to the rule six months later answers with today's
    wording, which is precisely the wording that differs on the day somebody edits a rule
    to explain an incident.

WHY THIS IS NOT IN migrations.py
    Seven anchors create or alter a table. If each reached for the next free id in
    `migrations.py`, that module would be a file seven anchors write and the wave
    scheduler would have to serialise all seven. `docs/TASKS.md` pre-allocates the ids and
    each anchor defines its own `Migration` in its own module, following
    `PROFILE_SNAPSHOT_MIGRATION`. `0022` is this one's, spent the moment it was written
    down there.

    Discovery is automatic: `migrations.discover_app_migrations()` imports every module of
    this package and collects the `Migration` objects it finds, so there is no
    registration line for the next agent to forget.

FORWARD-ONLY, AND NULLABLE ON PURPOSE (CLAUDE.md non-negotiable #6)
    `migrations.py`'s RULE: never destructive on an audit table. This ADDs a column and
    nothing else - no DROP, no rewrite, and `IF NOT EXISTS` makes re-running it a no-op on
    a database that already has it.

    The column is NULLABLE and gets no DEFAULT. Rows written before this migration have no
    sentence, and that is a true statement about them; a `NOT NULL DEFAULT ''` would make
    every historical denial claim it was refused for no stated reason, and a backfilled
    constant is a fabricated audit record - worse than an honest gap. A reader that finds
    NULL is reading a row from before the column existed.

    There is still no UPDATE path to this table anywhere in the tree. The sentence rides
    the same INSERT `PgAuditSink.record_tool_call` already writes, on the audit pool's own
    connection, outside the domain transaction.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

AUDIT_TOOL_CALL_REASON_MIGRATION = Migration(
    id="0022_audit_tool_calls_reason",
    sql="""
    ALTER TABLE audit_tool_calls ADD COLUMN IF NOT EXISTS reason TEXT;
    """,
)


def _apply_audit_reason_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (AUDIT_TOOL_CALL_REASON_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(AUDIT_TOOL_CALL_REASON_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (AUDIT_TOOL_CALL_REASON_MIGRATION.id,),
        )


async def apply_audit_reason_migration(app_conninfo: str) -> None:
    """Apply `AUDIT_TOOL_CALL_REASON_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates both the `audit_tool_calls`
    table this alters and the `schema_migrations` tracking table this reads. Idempotent by
    that tracking and by `IF NOT EXISTS` besides.

    Production does not call this: `migrations.apply_all_migrations` applies every
    discovered `Migration` in id order on one connection, which is the only way the order
    of the set can be right. This function exists for the same reason every sibling's
    does - a test that needs exactly this one against exactly one database.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_audit_reason_migration_sync, app_conninfo)
