"""Migration 0041: what kind of child an ask is, and how deep it sits (C28, D62).

Phase:   Agent-comms v2, C28
Tests:   Core/tests/contract/test_mailbox_conformance.py, Core/tests/integration/test_migrations.py

`peer_messages.kind` is `peer` (an agent was asked) or `subagent` (a parent turn spawned it);
`subagent_depth` is how many subagent links the chain down to the callee holds. Both are NOT NULL
with a constant default, so every row written before the migration is a peer ask at depth 0, which
is what it was, and a running build that does not know the columns keeps inserting without them.
Additive.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

SUBAGENT_MIGRATION = Migration(
    id="0041_peer_messages_subagent",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'peer';
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS subagent_depth INTEGER NOT NULL DEFAULT 0;
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'peer_messages_kind_check'
        ) THEN
            ALTER TABLE peer_messages ADD CONSTRAINT peer_messages_kind_check
                CHECK (kind IN ('peer', 'subagent'));
        END IF;
    END $$;
    """,
)


async def apply_subagent_migration(app_conninfo: str) -> None:
    """Apply `SUBAGENT_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, SUBAGENT_MIGRATION)
