"""Migration 0040: the text of a streamed reply chunk (C26, D58).

Phase:   Agent-comms v2, C26
Tests:   Core/tests/integration/test_turn_events.py, Core/tests/integration/test_migrations.py

`turn_events.text` holds the chunk of a `delta` event. It is NULL for every other kind (an event
about a turn never carries model text) and for every row written before streaming existed. Additive
and nullable: a running build that does not know the column is unaffected by it.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

TURN_EVENT_TEXT_MIGRATION = Migration(
    id="0040_turn_events_text",
    sql="ALTER TABLE turn_events ADD COLUMN IF NOT EXISTS text TEXT;",
)


async def apply_turn_event_text_migration(app_conninfo: str) -> None:
    """Apply `TURN_EVENT_TEXT_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, TURN_EVENT_TEXT_MIGRATION)
