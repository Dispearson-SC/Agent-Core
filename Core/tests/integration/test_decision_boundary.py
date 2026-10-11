"""C1b: the decision route's boundary check against the real `human_requests` table.

A decision may only be signalled for a (turn, tool call) that was published to a person and
is still live. A delegation row, an expired row and a call nobody published are refused
before the audit row and before the workflow is touched.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import psycopg
import pytest

from agent_core import composition
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.domain.turn import PendingKind, ToolCallId, TurnId

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


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_only_a_live_human_request_of_the_turn_passes_the_boundary_check() -> None:
    app_db = "agent_core_decision_boundary_test"
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO, app_database=app_db, dbos_database=f"{app_db}_dbos"
        )
    )
    conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(conninfo))
    turn = str(uuid.uuid4())
    other_turn = str(uuid.uuid4())
    rows = (
        ("c-approval", turn, "tc-approval", PendingKind.APPROVAL.value, "7 days"),
        ("c-evidence", turn, "tc-evidence", PendingKind.EVIDENCE.value, "7 days"),
        ("c-delegation", turn, "tc-peer", PendingKind.DELEGATION.value, "7 days"),
        ("c-expired", turn, "tc-expired", PendingKind.APPROVAL.value, "-1 hour"),
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        for corr, turn_id, call, kind, ttl in rows:
            conn.execute(
                "INSERT INTO human_requests (correlation_id, turn_id, tool_call_id, kind, "
                "expires_at) VALUES (%s, %s, %s, %s, now() + %s::interval) "
                "ON CONFLICT DO NOTHING",
                (corr + turn, turn_id, call, kind, ttl),
            )

    pending = composition._pg_pending_human_request(conninfo)

    def check(turn_id: str, call: str) -> bool:
        return asyncio.run(pending(TurnId(turn_id), ToolCallId(call)))

    assert check(turn, "tc-approval")
    assert check(turn, "tc-evidence")
    assert not check(turn, "tc-peer"), "a delegation is never decided by a person"
    assert not check(turn, "tc-expired")
    assert not check(turn, "tc-never-published")
    assert not check(other_turn, "tc-approval"), "another turn's call is not this turn's"
