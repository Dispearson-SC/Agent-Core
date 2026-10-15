"""Migration 0049: the fast path's verdict and answers on `audit_turn_routes` (Phase 5, S10).

Phase:   Phase 5 (fast communication), P12
Tests:   Core/tests/integration/test_route_audit.py, Core/tests/integration/test_migrations.py

With the fast path shadow or on, a router answers several questions in the call that picks its
route (`domain/fast_path.py`). Nullable columns, NULL on every row written with the fast path off
and on rows written before it:

    fast_path_mode      shadow or on
    fast_path           the verdict (`composite`, `pass:<why>`, `defer:<why>`); in shadow, what it
                        WOULD have done
    fast_path_intents   the detected intents, comma separated
    assessment          every answer with its value (`Assessment.summary`)

Additive and idempotent.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

FAST_PATH_AUDIT_MIGRATION = Migration(
    id="0049_audit_turn_routes_fast_path",
    sql="""
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS fast_path_mode TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS fast_path TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS fast_path_intents TEXT;
    ALTER TABLE audit_turn_routes ADD COLUMN IF NOT EXISTS assessment TEXT;
    """,
)


async def apply_fast_path_audit_migration(app_conninfo: str) -> None:
    """Apply `FAST_PATH_AUDIT_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, FAST_PATH_AUDIT_MIGRATION)
