"""Migration 0013 - the knowledge document table.

Phase:   F8 - Knowledge: Skills and FULL_TEXT
Tasks:   docs/TASKS.md#t-f8-04
Status:  DONE - table, its tenant-leading index and its full-text index
Tests:   Core/tests/integration/test_knowledge_repository.py

WHY THIS IS NOT IN migrations.py
    Seven anchors still to come each create a table. If every one reached for the next
    free id in `migrations.py`, that module would be a file seven anchors write, and the
    scheduler would have to serialise all seven in every wave they appear in - over a
    list. `docs/TASKS.md` pre-allocates the ids instead, and each anchor defines its own
    `Migration` in its own module, following `PROFILE_SNAPSHOT_MIGRATION`. `0013` is this
    one's, spent the moment it was written down.

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on a database that already ran it through
    `apply_knowledge_migration`.

`tenant_id` IS NOT NULL, AND THAT IS THE WHOLE SECURITY DESIGN OF THIS TABLE
    `ports/knowledge_base.py` requires the tenant to be filtered IN THE QUERY, and
    `TenantKnowledgePolicy` guarantees the adapter is always handed one. A nullable column
    would reintroduce the hole from the other side: a row with no tenant matches no
    `tenant_id = %s` predicate, so it would be invisible to retrieval and yet sit in the
    corpus looking like data. Either the row belongs to a business or it does not exist.

    It leads the primary key and the collection index for the same reason it leads every
    WHERE clause: it is the first thing every query narrows by. A `pricing` collection
    exists in every tenant, so `(tenant_id, collection)` - never `collection` alone - is
    what identifies a corpus.

VERSIONING IS THE POINT, NOT AN EXTRA
    `effective_from` plus `superseded_by` answer "why did the agent quote the old price on
    Tuesday?". A future `effective_from` is a SCHEDULED change and needs no scheduler,
    because retrieval filters on it. A superseded row is kept: a hard delete destroys the
    record of what the agent used to tell customers, which is the one thing an audit needs.

THE FULL-TEXT INDEX EXPRESSION MUST MATCH THE QUERY EXACTLY
    `repository.py` builds its `tsvector` as `to_tsvector('english', title || ' ' || body)`
    and this index is declared over those exact bytes. An index over a different
    expression - a different regconfig, a different concatenation - is not merely slower,
    it is never used, and nothing reports that. The regconfig is written as a literal
    rather than taken from `default_text_search_config` because an expression index needs
    an IMMUTABLE expression, and the one-argument form is not one.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration

KNOWLEDGE_MIGRATION = Migration(
    id="0013_knowledge_docs",
    sql="""
    CREATE TABLE IF NOT EXISTS knowledge_docs (
        tenant_id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        collection TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
        superseded_by TEXT,
        updated_by TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, doc_id)
    );
    CREATE INDEX IF NOT EXISTS ix_knowledge_docs_tenant_collection
        ON knowledge_docs (tenant_id, collection, effective_from DESC);
    CREATE INDEX IF NOT EXISTS ix_knowledge_docs_fts
        ON knowledge_docs
        USING GIN (to_tsvector('english', title || ' ' || body));
    """,
)


def _apply_knowledge_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (KNOWLEDGE_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(KNOWLEDGE_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (KNOWLEDGE_MIGRATION.id,))


async def apply_knowledge_migration(app_conninfo: str) -> None:
    """Apply `KNOWLEDGE_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the `schema_migrations`
    tracking table this reads. Idempotent by that tracking and by `IF NOT EXISTS`, and
    forward-only: it creates a table and never drops or rewrites one.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    await asyncio.to_thread(_apply_knowledge_migration_sync, app_conninfo)
