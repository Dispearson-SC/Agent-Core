"""Integration tests for the Postgres pending-input buffer.

Phase:   F2 (durability)
Tasks:   docs/TASKS.md#t-f2-04
Covers:  adapters/driven/persistence_pg/pending_input_repository.py

WHAT IS BEING DEFENDED (D19)
    Messages arriving inside the coalescing window land in a buffer, and the turn
    workflow's first step (t-f2-10, a later anchor - out of this file's scope) drains it
    into one turn. Two properties matter here:

    - drain returns the buffered messages in the order they ARRIVED, never resorted by
      anything else;
    - draining is transactional: a crash between reading the buffer and clearing it
      must neither lose a message (crash before commit) nor hand the same message out
      twice (a second drain after a real commit).

    Needs a real Postgres and is skipped when one is not reachable - same pattern as
    test_conversation_repository.py.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.pending_input_repository import (
    _DRAIN_SQL,
    PgPendingInputBuffer,
    apply_pending_input_migration,
)
from agent_core.domain.turn import SessionRef, UserInput

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


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_drain_returns_arrival_order_and_is_transactional_no_loss_no_duplicate() -> None:
    app_db = "agent_core_pending_input_test"
    dbos_db = "agent_core_pending_input_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(apply_pending_input_migration(app_conninfo))

    session = SessionRef(session_id=f"s-{uuid.uuid4()}", tenant_id="t-1")  # type: ignore[arg-type]
    other_session = SessionRef(session_id=f"s-{uuid.uuid4()}", tenant_id="t-1")  # type: ignore[arg-type]
    buffer = PgPendingInputBuffer(app_conninfo)

    # Appended in this exact order - the split-thought example from D19. The ordering
    # assertion below fails if drain resorts them by anything other than arrival (e.g.
    # alphabetically, or shortest-first).
    asyncio.run(buffer.append(session, UserInput(text="hola")))
    asyncio.run(buffer.append(session, UserInput(text="queria preguntar algo")))
    asyncio.run(buffer.append(session, UserInput(text="sobre mi pedido 123")))

    # A message on a DIFFERENT session must never leak into this session's drain.
    asyncio.run(buffer.append(other_session, UserInput(text="not this session")))

    # SIMULATE A CRASH MID-DRAIN. Run the adapter's own DELETE ... RETURNING statement
    # on a separate connection, read the result back exactly as the real drain step
    # would, then abandon the transaction WITHOUT committing - the crash. If draining
    # is truly one atomic unit, nothing was actually removed.
    with psycopg.connect(app_conninfo) as crashed:
        crashed_rows = crashed.execute(_DRAIN_SQL, (session.session_id,)).fetchall()
        crashed.rollback()
    assert len(crashed_rows) == 3, "the fixture must see all three rows before the crash"

    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM pending_inputs WHERE session_id = %s",
            (session.session_id,),
        ).fetchone()
    assert remaining is not None and remaining[0] == 3, (
        "a crash before commit must lose nothing - the buffered rows must still be there"
    )

    drained = asyncio.run(buffer.drain(session))

    assert [message.text for message in drained] == [
        "hola",
        "queria preguntar algo",
        "sobre mi pedido 123",
    ], "drain must return the buffered messages in arrival order"

    # NO DUPLICATES: draining again after a real, committed drain must come back empty -
    # the same messages must never be handed out a second time.
    assert asyncio.run(buffer.drain(session)) == ()

    # The other session's message was never touched by either drain above.
    assert [message.text for message in asyncio.run(buffer.drain(other_session))] == [
        "not this session"
    ]
