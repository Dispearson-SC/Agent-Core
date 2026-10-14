"""Migration 0045: the audit trail of turn routers (D95).

Phase:   Phase 5 (fast communication), P1
Tests:   Core/tests/integration/test_route_audit.py, Core/tests/integration/test_migrations.py

`audit_turn_routes` holds one row per router consultation (`RouteAudit.record_route`): which
router, the profile the person asked for, the profile the turn ran under, what the router answered
(NULL when it answered nothing), its label, confidence, calibration and model, how long it took and
why the turn fell back. Append-only like every `audit_*` table (CLAUDE.md #6); no model text and no
argument values. `turn_id` is TEXT because a turn id is the workflow id, not always a UUID.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

TURN_ROUTES_MIGRATION = Migration(
    id="0045_audit_turn_routes",
    sql="""
    CREATE TABLE IF NOT EXISTS audit_turn_routes (
        id BIGSERIAL PRIMARY KEY,
        turn_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        router TEXT NOT NULL,
        requested_profile TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        decision TEXT,
        label TEXT,
        confidence DOUBLE PRECISION,
        calibrated BOOLEAN NOT NULL,
        model TEXT,
        intent TEXT,
        elapsed_ms INTEGER NOT NULL,
        fallback_reason TEXT,
        at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_audit_turn_routes_turn ON audit_turn_routes (turn_id);
    """,
)


async def apply_turn_routes_migration(app_conninfo: str) -> None:
    """Apply `TURN_ROUTES_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, TURN_ROUTES_MIGRATION)
