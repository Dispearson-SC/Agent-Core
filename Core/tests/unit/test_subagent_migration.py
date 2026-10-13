"""C1r R4: the `kind` CHECK constraint is added without a long table lock.

Subject: adapters/driven/persistence_pg/subagent_migration.py (migrations 0041 and 0042).

`ADD CONSTRAINT ... CHECK` validates every row while holding a lock that blocks writes, and the
mailbox is a hot table. The constraint is added `NOT VALID` (a catalogue change; new rows are
checked at once) and validated by a SEPARATE migration. They must be separate: one migration is one
implicit transaction, in which the validation would still run under the first statement's lock.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import discover_app_migrations
from agent_core.adapters.driven.persistence_pg.subagent_migration import (
    SUBAGENT_MIGRATION,
    SUBAGENT_VALIDATE_MIGRATION,
)


def test_the_constraint_is_added_not_valid() -> None:
    assert "NOT VALID" in SUBAGENT_MIGRATION.sql
    assert "VALIDATE CONSTRAINT" not in SUBAGENT_MIGRATION.sql


def test_validation_is_its_own_later_migration() -> None:
    ids = [m.id for m in discover_app_migrations()]

    assert "VALIDATE CONSTRAINT peer_messages_kind_check" in SUBAGENT_VALIDATE_MIGRATION.sql
    assert ids.index(SUBAGENT_MIGRATION.id) < ids.index(SUBAGENT_VALIDATE_MIGRATION.id)
