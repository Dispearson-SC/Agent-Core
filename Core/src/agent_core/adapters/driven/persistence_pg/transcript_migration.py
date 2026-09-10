"""Migration 0016 - the transcript projection.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-03
Status:  DONE - two views and the indexes the keyset ordering reads
Tests:   Core/tests/integration/test_transcript_repository.py

WHY THIS IS NOT IN migrations.py
    Seven anchors create a table, and `docs/TASKS.md` pre-allocates their ids so that
    `migrations.py` never becomes a file seven anchors write - `docs/WAVES.md` rule 2
    would otherwise serialise all seven in every wave they appear in, over a list. `0016`
    is this anchor's, spent the moment it was written down, following the precedent of
    `PROFILE_SNAPSHOT_MIGRATION`, `HUMAN_REQUESTS_MIGRATION` and `PEER_MESSAGES_MIGRATION`.

    The object is shaped so the owner of `migrations.py` can append it to `APP_MIGRATIONS`
    verbatim if the two ever converge: it is tracked in `schema_migrations` under its own
    id, so doing so is a no-op on a database that already ran it here.

WHY THIS MIGRATION CREATES VIEWS AND NOT A TABLE
    `domain/transcript.py` and `ports/transcript_reader.py` both open with the same
    sentence: NOTHING NEW IS STORED. The transcript is a PROJECTION over rows that already
    exist - messages, audit rows, pending requests, peer traffic, checkpoints. A second
    copy would be a second write path, and the copy is always the one that drifts.

    So `0016` is a projection in the literal sense: two read-only views that give those
    scattered tables one shape, one clock and one ordering key. The adapter above them
    contains no UNION and no per-table knowledge, which is what keeps "add a source" a
    change to this file alone.

THE TENANT COLUMN IS THE WHOLE POINT OF THE FIRST VIEW
    `messages` and `checkpoints` carry a `session_id` and no tenant; `turns` is the only
    table that knows which tenant a session belongs to. Without this view every call site
    would have to remember to join `turns` - and `ports/transcript_reader.py` says
    plainly that the moment tenant scoping depends on a call site remembering, it has
    stopped being a guarantee.

    `session_tenant` resolves ONE tenant per session, `DISTINCT ON (session_id)` ordered
    by the session's first turn. Not `DISTINCT session_id, tenant_id`: if the same
    session id ever appeared under two tenants, that would emit each message twice, once
    under each tenant, and the transcript would leak across the boundary it exists to
    hold. One row per session cannot do that - the worst case becomes a session
    attributed to the wrong tenant and visible to nobody else, which is a bug that hides
    data rather than one that spills it.

ORDERING KEY: (at, entry_id), TOTAL AND STABLE
    Cursor pagination needs a total order, or the cursor is ambiguous exactly where rows
    share a timestamp - and rows here routinely do, because a turn row and its message
    are written in one transaction and `now()` is the transaction's clock.

    `entry_id` is therefore unique across every branch (a per-source prefix) and sorts
    the way a human would expect within a branch (`lpad` to 20, so text comparison agrees
    with numeric comparison and 'message:...10' does not sort before 'message:...9').

SOURCES NOT WIRED YET
    TOOL_RESULT has no table anywhere yet, and REASONING arrives with migration 0015
    (docs/TASKS.md#t-f10-04). Both are absent branches rather than empty ones: a kind with
    no rows behind it simply never appears, and adding the branch later is a `CREATE OR
    REPLACE VIEW` in this file with no adapter change.
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.persistence_pg.human_requests_migration import (
    apply_human_requests_migration,
)
from agent_core.adapters.driven.persistence_pg.migrations import Migration
from agent_core.adapters.driven.persistence_pg.peers_migration import (
    apply_peer_messages_migration,
)

# Id 0016 is pre-allocated to this anchor in docs/TASKS.md. Forward-only: it creates
# views and indexes and never drops or rewrites a table. `CREATE OR REPLACE VIEW` and
# `CREATE INDEX IF NOT EXISTS` make the SQL idempotent on its own, on top of the
# `schema_migrations` tracking below.
#
# The indexes are additive and touch no audit table with DROP or ALTER - see the RULE in
# migrations.py. They exist because the ordering key of every page query is a timestamp
# per source, and a projection that sorts by a column nobody indexed degrades quietly as
# conversations grow, which presents as "the viewer got slow" and traces back to nothing.
TRANSCRIPT_PROJECTION_MIGRATION = Migration(
    id="0016_transcript_projection",
    sql="""
    CREATE INDEX IF NOT EXISTS ix_turns_session_created
        ON turns (session_id, created_at, turn_id);
    CREATE INDEX IF NOT EXISTS ix_turns_tenant_session
        ON turns (tenant_id, session_id);
    CREATE INDEX IF NOT EXISTS ix_messages_session_created
        ON messages (session_id, created_at, id);
    CREATE INDEX IF NOT EXISTS ix_audit_tool_calls_turn_at
        ON audit_tool_calls (turn_id, at);
    CREATE INDEX IF NOT EXISTS ix_audit_human_decisions_turn_at
        ON audit_human_decisions (turn_id, at);
    CREATE INDEX IF NOT EXISTS ix_audit_turn_costs_turn
        ON audit_turn_costs (turn_id);
    CREATE INDEX IF NOT EXISTS ix_checkpoints_session_created
        ON checkpoints (session_id, created_at);

    CREATE OR REPLACE VIEW transcript_entries AS
    WITH session_tenant AS (
        SELECT DISTINCT ON (session_id) session_id, tenant_id
        FROM turns
        ORDER BY session_id, created_at ASC, turn_id ASC
    )
    SELECT
        st.tenant_id                                        AS tenant_id,
        m.session_id                                        AS session_id,
        'message:' || lpad(m.id::text, 20, '0')             AS entry_id,
        owning.turn_id                                      AS turn_id,
        CASE WHEN m.role = 'user' THEN 'user_message'
             ELSE 'agent_message' END                       AS kind,
        m.created_at                                        AS at,
        jsonb_build_object('role', m.role, 'content', m.content)
                                                            AS payload,
        NULL::numeric                                       AS cost_usd
    FROM messages m
    JOIN session_tenant st ON st.session_id = m.session_id
    LEFT JOIN LATERAL (
        SELECT t.turn_id
        FROM turns t
        WHERE t.session_id = m.session_id AND t.created_at <= m.created_at
        ORDER BY t.created_at DESC, t.turn_id DESC
        LIMIT 1
    ) owning ON TRUE

    UNION ALL
    SELECT
        t.tenant_id,
        t.session_id,
        'tool_call:' || lpad(a.id::text, 20, '0'),
        a.turn_id,
        CASE WHEN a.effect = 'deny' THEN 'policy_denial' ELSE 'tool_call' END,
        a.at,
        jsonb_build_object(
            'tool', a.tool, 'caller', a.caller, 'arguments', a.arguments,
            'effect', a.effect, 'rule_id', a.rule_id
        ),
        NULL::numeric
    FROM audit_tool_calls a
    JOIN turns t ON t.turn_id = a.turn_id

    UNION ALL
    SELECT
        t.tenant_id,
        t.session_id,
        'human_decision:' || lpad(d.id::text, 20, '0'),
        d.turn_id,
        'human_decision',
        d.at,
        jsonb_build_object(
            'tool_call_id', d.tool_call_id, 'subject_id', d.subject_id,
            'approved', d.approved, 'note', d.note
        ),
        NULL::numeric
    FROM audit_human_decisions d
    JOIN turns t ON t.turn_id = d.turn_id

    UNION ALL
    SELECT
        t.tenant_id,
        t.session_id,
        'pending_request:' || hr.correlation_id,
        hr.turn_id,
        'pending_request',
        hr.created_at,
        jsonb_build_object(
            'tool_call_id', hr.tool_call_id, 'kind', hr.kind,
            'expires_at', hr.expires_at, 'answered_at', hr.answered_at
        ),
        NULL::numeric
    FROM human_requests hr
    JOIN turns t ON t.turn_id = hr.turn_id

    UNION ALL
    SELECT
        p.from_tenant_id,
        p.from_session_id,
        'peer_exchange:' || p.correlation_id,
        p.turn_id,
        'peer_exchange',
        p.enqueued_at,
        jsonb_build_object(
            'target_agent_id', p.target_agent_id, 'hop', p.hop, 'state', p.state,
            'question', p.question, 'answer', p.answer
        ),
        NULL::numeric
    FROM peer_messages p

    UNION ALL
    SELECT
        st.tenant_id,
        c.session_id,
        'compaction:' || c.checkpoint_id::text,
        NULL::uuid,
        'compaction',
        c.created_at,
        jsonb_build_object(
            'summary', c.summary,
            'covers_through_message', c.covers_through_message,
            'supersedes', c.supersedes
        ),
        NULL::numeric
    FROM checkpoints c
    JOIN session_tenant st ON st.session_id = c.session_id;

    CREATE OR REPLACE VIEW transcript_conversations AS
    SELECT
        s.tenant_id                                             AS tenant_id,
        s.session_id                                            AS session_id,
        s.profile_id                                            AS profile_id,
        GREATEST(s.last_turn_at, COALESCE(msg.last_at, s.last_turn_at))
                                                                AS last_activity_at,
        COALESCE(msg.message_count, 0)                          AS message_count,
        (pend.waiting_since IS NOT NULL)                        AS is_suspended,
        pend.waiting_since                                      AS waiting_since,
        COALESCE(spend.total_cost_usd, 0)::numeric              AS total_cost_usd
    FROM (
        SELECT DISTINCT ON (session_id)
            session_id, tenant_id, profile_id, created_at AS last_turn_at
        FROM turns
        ORDER BY session_id, created_at DESC, turn_id DESC
    ) s
    LEFT JOIN LATERAL (
        SELECT count(*) AS message_count, max(m.created_at) AS last_at
        FROM messages m
        WHERE m.session_id = s.session_id
    ) msg ON TRUE
    LEFT JOIN LATERAL (
        SELECT min(hr.created_at) AS waiting_since
        FROM human_requests hr
        JOIN turns t ON t.turn_id = hr.turn_id
        WHERE t.session_id = s.session_id AND hr.answered_at IS NULL
    ) pend ON TRUE
    LEFT JOIN LATERAL (
        SELECT sum(c.cost_usd) AS total_cost_usd
        FROM audit_turn_costs c
        JOIN turns t ON t.turn_id = c.turn_id
        WHERE t.session_id = s.session_id
    ) spend ON TRUE;
    """,
)

# `transcript_entries` reads `human_requests` (0010) and `peer_messages` (0014), so those
# tables have to exist before `CREATE VIEW` can name them. Declared here as an ordered
# prerequisite rather than left to whoever wires composition: a view over a missing table
# fails at migration time, and "apply these three in this order" is exactly the kind of
# instruction that survives in code and evaporates in prose.
#
# Applying them is a no-op when they are already applied - both are tracked under their
# own id in `schema_migrations` - so this costs nothing and cannot double-apply.
_PREREQUISITE_MIGRATIONS = (apply_human_requests_migration, apply_peer_messages_migration)


def _apply_transcript_projection_migration_sync(app_conninfo: str) -> None:
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = %s",
            (TRANSCRIPT_PROJECTION_MIGRATION.id,),
        ).fetchone()
        if applied is not None:
            return
        conn.execute(TRANSCRIPT_PROJECTION_MIGRATION.sql)
        conn.execute(
            "INSERT INTO schema_migrations (id) VALUES (%s)",
            (TRANSCRIPT_PROJECTION_MIGRATION.id,),
        )


async def apply_transcript_projection_migration(app_conninfo: str) -> None:
    """Apply `TRANSCRIPT_PROJECTION_MIGRATION`, once, after `migrations.run_migrations`.

    Runs after `run_migrations` because that is what creates the base tables and the
    `schema_migrations` tracking table this reads, and after its two prerequisites
    because a view cannot name a table that does not exist yet. Idempotent by that
    tracking and by the SQL itself, and forward-only: it creates views and indexes and
    never drops or rewrites a table.

    Postgres transactions are sync (D13); the blocking work runs in a thread.
    """
    for apply_prerequisite in _PREREQUISITE_MIGRATIONS:
        await apply_prerequisite(app_conninfo)
    await asyncio.to_thread(_apply_transcript_projection_migration_sync, app_conninfo)
