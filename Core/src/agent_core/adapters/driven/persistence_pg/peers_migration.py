"""Migration 0014 - the peer mailbox queue.

Phase:   F9 - Agent-to-agent foundations
Tasks:   docs/TASKS.md#t-f9-03
Status:  DONE - table, its uniqueness constraint and the queued-work index
Tests:   Core/tests/integration/test_peer_mailbox.py

WHY THIS IS NOT IN migrations.py
    Seven anchors create a table, and `docs/TASKS.md` pre-allocates their ids so that
    `migrations.py` never becomes a file seven anchors write - `docs/WAVES.md` rule 2
    would otherwise serialise all seven in every wave they appear in, over a list. `0014`
    is this anchor's, spent the moment it was written down, following the precedent of
    `PROFILE_SNAPSHOT_MIGRATION` and `HUMAN_REQUESTS_MIGRATION`.

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on a database that already ran it here.

THE TABLE IS THE QUEUE, AND THE QUEUE IS WHY THE TABLE EXISTS
    peer_messages (correlation_id pk, target_agent_id, from_session_id, from_tenant_id,
                   turn_id, hop, question, state, answer, enqueued_at, delivered_at,
                   answered_at)

    An in-memory queue loses every pending ask on the next deploy, and a lost ask is a
    turn suspended forever: the asking agent is waiting on `DBOS.recv()` for an answer
    nobody will ever send. Durability is not an optimisation here, it is the difference
    between a slow answer and a conversation that never resumes.

WHY THE UNIQUE CONSTRAINT IS PART OF EXACTLY-ONCE
    `AgentMailbox.ask` runs inside a DBOS step and a step is re-executed after a crash. A
    second row for the same (turn_id, target, question) is a second ask, which the peer
    answers a second time - and the asking turn, waiting on one correlation id, resumes
    with whichever answer raced in first while the other is orphaned.

    The rule lives here as a constraint rather than in the adapter as SELECT-then-INSERT,
    because a check in application code is a race: two step attempts can both read "not
    enqueued" before either writes. The database is the only place the question is
    settled once. This is the same reasoning as `uq_human_requests_turn_tool` and it is
    the same defect class.

WHY `state` IS A COLUMN AND NOT A DELETE
    `pending_inputs` drains with `DELETE ... RETURNING` because nothing ever comes back
    for those rows. A peer ask does: the answer arrives later and has to find the row it
    belongs to. So the row survives delivery and moves through
    queued -> delivered -> answered, and the partial index keeps the claim query reading
    only the queued tail rather than the whole history.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

# Id 0014 is pre-allocated to this anchor in docs/TASKS.md. Forward-only and idempotent
# by `IF NOT EXISTS` as well as by the `schema_migrations` tracking below, so a re-run is
# always a no-op.
PEER_MESSAGES_MIGRATION = Migration(
    id="0014_peer_messages",
    sql="""
    CREATE TABLE IF NOT EXISTS peer_messages (
        correlation_id TEXT PRIMARY KEY,
        target_agent_id TEXT NOT NULL,
        from_session_id TEXT NOT NULL,
        from_tenant_id TEXT NOT NULL,
        turn_id UUID NOT NULL,
        hop INTEGER NOT NULL,
        question TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        answer TEXT,
        enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        answered_at TIMESTAMPTZ,
        CONSTRAINT uq_peer_messages_turn_target_question
            UNIQUE (turn_id, target_agent_id, question)
    );
    CREATE INDEX IF NOT EXISTS ix_peer_messages_queued
        ON peer_messages (target_agent_id, enqueued_at, correlation_id)
        WHERE state = 'queued';
    """,
)


def _apply_peer_messages_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (PEER_MESSAGES_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_MESSAGES_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_MESSAGES_MIGRATION.id,)
        )


async def apply_peer_messages_migration(app_conninfo: str) -> None:
    """Apply `PEER_MESSAGES_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the `schema_migrations`
    tracking table this reads. Postgres transactions are sync (D13); the blocking work
    runs in a thread.
    """
    await asyncio.to_thread(_apply_peer_messages_migration_sync, app_conninfo)
