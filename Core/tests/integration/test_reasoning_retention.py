"""Integration tests for D27's reasoning retention sweep and the index it needs.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-10
Decision: docs/DECISIONS.md#d27
Covers:  adapters/driven/persistence_pg/reasoning_migration.py

WHAT IS BEING DEFENDED

    D27 sets a 30-day default window: `turn_reasoning` rows older than the window are
    deleted by a scheduled sweep, rows inside it are not. D27 also records a defect in
    this module's own docstring: it claimed `created_at` was indexed for a range-delete
    sweep, but the only index that existed, `ix_turn_reasoning_session_created
    (session_id, tenant_id, created_at, id)`, has `created_at` as its THIRD column - a
    sweep that only knows an age cutoff (no `session_id`) cannot use it as a range scan,
    so a "range delete" against that index is in fact a sequential scan of the whole
    table.

    Two things are asserted, and neither is safe to skip:

      DELETION IS CORRECT  - a stale block is gone and a fresh block survives the same
                              sweep call. Getting this backwards silently destroys
                              evidence an operator (t-f10-06) still needed, or keeps a
                              liability D27 exists to bound.
      THE INDEX IS USED    - proven with `EXPLAIN (FORMAT JSON)` against the sweep's own
                              SQL, not by asserting an index merely exists on the table.
                              The old `ix_turn_reasoning_session_created` also "exists"
                              and would pass a mere-existence check while still being
                              useless for this query - that is exactly the defect D27
                              recorded.

    Reasoning is admin-only (t-f10-04). The sweep deletes by `id`; it is asserted here to
    never `SELECT content`, so it cannot become a read path into an admin-only table.

These need a real Postgres and skip cleanly without one, the same pattern as
test_reasoning_persistence.py and test_conversation_repository.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations, reasoning_migration
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.adapters.driven.persistence_pg.reasoning_migration import (
    _SWEEP_BATCH_SQL,
    sweep_expired_reasoning,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo(app_db: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)


def _migrated_conninfo() -> str:
    """A migrated app database for this module, including migration 0020. Idempotent."""
    app_db = "agent_core_reasoning_retention_test"
    dbos_db = "agent_core_reasoning_retention_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(reasoning_migration.apply_turn_reasoning_migration(app_conninfo))
    asyncio.run(reasoning_migration.apply_turn_reasoning_retention_index_migration(app_conninfo))
    return app_conninfo


def _session(tenant: str = "t-1") -> SessionRef:
    return SessionRef(session_id=SessionId(f"s-{uuid.uuid4()}"), tenant_id=TenantId(tenant))


def _backdate(conninfo: str, turn_id: TurnId, created_at: datetime) -> None:
    """Force a stored block's `created_at` into the past.

    `append_reasoning` always stamps `now()`; a real retention test needs a block that
    is genuinely older than the window, not one merely inserted a moment before the
    sweep runs.
    """
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE turn_reasoning SET created_at = %s WHERE turn_id = %s",
            (created_at, str(turn_id)),
        )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_sweep_deletes_blocks_older_than_d27s_window_and_keeps_newer_ones() -> None:
    conninfo = _migrated_conninfo()
    store = PgConversationStore(conninfo)
    session = _session()
    stale_turn = TurnId(str(uuid.uuid4()))
    fresh_turn = TurnId(str(uuid.uuid4()))
    stale_content = f"stale speculation about the caller [{uuid.uuid4()}]"
    fresh_content = f"fresh speculation about the caller [{uuid.uuid4()}]"

    asyncio.run(store.append_reasoning(stale_turn, session, stale_content))
    asyncio.run(store.append_reasoning(fresh_turn, session, fresh_content))

    now = datetime.now(UTC)
    _backdate(conninfo, stale_turn, now - timedelta(days=31))
    _backdate(conninfo, fresh_turn, now - timedelta(days=1))

    asyncio.run(sweep_expired_reasoning(conninfo, window=timedelta(days=30)))

    with psycopg.connect(conninfo) as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT content FROM turn_reasoning WHERE session_id = %s",
                (session.session_id,),
            ).fetchall()
        }

    assert stale_content not in remaining, (
        "a reasoning block older than D27's 30-day window survived the sweep - it is "
        "exactly the liability D27 exists to bound"
    )
    assert fresh_content in remaining, (
        "the sweep deleted a block still inside D27's 30-day window - the operator "
        "endpoint (t-f10-06) this window exists for just lost evidence it was promised"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_sweep_query_uses_the_retention_index_not_a_table_scan() -> None:
    """`EXPLAIN`, not existence. The old session-first index also exists and cannot
    serve this query - a mere-existence check would pass on the defect D27 recorded.
    """
    conninfo = _migrated_conninfo()
    cutoff = datetime.now(UTC) - timedelta(days=30)

    with psycopg.connect(conninfo) as conn:
        conn.execute("SET enable_seqscan = off")
        plan_rows = conn.execute(
            f"EXPLAIN (FORMAT JSON) {_SWEEP_BATCH_SQL}", (cutoff, 500)
        ).fetchall()
    plan_text = json.dumps(plan_rows[0][0])

    assert "Index Scan" in plan_text, (
        "the sweep query could not be satisfied by any index with seq scan disabled - "
        "it is a table scan"
    )
    assert "ix_turn_reasoning_created_at" in plan_text, (
        "the sweep did not use ix_turn_reasoning_created_at - the session-first "
        "composite index cannot serve a global sweep by age, which is the exact defect "
        "D27 recorded against this module's old docstring claim"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_sweep_query_never_selects_content() -> None:
    """Reasoning is admin-only (t-f10-04). A sweep that reads `content` to decide what
    to delete is a read path into a table nothing but the admin projection may see.
    """
    assert "content" not in _SWEEP_BATCH_SQL.lower(), (
        "the sweep's own SQL names the `content` column - an admin-only table must "
        "never be read by a maintenance job that only needs row identity"
    )
