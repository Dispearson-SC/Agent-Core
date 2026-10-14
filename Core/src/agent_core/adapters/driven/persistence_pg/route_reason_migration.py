"""Migration 0046: the router's reason and token usage on `audit_turn_routes` (D95, L12).

Phase:   Phase 5 (fast communication), P3
Tests:   Core/tests/integration/test_route_audit.py, Core/tests/integration/test_migrations.py

Three nullable columns: `reason` (the router's own code for what it answered, bounded by
`RouteTurn`), `input_tokens` and `output_tokens` (what the consultation spent, NULL when the router
said nothing). Rows written before it read NULL, which is what they knew. Additive and idempotent.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

ROUTE_REASON_MIGRATION = Migration(
    id="0046_audit_turn_routes_reason_usage",
    sql="""
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS reason TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS input_tokens INTEGER;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS output_tokens INTEGER;
    """,
)


async def apply_route_reason_migration(app_conninfo: str) -> None:
    """Apply `ROUTE_REASON_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, ROUTE_REASON_MIGRATION)
