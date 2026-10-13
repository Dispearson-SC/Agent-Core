"""Migrations 0041 and 0042: what kind of child an ask is, and how deep it sits (C28, D62).

Phase:   Agent-comms v2, C28 (0042: C1r)
Tests:   Core/tests/contract/test_mailbox_conformance.py, Core/tests/integration/test_migrations.py,
         Core/tests/unit/test_subagent_migration.py

`peer_messages.kind` is `peer` (an agent was asked) or `subagent` (a parent turn spawned it);
`subagent_depth` is how many subagent links the chain down to the callee holds. Both are NOT NULL
with a constant default, so every row written before the migration is a peer ask at depth 0, which
is what it was, and a running build that does not know the columns keeps inserting without them.
Additive.

THE CHECK IS TWO MIGRATIONS ON PURPOSE. A plain `ADD CONSTRAINT ... CHECK` scans the whole table
while holding a lock that blocks writes, and `peer_messages` is the hot table of the mailbox. 0041
adds the constraint `NOT VALID` (no scan; every new row is checked from that moment) and 0042
validates the old rows with `VALIDATE CONSTRAINT`, which only takes a lock that lets reads and
writes continue. They are separate migrations because one migration runs as one implicit
transaction, in which the validation would still sit under the first statement's lock. 0041 was
edited in place (it was never deployed); a database that already applied the old 0041 holds a valid
constraint and 0042 is a no-op there.
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
                CHECK (kind IN ('peer', 'subagent')) NOT VALID;
        END IF;
    END $$;
    """,
)

SUBAGENT_VALIDATE_MIGRATION = Migration(
    id="0042_peer_messages_kind_validate",
    sql="ALTER TABLE peer_messages VALIDATE CONSTRAINT peer_messages_kind_check;",
)


async def apply_subagent_migration(app_conninfo: str) -> None:
    """Apply 0041 then 0042 once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, SUBAGENT_MIGRATION)
    await apply_migration(app_conninfo, SUBAGENT_VALIDATE_MIGRATION)
