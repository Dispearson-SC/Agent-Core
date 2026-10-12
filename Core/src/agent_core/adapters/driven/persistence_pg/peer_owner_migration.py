"""Migration 0035: the owner chain of a peer's approval request (C16, D46).

Phase:   Agent-comms v2, C16
Tests:   Core/tests/contract/test_mailbox_conformance.py,
         Core/tests/integration/test_peer_owner_visibility.py,
         Core/tests/integration/test_migrations.py

A specialist that suspends on a person used to be invisible to the conversation the person was in:
its request was filed under the specialist's own session, which no inbox lists. Now:

* `peer_messages.root_turn_id` - the turn a person started that the ask hangs from (the asking
  turn itself for a direct ask; the root the asking callee was started under for a nested one).
  NULL for rows of older builds, which read as "the asking turn".
* `human_requests.root_turn_id / asker_turn_id / asker_agent_id / ask_correlation_id` - the same
  chain, written when the callee's request is published. All NULL for an ordinary request.
* `transcript_entries` lists such a request under the ROOT turn's conversation (with the chain in
  the payload; a user still sees only the placeholder, #11), and `transcript_conversations` counts
  it when it decides whether that conversation is suspended.

The views are re-created from the definitions 0016/0029 use, with the one branch each that changes
replaced. The replacement is asserted at import: a definition that moved under this module would
otherwise silently keep the old behaviour.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.adapters.driven.persistence_pg.transcript_migration import (
    TRANSCRIPT_CONVERSATIONS_VIEW_SQL,
    TRANSCRIPT_ENTRIES_VIEW_SQL,
)

_OLD_PENDING_BRANCH = """
    SELECT
        t.tenant_id,
        t.session_id,
        'pending_request:' || hr.correlation_id,
        hr.turn_id::text,
        'pending_request',
        hr.created_at,
        jsonb_build_object(
            'tool_call_id', hr.tool_call_id, 'kind', hr.kind,
            'expires_at', hr.expires_at, 'answered_at', hr.answered_at
        ),
        NULL::numeric
    FROM human_requests hr
    JOIN turns t ON t.turn_id = hr.turn_id
"""

_NEW_PENDING_BRANCH = """
    SELECT
        COALESCE(rt.tenant_id, t.tenant_id),
        COALESCE(rt.session_id, t.session_id),
        'pending_request:' || hr.correlation_id,
        hr.turn_id::text,
        'pending_request',
        hr.created_at,
        jsonb_build_object(
            'tool_call_id', hr.tool_call_id, 'kind', hr.kind,
            'expires_at', hr.expires_at, 'answered_at', hr.answered_at
        ) || CASE WHEN hr.root_turn_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object(
            'owner', jsonb_build_object(
                'root_turn_id', hr.root_turn_id, 'asker_turn_id', hr.asker_turn_id,
                'asker', hr.asker_agent_id, 'call', hr.ask_correlation_id
            )
        ) END,
        NULL::numeric
    FROM human_requests hr
    JOIN turns t ON t.turn_id = hr.turn_id
    LEFT JOIN turns rt ON hr.root_turn_id IS NOT NULL AND rt.turn_id::text = hr.root_turn_id
"""

_OLD_WAITING = """
        SELECT min(hr.created_at) AS waiting_since
        FROM human_requests hr
        JOIN turns t ON t.turn_id = hr.turn_id
        WHERE t.session_id = s.session_id AND hr.answered_at IS NULL
"""

_NEW_WAITING = """
        SELECT min(hr.created_at) AS waiting_since
        FROM human_requests hr
        JOIN turns t ON t.turn_id = hr.turn_id
        LEFT JOIN turns rt ON hr.root_turn_id IS NOT NULL AND rt.turn_id::text = hr.root_turn_id
        WHERE COALESCE(rt.session_id, t.session_id) = s.session_id AND hr.answered_at IS NULL
"""


def _replaced(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError("a view definition moved: migration 0035 cannot patch it")
    return text.replace(old, new)


PEER_OWNER_MIGRATION = Migration(
    id="0035_peer_owner_chain",
    sql=f"""
    ALTER TABLE peer_messages ADD COLUMN IF NOT EXISTS root_turn_id TEXT;
    ALTER TABLE human_requests
        ADD COLUMN IF NOT EXISTS root_turn_id TEXT,
        ADD COLUMN IF NOT EXISTS asker_turn_id TEXT,
        ADD COLUMN IF NOT EXISTS asker_agent_id TEXT,
        ADD COLUMN IF NOT EXISTS ask_correlation_id TEXT;
    CREATE INDEX IF NOT EXISTS ix_human_requests_root_turn
        ON human_requests (root_turn_id) WHERE root_turn_id IS NOT NULL;
    {_replaced(TRANSCRIPT_ENTRIES_VIEW_SQL, _OLD_PENDING_BRANCH, _NEW_PENDING_BRANCH)};
    {_replaced(TRANSCRIPT_CONVERSATIONS_VIEW_SQL, _OLD_WAITING, _NEW_WAITING)};
    """,
)


def _apply_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s", (PEER_OWNER_MIGRATION.id,)
        ).fetchone()
        if applied is not None:
            return
        conn.execute(PEER_OWNER_MIGRATION.sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (PEER_OWNER_MIGRATION.id,))


async def apply_peer_owner_migration(app_conninfo: str) -> None:
    """Apply `PEER_OWNER_MIGRATION` once (tests; production uses `apply_all_migrations`)."""
    await asyncio.to_thread(_apply_sync, app_conninfo)
