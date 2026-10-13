"""Migration 0043: the tool journal (C30, D65).

Phase:   Agent-comms v2, C30
Tests:   Core/tests/contract/test_tool_journal.py, Core/tests/integration/test_migrations.py

One row per tool call that is not `replay: safe`: the turn and the call's key (the provider's
tool-call id qualified by the model-response ordinal, `j<version>:r<n>:<id>`, D68, D69), the
tool's name, when it began and, once it ended, when. A row without `finished_at` is a call a crash
interrupted. Additive; nothing reads it but the journal. Rows are not deleted here: they are small
and they are the evidence of an interruption (trim them on your own schedule, OPERATIONS.md).
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

TOOL_JOURNAL_MIGRATION = Migration(
    id="0043_tool_executions",
    sql="""
    CREATE TABLE IF NOT EXISTS tool_executions (
        turn_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        PRIMARY KEY (turn_id, tool_call_id)
    );
    """,
)


async def apply_tool_journal_migration(app_conninfo: str) -> None:
    """Apply `TOOL_JOURNAL_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, TOOL_JOURNAL_MIGRATION)
