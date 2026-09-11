"""Migration 0023 - the tenant an audit row belongs to.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-21
Tests:   Core/tests/integration/test_schema_truths.py

WHAT WAS MISSING
    `0005_audit_tool_calls` gave the table turn_id, caller, tool, arguments, effect,
    rule_id and at; `0022` added reason. Not one of them says WHICH TENANT the call
    belonged to, so `AuditReader` could be scoped only to a turn - and "show me this
    tenant's refusals" had no column to ask about.

WHY NOT A JOIN TO `turns`, WHICH ALREADY HAS `tenant_id`
    Because the two tables are deliberately not written together. CLAUDE.md
    non-negotiable #6 puts the audit write OUTSIDE the domain transaction precisely so
    that a turn whose domain writes rolled back still leaves a trace: that turn has audit
    rows and NO `turns` row. An inner join would then answer completely on every
    successful turn and silently drop every failed one - the trail would be perfect
    exactly where nothing went wrong, and empty exactly where somebody is looking. A fix
    that works on the happy path and erases evidence on the sad one is worse than the gap
    it closes.

    A column costs one more value on an INSERT that was already happening, and it is
    written from `caller.tenant_id` - the same identity the policy decision on that row
    was made for, carried on the same statement, so the tenant and the verdict can never
    be the halves that did not land together.

FORWARD-ONLY, AND NULLABLE ON PURPOSE
    `migrations.py`'s RULE: never destructive on an audit table. This ADDs a column and
    nothing else - no DROP, no rewrite, no backfill - and `IF NOT EXISTS` makes a re-run a
    no-op.

    WHAT NULL MEANS: the row was written before this column existed, and this codebase
    does not know which tenant it belonged to. It does NOT mean "every tenant", and it
    must never be read as one - a reader scoping to a tenant must not return NULL rows,
    because that would hand one tenant another's evidence on the strength of a gap.

    The existing rows are NOT backfilled, and the obvious source for a backfill is the
    join this migration exists to avoid: it would stamp a tenant on every row whose turn
    committed and leave NULL on exactly the rolled-back ones, which is a fabricated audit
    record for the happy path and an honest gap only where the join happened to work.
    A constant would be worse still. An audit trail that says "not recorded" is telling
    the truth; one that says "t-1" because a join said so is not.

    NOT NULL is therefore impossible on this table as it stands, and that is the cost of
    having shipped without the column rather than a design choice. New rows always carry
    a tenant, because `PgAuditSink.record_tool_call` takes the `CallerIdentity` and
    `CallerIdentity.tenant_id` is not optional.

THE INDEX LEADS ON THE TENANT
    `(tenant_id, id)`, so a tenant-scoped read is one range scan in append order and the
    same index serves "this tenant's most recent calls". `id` is the BIGSERIAL the row was
    appended under, which is the append order itself; `at` ties inside a millisecond.

WHY THIS IS NOT IN migrations.py
    The convention docs/TASKS.md established and `0022` follows: each anchor defines its
    own `Migration` in its own module, so `migrations.py` is never a write two anchors
    share and no wave has to serialise them. `discover_app_migrations()` imports every
    module of this package and collects what it finds, so there is no registration line to
    forget. `0023` is this one's, pre-allocated there.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

AUDIT_TOOL_CALL_TENANT_MIGRATION = Migration(
    id="0023_audit_tool_calls_tenant",
    sql="""
    ALTER TABLE audit_tool_calls ADD COLUMN IF NOT EXISTS tenant_id TEXT;
    CREATE INDEX IF NOT EXISTS ix_audit_tool_calls_tenant_id
        ON audit_tool_calls (tenant_id, id);
    """,
)


def _apply_audit_tenant_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (AUDIT_TOOL_CALL_TENANT_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(AUDIT_TOOL_CALL_TENANT_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (AUDIT_TOOL_CALL_TENANT_MIGRATION.id,),
        )


async def apply_audit_tenant_migration(app_conninfo: str) -> None:
    """Apply `AUDIT_TOOL_CALL_TENANT_MIGRATION`, once, against one database.

    Production does not call this. `migrations.apply_all_migrations` applies every
    discovered `Migration` in id order on one connection, which is the only way the order
    of the SET can be right; this exists for the same reason every sibling's does - a test
    that needs exactly this one. It needs `audit_tool_calls` and `schema_migrations` to
    exist already, so it runs after the applier, and is idempotent by that tracking and by
    `IF NOT EXISTS` besides.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_audit_tenant_migration_sync, app_conninfo)
