"""Migration 0039: the peer answer cache (C25, D57).

Phase:   Agent-comms v2, C25
Tests:   Core/tests/contract/test_answer_cache.py, Core/tests/integration/test_migrations.py

One row per remembered answer, keyed by a SHA-256 digest of (tenant, target, audience, normalised
question, version). The question itself is deliberately NOT stored: the digest is all the lookup
needs, and a second copy of what customers asked would sit outside the transcript's retention.
`tenant_id` and `target` are plain columns because invalidation is "everything for this tenant" or
"everything for this tenant's target". `expires_at` is set by the database's clock. Additive; the
table stays empty unless a deployment opts in.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.migrations import Migration, apply_migration

PEER_ANSWER_CACHE_MIGRATION = Migration(
    id="0039_peer_answer_cache",
    sql="""
    CREATE TABLE IF NOT EXISTS peer_answer_cache (
        key_digest TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        target     TEXT NOT NULL,
        answer     TEXT NOT NULL,
        typed      BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_peer_answer_cache_tenant
        ON peer_answer_cache (tenant_id, target);
    CREATE INDEX IF NOT EXISTS ix_peer_answer_cache_expiry ON peer_answer_cache (expires_at);
    """,
)


async def apply_peer_answer_cache_migration(app_conninfo: str) -> None:
    """Apply `PEER_ANSWER_CACHE_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await apply_migration(app_conninfo, PEER_ANSWER_CACHE_MIGRATION)
