"""Integration tests for the Postgres `ConversationStore` adapter.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-13
Covers:  adapters/driven/persistence_pg/conversation_repository.py

Needs a real Postgres and is skipped when one is not reachable - same pattern as
test_migrations.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

import psycopg
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    _LOAD_HISTORY_SQL,
    PgConversationStore,
    encode_message,
)
from agent_core.domain.turn import SessionRef


def _prompt(text: str) -> ModelMessage:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _text(text: str) -> ModelMessage:
    return ModelResponse(parts=[TextPart(text)])


def _only_text(message: ModelMessage) -> str:
    """The one text-bearing part of a message built by `_prompt` or `_text`."""
    (part,) = message.parts
    assert isinstance(part, (UserPromptPart, TextPart))
    return str(part.content)


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
def test_load_history_returns_messages_in_seq_order_using_the_session_seq_index() -> None:
    app_db = "agent_core_conversation_repo_test"
    dbos_db = "agent_core_conversation_repo_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))

    session_id = f"s-{uuid.uuid4()}"
    other_session_id = f"s-{uuid.uuid4()}"
    session = SessionRef(session_id=session_id, tenant_id="t-1")  # type: ignore[arg-type]

    # THE ROWS ARE BUILT BY THE ADAPTER'S OWN ENCODER, not typed out by hand. `content`
    # holds one Pydantic AI `ModelMessage` (t-f1-24), and a test that hand-wrote that JSON
    # would be a second copy of a third-party format - the thing `encode_message` exists so
    # that nobody keeps. What is still hand-written is the `seq`, which is the subject.
    #
    # Inserted OUT OF seq order on purpose: a test that inserts in order and then
    # asserts order-preserving return would pass even if load_history forgot the
    # ORDER BY entirely.
    rows = [
        (session_id, 3, _text("third")),
        (session_id, 1, _prompt("first")),
        (session_id, 2, _text("second")),
        # A message in a DIFFERENT session must never leak into this session's history.
        (other_session_id, 1, _prompt("not this session")),
    ]
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM messages WHERE session_id IN (%s, %s)",
            (session_id, other_session_id),
        )
        for row_session_id, seq, message in rows:
            role, content = encode_message(message)
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content) VALUES (%s, %s, %s, %s)",
                (row_session_id, seq, role, content),
            )

    store = PgConversationStore(app_conninfo)
    history = asyncio.run(store.load_history(session))

    assert [_only_text(message) for message in history] == ["first", "second", "third"]

    # The query the adapter actually runs must be answerable by the (session_id, seq)
    # index, not just "an index exists somewhere". Forcing seqscan off proves the
    # planner CAN satisfy this exact SQL from ix_messages_session_seq - on the tiny
    # table this test builds, the planner would otherwise pick a seq scan regardless
    # of the index, which would make this assertion pass for the wrong reason.
    #
    # THREE SETTINGS, NOT ONE, AND THE OTHER TWO ARE NOT DECORATION. `messages` carries a
    # SECOND index leading on session_id - `ix_messages_session_created`, migration 0016 -
    # and on a four-row table the planner is free to pick either and sort afterwards.
    # Which one it happens to choose is not a fact about this adapter, so this assertion
    # was a coin flip that happened to be landing the right way. Turning off the bitmap
    # path and the sort leaves exactly one plan that can satisfy `ORDER BY seq`: an
    # ordered scan of the (session_id, seq) index - which is the claim the test means.
    with psycopg.connect(app_conninfo) as conn:
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET enable_bitmapscan = off")
        conn.execute("SET enable_sort = off")
        plan_rows = conn.execute(
            f"EXPLAIN (FORMAT JSON) {_LOAD_HISTORY_SQL}", (session_id,)
        ).fetchall()
    plan_text = json.dumps(plan_rows[0][0])
    assert "Index Scan" in plan_text
    assert "ix_messages_session_seq" in plan_text
