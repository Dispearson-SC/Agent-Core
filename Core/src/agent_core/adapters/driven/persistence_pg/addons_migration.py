"""Migration 0044: the addons that are installed, and the migrations they ran (A7, D80).

Phase:   Phase 2 (addon system)
Tests:   Core/tests/unit/test_addon_install_plan.py, Core/tests/integration/test_addon_install.py

`addons` is the `ir.module.module` analogue: one row per addon that was ever installed, with the
version last installed successfully. `addon_migrations` records each of an addon's own
`migrations/*.sql` files once it ran, with the addon version at that time. Both are additive and
written only by `adapters/driven/addons_fs/install.py`; with no addon enabled they stay empty.
Rows are never deleted by Core: removing an addon from the enable list leaves them (D80).
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

ADDONS_MIGRATION = Migration(
    id="0044_addons",
    sql="""
    CREATE TABLE IF NOT EXISTS addons (
        name TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        upgraded_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS addon_migrations (
        addon TEXT NOT NULL,
        file TEXT NOT NULL,
        version TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (addon, file)
    );
    """,
)


async def apply_addons_migration(app_conninfo: str) -> None:
    """Apply `ADDONS_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, ADDONS_MIGRATION)
