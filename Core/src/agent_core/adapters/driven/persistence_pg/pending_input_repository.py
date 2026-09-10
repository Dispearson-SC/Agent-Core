"""Driven adapter: pending-input buffer over Postgres.

Phase:   F2 (durability / coalescing)
Tasks:   docs/TASKS.md#t-f2-04
Status:  append() and drain() DONE.

TABLE
    pending_inputs (id bigserial pk, session_id, tenant_id, text, created_at)

SCOPE - WHY THERE IS NO DRAIN STEP HERE
    This module is the table and the repository primitive only. The code that actually
    CALLS `drain()` as the turn workflow's first step is docs/TASKS.md#t-f2-10, a later
    anchor writing adapters/driving/workflow/turn_workflow.py. docs/TASKS.md's note under
    this anchor explains why the two were split - one task naming a file in one layer and
    an activity that lives in another is itself the defect (the same class as t-f1-04).

WHY A ROW PER MESSAGE (D19)
    D19 describes appending "a message" to "a pending-input buffer row for the session"
    and later draining "the buffer". Read as one row per arriving message, not a single
    row mutated in place: a row per message is what makes the ordering guarantee
    checkable (`ORDER BY id` over immutable rows) instead of trusting that concurrent
    appends serialized correctly inside one UPDATE ... array_append.

WHY DRAIN IS ONE STATEMENT - the transactional half of the contract
    `drain` issues a single `DELETE ... RETURNING`, never a SELECT followed by a DELETE.
    Two statements can crash between them: the rows would already be handed to the
    caller (the SELECT succeeded) while still sitting in the table (the DELETE never
    ran), and the NEXT drain would hand the same rows out again - a duplicate. One
    statement makes the read and the delete commit atomically: a crash before commit
    loses the delete entirely (the rows are still there - nothing lost) and a crash
    after commit has already handed the rows to the caller (nothing left to duplicate).
    There is no window in which the answer is given out twice.

WHY ORDERING IS RESOLVED IN PYTHON, NOT SQL
    Postgres does not accept `ORDER BY` on `DELETE`, and `RETURNING` makes no promise
    about the order it hands rows back in. `id` (bigserial) is monotonic in arrival
    order regardless of that, so the adapter sorts the returned rows by `id` after the
    delete instead of trusting whatever order psycopg happens to report.

MEDIA IS NOT BUFFERED YET
    `UserInput.media` exists for F7, which has not landed. Every message reconstructed
    off this table carries `media=()`; buffering media references is out of this
    anchor's scope, left for whichever F7 anchor extends it.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.domain.turn import SessionRef, UserInput

# Id 0011 is pre-allocated to this anchor in docs/TASKS.md. Defined here, in this
# module, rather than appended to migrations.py - the same precedent as
# conversation_repository.PROFILE_SNAPSHOT_MIGRATION - so migrations.py is never a
# shared write and no two anchors collide on it (docs/WAVES.md).
PENDING_INPUT_MIGRATION = Migration(
    id="0011_pending_inputs",
    sql="""
    CREATE TABLE IF NOT EXISTS pending_inputs (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS ix_pending_inputs_session_id ON pending_inputs (session_id, id);
    """,
)

_APPEND_SQL = """
    INSERT INTO pending_inputs (session_id, tenant_id, text) VALUES (%s, %s, %s)
"""

# Kept as a module constant, not inlined, so the integration test can force the exact
# same statement on its own connection to simulate a crash mid-drain - see
# test_pending_input.py.
_DRAIN_SQL = """
    DELETE FROM pending_inputs WHERE session_id = %s RETURNING id, text
"""


def _apply_pending_input_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (PENDING_INPUT_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PENDING_INPUT_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PENDING_INPUT_MIGRATION.id,)
        )


async def apply_pending_input_migration(app_conninfo: str) -> None:
    """Apply `PENDING_INPUT_MIGRATION`, once, after `migrations.run_migrations`.

    Idempotent by the same `schema_migrations` tracking every other migration in this
    package uses, and forward-only - it never drops or rewrites the table.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_pending_input_migration_sync, app_conninfo)


class PgPendingInputBuffer:
    """Postgres-backed buffer for messages arriving inside D19's coalescing window.

    D13: both members are async; the psycopg calls underneath are synchronous and run
    in a thread, exactly as in `PgConversationStore`.
    """

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def append(self, session: SessionRef, message: UserInput) -> None:
        """Record one arriving message for `session`.

        No cross-session lock here and none needed: ordering only has to hold WITHIN a
        session (the bigserial `id`), and D19's serialization across turns for one
        session is a separate mechanism (the partitioned DBOS queue), not this table.
        """
        await asyncio.to_thread(self._append_sync, session, message)

    def _append_sync(self, session: SessionRef, message: UserInput) -> None:
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(_APPEND_SQL, (session.session_id, session.tenant_id, message.text))

    async def drain(self, session: SessionRef) -> tuple[UserInput, ...]:
        """Return every message buffered for `session`, in arrival order, and empty the
        buffer as part of the same statement (see the module docstring for why that is
        one `DELETE ... RETURNING` rather than a read followed by a delete).

        Empty when nothing is buffered - the NORMAL case for a session with no turn
        currently coalescing, not an error the caller must guard against.
        """
        return await asyncio.to_thread(self._drain_sync, session)

    def _drain_sync(self, session: SessionRef) -> tuple[UserInput, ...]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(_DRAIN_SQL, (session.session_id,)).fetchall()
        ordered = sorted(rows, key=lambda row: row[0])
        return tuple(UserInput(text=text) for _, text in ordered)
