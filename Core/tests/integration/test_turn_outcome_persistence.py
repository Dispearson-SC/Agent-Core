"""Integration tests for `PgConversationStore.append_outcome`.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-22
Covers:  adapters/driven/persistence_pg/conversation_repository.py

Needs a real Postgres and is skipped when one is not reachable - same pattern as
test_conversation_repository.py. Uses its OWN app database (`agent_core_turn_outcome_test`)
so it never collides with the other integration tests running concurrently against the
same Postgres instance.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
    apply_turn_outcome_migration,
)
from agent_core.domain.turn import (
    PendingKind,
    PendingRequest,
    TurnOutcome,
    TurnResult,
    Usage,
)

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


def _insert_bare_turn(app_conninfo: str, turn_id: str) -> None:
    """The row `append_request` would have already left behind.

    `append_outcome` only ever UPDATEs an existing turn row - it never creates one - so
    this recreates exactly that precondition. It used to skip D20's `profile_version` and
    `profile_snapshot` as "a different anchor's concern"; migration 0009 makes both NOT
    NULL, so that only worked against a scratch database created before 0009 existed. On a
    database migrated from empty - CI, and every fresh clone - the INSERT failed."""
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, "
            "profile_version, profile_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (turn_id, f"s-{uuid.uuid4()}", "t-1", "p-1", "started", 1, "{}"),
        )


def _fetch_turn_row(app_conninfo: str, turn_id: str) -> tuple[Any, ...]:
    with psycopg.connect(app_conninfo) as conn:
        row = conn.execute(
            "SELECT state, pending, result, finished_at FROM turns WHERE turn_id = %s",
            (turn_id,),
        ).fetchone()
    assert row is not None, f"no turns row for {turn_id}"
    return row


def _count_turn_rows(app_conninfo: str, turn_id: str) -> int:
    with psycopg.connect(app_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM turns WHERE turn_id = %s", (turn_id,)).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_append_outcome_persists_suspended_and_finished_state_and_is_idempotent() -> None:
    app_db = "agent_core_turn_outcome_test"
    dbos_db = "agent_core_turn_outcome_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(apply_turn_outcome_migration(app_conninfo))

    store = PgConversationStore(app_conninfo)

    # --- SUSPENDED: a tool is waiting on a human. The pending request itself - not just
    # the fact that the turn suspended - must survive, because a suspended turn that
    # loses it can never be resumed (ports/conversation_store.py's own docstring).
    suspended_turn_id = str(uuid.uuid4())
    _insert_bare_turn(app_conninfo, suspended_turn_id)
    pending = (
        PendingRequest(
            kind=PendingKind.APPROVAL,
            tool_call_id="call-1",  # type: ignore[arg-type]
            tool_name="terminal",
            arguments={"command": "rm -rf /tmp/x"},
            reason="destructive command needs a human yes",
        ),
    )
    suspended_outcome = TurnOutcome(
        turn_id=suspended_turn_id,  # type: ignore[arg-type]
        pending=pending,
    )
    asyncio.run(store.append_outcome(suspended_turn_id, suspended_outcome))  # type: ignore[arg-type]

    state, pending_json, result_json, finished_at = _fetch_turn_row(
        app_conninfo, suspended_turn_id
    )
    assert state == "suspended"
    assert result_json is None
    assert finished_at is None
    assert pending_json == [
        {
            "kind": "approval",
            "tool_call_id": "call-1",
            "tool_name": "terminal",
            "arguments": {"command": "rm -rf /tmp/x"},
            "reason": "destructive command needs a human yes",
        }
    ]

    # Appending the SAME outcome again for the SAME turn_id must not create a second row.
    asyncio.run(store.append_outcome(suspended_turn_id, suspended_outcome))  # type: ignore[arg-type]
    assert _count_turn_rows(app_conninfo, suspended_turn_id) == 1

    # --- FINISHED: the model produced an answer. `usage` must round-trip too, since
    # StartTurn step 7 reads `outcome.result.usage` straight back off what was just
    # appended, to feed the audit sink's `record_turn_end`.
    finished_turn_id = str(uuid.uuid4())
    _insert_bare_turn(app_conninfo, finished_turn_id)
    finished_at_value = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    finished_outcome = TurnOutcome(
        turn_id=finished_turn_id,  # type: ignore[arg-type]
        result=TurnResult(
            text="done",
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.002")),
            finished_at=finished_at_value,
        ),
    )
    asyncio.run(store.append_outcome(finished_turn_id, finished_outcome))  # type: ignore[arg-type]

    state, pending_json, result_json, finished_at = _fetch_turn_row(app_conninfo, finished_turn_id)
    assert state == "finished"
    assert pending_json == []
    assert result_json is not None
    assert result_json["text"] == "done"
    assert result_json["usage"]["input_tokens"] == 10
    assert result_json["usage"]["cost_usd"] == "0.002"
    assert finished_at == finished_at_value

    # Idempotent for FINISHED too, not just SUSPENDED.
    asyncio.run(store.append_outcome(finished_turn_id, finished_outcome))  # type: ignore[arg-type]
    assert _count_turn_rows(app_conninfo, finished_turn_id) == 1
