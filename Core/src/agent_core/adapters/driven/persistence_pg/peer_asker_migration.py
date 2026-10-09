"""Migration 0025 - who asked, recorded on the ask itself.

Phase:   F12 - Agent-to-agent orchestration
Tasks:   docs/TASKS.md#t-f11-48
Status:  DONE - one nullable column, no backfill
Tests:   Core/tests/integration/test_peer_asker_identity.py

WHAT WAS MISSING, AND WHY NOTHING ELSE SUBSTITUTES FOR IT
    `0014_peer_messages` gave the queue the target, the asking session, the turn, the hop
    count and the question. Everything the answer needs to find its way home, and nothing
    that says WHO ASKED - so `PeerAsk` reached the answering agent with no caller identity
    and `hop_limit.authorise_hop`'s callee-side check, `callee_policy.may_ask(caller)`,
    could not be re-run on arrival. The answering side took the question on trust because
    the asking side said it was allowed.

    The asking side's gate is real and still applies, so this was never an open door. It
    is the SECOND lock on a two-sided allowlist, and a two-sided check enforced on one
    side is a one-sided check with extra words. CLAUDE.md non-negotiable #10 is the reason
    to want the second: a peer that was itself misled is a confused deputy, and what it
    sends arrives with friendly provenance.

    `from_session_id` is not the missing column wearing a different name. A session
    identifies a conversation, not the profile the asking turn ran under, and deriving one
    from the other would be an identity this table never recorded - see
    `adapters/driving/peers/worker.py`, which says the same thing about inventing an id
    from the session.

WHY THIS IS NOT IN migrations.py
    Every anchor that creates or alters a table defines its own `Migration` in its own
    module, so `migrations.py` is never a file several anchors write and the wave
    scheduler never has to serialise them over a shared list. `docs/TASKS.md`
    pre-allocates the ids; `0025` is this one's, spent the moment it was written down
    there. Discovery is automatic - `migrations.discover_app_migrations()` imports every
    module of this package and applies what it finds in id order, so `0025` lands after
    the `0014` table it alters with no registration line for anyone to forget.

NULLABLE, WITH NO DEFAULT AND NO BACKFILL
    Rows enqueued before this column existed have no asker, and that is a true statement
    about them. A `NOT NULL DEFAULT ...` would make every historical ask claim an identity
    nobody asserted, and a backfilled identity in an audit-adjacent table is a claim nobody
    made - the same reasoning `audit_reason_migration.py` gives for leaving its own column
    NULL, and worse here, because this one is read by a security check.

    So NULL means UNKNOWN: the asking identity did not travel with that row. It never
    means "anyone may ask". The answering side reads it as `PeerAsk.asker is None` and has
    no caller to re-check; `mailbox.py` says what that obliges it to do.

    Old rows stay DELIVERABLE. An enqueued ask is a turn suspended on `DBOS.recv()`, and a
    migration that made those rows unclaimable would strand every conversation already in
    flight when it was applied - a data-loss bug dressed as a security fix.

FORWARD-ONLY AND IDEMPOTENT
    ADD COLUMN IF NOT EXISTS, nothing else. No DROP, no rewrite, no UPDATE: append-only,
    for the same reason the audit tables are. A re-run is a no-op by `IF NOT EXISTS` as
    well as by the `schema_migrations` tracking.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

PEER_ASKER_MIGRATION = Migration(
    id="0025_peer_messages_from_agent_id",
    sql="""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS from_agent_id TEXT;
    """,
)


def _apply_peer_asker_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (PEER_ASKER_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_ASKER_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_ASKER_MIGRATION.id,))


async def apply_peer_asker_migration(app_conninfo: str) -> None:
    """Apply `PEER_ASKER_MIGRATION`, once, after `peers_migration` created the table.

    Production does not call this: `migrations.apply_all_migrations` applies every
    discovered `Migration` in id order on one connection, which is the only way the order
    of the set can be right - and order matters here, because this ALTERs a table another
    migration creates. This function exists for the same reason every sibling's does: a
    test that needs exactly this one against exactly one database.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_peer_asker_migration_sync, app_conninfo)
