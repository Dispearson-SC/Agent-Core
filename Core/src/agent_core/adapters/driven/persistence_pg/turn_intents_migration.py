"""Migration 0047: the audit trail of intent plans (D97).

Phase:   Phase 5 (fast communication), P3
Tests:   Core/tests/integration/test_route_audit.py, Core/tests/integration/test_migrations.py

`audit_turn_intents` holds one row per intent plan a turn tried (`RouteAudit.record_intent`): the
intent, the profile it ran as, whether the plan answered the turn, how many tools it called, its
reply mode (`compose` or `template`) and, when it fell back to the normal path, why. Append-only
like every `audit_*` table (CLAUDE.md #6); no argument values and no result text (each tool call
has its own `audit_tool_calls` row). The route that led to it is the turn's `audit_turn_routes` row.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

TURN_INTENTS_MIGRATION = Migration(
    id="0047_audit_turn_intents",
    sql="""
    CREATE TABLE IF NOT EXISTS audit_turn_intents (
        id BIGSERIAL PRIMARY KEY,
        turn_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        intent TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        plan_used BOOLEAN NOT NULL,
        tool_count INTEGER NOT NULL,
        reply_mode TEXT,
        fallback_reason TEXT,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_audit_turn_intents_turn ON audit_turn_intents (turn_id);
    """,
)


async def apply_turn_intents_migration(app_conninfo: str) -> None:
    """Apply `TURN_INTENTS_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, TURN_INTENTS_MIGRATION)
