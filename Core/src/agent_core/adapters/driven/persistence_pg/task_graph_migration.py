"""Migration 0038: find the asks of a tree by its root (C23, D54).

Phase:   Agent-comms v2, C23
Tests:   Core/tests/integration/test_task_graph.py, Core/tests/integration/test_migrations.py

`GET /turns/{id}/graph` asks `peer_messages` for "every ask under this root turn". Until now
`root_turn_id` (migration 0035) was written and read back by key, never searched; this index serves
the search. Additive.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

TASK_GRAPH_MIGRATION = Migration(
    id="0038_peer_messages_root_index",
    sql="""
    CREATE INDEX IF NOT EXISTS ix_peer_messages_root
        ON peer_messages (from_tenant_id, root_turn_id);
    """,
)


async def apply_task_graph_migration(app_conninfo: str) -> None:
    """Apply `TASK_GRAPH_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, TASK_GRAPH_MIGRATION)
