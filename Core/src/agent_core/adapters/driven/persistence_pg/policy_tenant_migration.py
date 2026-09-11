"""Migration 0012 - the tenant column on `policy_rules`.

Phase:   D2 - Multi-tenancy
Tasks:   docs/TASKS.md#t-d2-03
Status:  DONE - column, backfill from the pre-D2 jsonb key, and the index the predicate uses
Tests:   Core/tests/integration/test_policy_tenant_sql.py

WHY THIS IS NOT IN migrations.py
    Seven anchors still to come each create or alter a table. If every one reached for the
    next free id in `migrations.py`, that module would be a file seven anchors write, and
    the scheduler would have to serialise all seven in every wave they appear in - over a
    list. `docs/TASKS.md` pre-allocates the ids instead and each anchor defines its own
    `Migration` in its own module, following `PROFILE_SNAPSHOT_MIGRATION`. `0012` is this
    one's, spent the moment it was written down there.

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on a database that already ran it through
    `apply_policy_tenant_migration`.

WHAT AN EXISTING ROW MEANS - THIS MIGRATION PRESERVES REACH, IT DOES NOT DECIDE IT
    The column is NULLABLE and NULL means ALL TENANTS, the reading `domain/policy.py`
    already gives `PolicyRule.tenant_id`. Read cold that looks like a privilege widening -
    every stored rule becoming platform-wide in one ALTER - so here is why it is not.

    Those rows are ALREADY platform-wide, today, before this runs. Migration 0004 shaped
    `policy_rules` as `(rule_id, definition jsonb, updated_at)` and nothing has ever
    written a tenant into `definition`; the pre-D2 predicate returned a row without one to
    every tenant and `_to_rule` built it with no tenant at all. `ADD COLUMN` gives those
    rows NULL, which is a faithful record of the reach they have, not a new grant.

    The fail-closed-looking alternative - NOT NULL, so a row naming no tenant matches no
    caller - is an outage rather than a defence. No matching rule already means DENY, so
    it would refuse every tool for every tenant on the first turn after deployment, and
    the operator's fix would be to re-grant rules under time pressure. Fail closed is
    right when it denies the thing in question; here it denies everything else too.

    The backfill covers the one case where reach WAS narrower than the column would say:
    an operator who hand-wrote `tenant_id` into the jsonb, which the pre-D2 predicate
    honoured. Those values are copied into the column, so no row is widened by moving the
    field. Rows written in the old shape AFTER this migration are still honoured, by
    `policy_repository._RULE_TENANT` reading the column and the jsonb key together.

FORWARD-ONLY AND NON-DESTRUCTIVE
    It adds a column, copies into it, and creates an index. It drops nothing and rewrites
    no rule. `definition` keeps its jsonb key where one exists, so a rollback to the
    previous adapter reads exactly what it read before.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

POLICY_TENANT_MIGRATION = Migration(
    id="0012_policy_rules_tenant",
    sql="""
    ALTER TABLE policy_rules ADD COLUMN IF NOT EXISTS tenant_id TEXT;
    UPDATE policy_rules
        SET tenant_id = definition->>'tenant_id'
        WHERE tenant_id IS NULL AND definition->>'tenant_id' IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_policy_rules_tenant
        ON policy_rules ((COALESCE(tenant_id, definition->>'tenant_id')));
    """,
)
# The index is over the EXPRESSION the adapter's WHERE clause uses, not over the bare
# column, for the same reason the knowledge full-text index is declared over the exact
# tsvector the query builds: an index over a different expression is not merely slower,
# it is never used, and nothing reports that. Both halves are IMMUTABLE - `jsonb ->> text`
# and `COALESCE` - which an expression index requires.


def _apply_policy_tenant_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (POLICY_TENANT_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(POLICY_TENANT_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (POLICY_TENANT_MIGRATION.id,),
        )


async def apply_policy_tenant_migration(app_conninfo: str) -> None:
    """Apply `POLICY_TENANT_MIGRATION`, once, after `migrations.run_migrations`.

    It runs after `run_migrations` for two reasons: that is what creates `policy_rules`
    (migration 0004), which this alters, and what creates the `schema_migrations` tracking
    table this reads. Idempotent by that tracking and by `IF NOT EXISTS` on both the column
    and the index, and the backfill is guarded on `tenant_id IS NULL` so a re-run can never
    overwrite a tenant an administrator has since changed.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_policy_tenant_migration_sync, app_conninfo)
