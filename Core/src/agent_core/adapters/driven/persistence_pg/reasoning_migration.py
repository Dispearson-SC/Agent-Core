"""Migration 0015 - reasoning blocks, admin-only.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-04
Status:  PENDING - the table exists, the store does not write to it yet
Tests:   Core/tests/integration/test_reasoning_persistence.py

WHY THIS IS NOT IN migrations.py
    Seven anchors create a table, and `docs/TASKS.md` pre-allocates their ids so that
    `migrations.py` never becomes a file seven anchors write - `docs/WAVES.md` rule 2
    would otherwise serialise all seven in every wave they appear in, over a list. `0015`
    is this anchor's, following the precedent of `PROFILE_SNAPSHOT_MIGRATION` (`0009`,
    next door in conversation_repository.py) and `PEER_MESSAGES_MIGRATION` (`0014`).

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on a database that already ran it here.

WHY REASONING GETS ITS OWN TABLE INSTEAD OF A `messages` ROW
    A `messages` row is the cheap option and it is the wrong one. `load_history` reads
    that table on every turn, and it is both the model's conversation and the raw material
    of the USER transcript. Put reasoning there and the only thing between the model's
    private speculation and the person it speculated about is a WHERE clause that every
    present and future reader has to remember.

    `domain/transcript.py` answers REASONING as `USER: False, ADMIN: True`. A separate
    table makes that answer structural: a USER read path that never names this table
    cannot leak from it by forgetting something.

WHY THE UNIQUE CONSTRAINT IS ON A CONTENT HASH
    `append_reasoning` runs inside a DBOS step, and a step is re-executed after a crash.
    `docs/FIELD-NOTES.md` records that MiniMax M3 returns `reasoning_content` in BOTH
    modes, so every turn produces a block and every retried turn would produce a second
    copy of it. The rule lives here as a constraint rather than in the adapter as
    SELECT-then-INSERT, because a check in application code is a race: two step attempts
    can both read "not stored" before either writes.

    The key is (turn_id, sha256(content)) rather than a sequence number, because a retry
    has no way to know which attempt it is. The cost is that two GENUINELY identical
    blocks inside one turn collapse into one row - and collapsing an indistinguishable
    duplicate is the safe direction, given that the alternative is a transcript claiming
    the agent had the same thought twice whenever a step retried.

WHY THE TABLE CARRIES ITS OWN tenant_id
    The read is scoped by (session_id, tenant_id) in the SQL, not filtered afterwards.
    This is the same rule `ports/transcript_reader.py` states for the projection, and it
    applies hardest here: reasoning is the most sensitive row in the transcript, and a
    guessed or reused session id must not be enough to read one.

RETENTION IS A SEPARATE DECISION AND IS STILL OPEN
    Reasoning traces are bulky and hold guesses about a user that were never said out
    loud; storing them forever is a liability, not an asset. `created_at` is indexed so a
    retention sweep is a range delete rather than a table scan, but WHAT the window is,
    is `docs/TASKS.md#t-f10-05` and belongs in `docs/DECISIONS.md`. This migration does
    not invent one.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

# Id 0015 is pre-allocated to this anchor in docs/TASKS.md. Forward-only and idempotent
# by `IF NOT EXISTS` as well as by the `schema_migrations` tracking below, so a re-run is
# always a no-op.
TURN_REASONING_MIGRATION = Migration(
    id="0015_turn_reasoning",
    sql="""
    CREATE TABLE IF NOT EXISTS turn_reasoning (
        id BIGSERIAL PRIMARY KEY,
        turn_id UUID NOT NULL,
        session_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_turn_reasoning_turn_content UNIQUE (turn_id, content_sha256)
    );
    CREATE INDEX IF NOT EXISTS ix_turn_reasoning_session_created
        ON turn_reasoning (session_id, tenant_id, created_at, id);
    """,
)


def _apply_turn_reasoning_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (TURN_REASONING_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(TURN_REASONING_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (TURN_REASONING_MIGRATION.id,),
        )


async def apply_turn_reasoning_migration(app_conninfo: str) -> None:
    """Apply `TURN_REASONING_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the `schema_migrations`
    tracking table this reads. Postgres transactions are sync (D13); the blocking work
    runs in a thread.
    """
    await asyncio.to_thread(_apply_turn_reasoning_migration_sync, app_conninfo)
