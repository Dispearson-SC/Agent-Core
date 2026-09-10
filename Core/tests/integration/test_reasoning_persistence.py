"""Integration tests for admin-only reasoning persistence.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-04
Covers:  adapters/driven/persistence_pg/conversation_repository.py
         adapters/driven/persistence_pg/reasoning_migration.py

WHAT IS BEING DEFENDED

    A reasoning block is the model's private working-out. `docs/FIELD-NOTES.md` records
    that MiniMax M3 returns `reasoning_content` in BOTH modes, so this is not a rare path:
    every single turn produces one. It contains intermediate speculation the model
    discarded - including guesses about the user that were never said out loud - and
    `domain/transcript.py` answers `REASONING` as `USER: False, ADMIN: True`.

    Two failure modes sit either side of that cell, and both are silent:

      LOST      - the block was never stored, so an operator debugging a wrong answer has
                  the answer and not the reason for it. Nothing errors.
      LEAKED    - the block reached a USER read. Nothing errors either; the user simply
                  reads what the agent privately thought about them, and nobody finds out
                  until it is quoted back in a complaint.

    So the tests below assert the two halves separately, on the same block: it IS there
    for `Audience.ADMIN`, and it is NOT there for `Audience.USER` - nor anywhere in the
    conversation history that every existing USER-facing read path is built on.

WHY THE HISTORY ASSERTION IS NOT REDUNDANT
    Reasoning could have been stored as another row in `messages`. It would then flow
    into `load_history` - the model's conversation and the USER transcript's raw
    material - and the only thing standing between it and a user would be a WHERE clause
    every future reader has to remember. A separate table makes the defence structural
    rather than remembered, and this assertion is what pins that.

These need a real Postgres and skip cleanly without one, the same pattern as
test_conversation_repository.py and test_peer_mailbox.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import migrations, reasoning_migration
from agent_core.adapters.driven.persistence_pg.conversation_repository import (
    PgConversationStore,
)
from agent_core.domain.transcript import Audience, EntryKind
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
    """A migrated app database for this module. Idempotent, so every test may call it."""
    app_db = "agent_core_reasoning_test"
    dbos_db = "agent_core_reasoning_test_dbos"
    asyncio.run(
        migrations.ensure_databases(_ADMIN_CONNINFO, app_database=app_db, dbos_database=dbos_db)
    )
    app_conninfo = _app_conninfo(app_db)
    asyncio.run(migrations.run_migrations(app_conninfo))
    asyncio.run(reasoning_migration.apply_turn_reasoning_migration(app_conninfo))
    return app_conninfo


def _session(tenant: str = "t-1") -> SessionRef:
    return SessionRef(session_id=SessionId(f"s-{uuid.uuid4()}"), tenant_id=TenantId(tenant))


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_reasoning_block_is_persisted_and_never_reaches_a_user_read() -> None:
    """The whole of t-f10-04 in one test: stored for ADMIN, absent for USER."""
    conninfo = _migrated_conninfo()
    session = _session()
    turn_id = TurnId(str(uuid.uuid4()))
    secret = f"they said 400 but are probably lying about the budget [{uuid.uuid4()}]"

    asyncio.run(PgConversationStore(conninfo).append_reasoning(turn_id, session, secret))

    # PERSISTED. Read back through a store that never saw the write - all a freshly
    # deployed process holds is a connection string, so the database is the only thing
    # carrying the block across.
    reader = PgConversationStore(conninfo)
    admin_entries = asyncio.run(reader.load_reasoning(session, Audience.ADMIN))

    assert [entry.payload["content"] for entry in admin_entries] == [secret], (
        "the reasoning block did not come back for Audience.ADMIN. An operator "
        "debugging this turn has the answer and no record of why the agent gave it, "
        "and nothing anywhere reported a failure"
    )
    assert admin_entries[0].kind is EntryKind.REASONING, (
        f"entry came back as {admin_entries[0].kind!r}; the transcript projection routes "
        "on kind, and a mislabelled entry is filtered by the wrong row of VISIBILITY"
    )
    assert admin_entries[0].turn_id == turn_id, (
        "the stored block lost the turn it belongs to, so it cannot be placed in the "
        "timeline next to the answer it explains"
    )

    # NEVER RETURNED TO A USER READ.
    user_entries = asyncio.run(reader.load_reasoning(session, Audience.USER))
    user_payloads = json.dumps([entry.payload for entry in user_entries], default=str)
    assert secret not in user_payloads, (
        "the reasoning text came back inside a USER payload. domain/transcript.py "
        "answers REASONING as USER: False - this is the model's private speculation "
        "about the person now reading it"
    )
    assert user_entries == (), (
        f"a USER read returned {len(user_entries)} reasoning entries; even a redacted "
        "one tells the user there is something to redact"
    )

    # AND NOT VIA THE HISTORY EITHER. If reasoning were a `messages` row it would arrive
    # here, one forgotten WHERE clause away from every user-facing read there is.
    history = asyncio.run(reader.load_history(session))
    assert secret not in json.dumps(history, default=str), (
        "the reasoning text is inside load_history(). That is the model's conversation "
        "and the raw material of the USER transcript; storing it there makes the "
        "admin-only rule something every future reader has to remember"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_retried_step_stores_one_reasoning_block() -> None:
    """`append_reasoning` runs inside a DBOS step, and a step is re-executed after a crash.

    Every turn emits a block (FIELD-NOTES: M3 reasons in both modes), so a second row per
    retry is not a curiosity - it is the transcript showing an operator that the agent
    thought the same thing twice, in a table already described as bulky enough to need a
    retention decision of its own (t-f10-05).
    """
    conninfo = _migrated_conninfo()
    session = _session()
    turn_id = TurnId(str(uuid.uuid4()))
    block = f"check the refund window first [{uuid.uuid4()}]"
    store = PgConversationStore(conninfo)

    asyncio.run(store.append_reasoning(turn_id, session, block))
    asyncio.run(store.append_reasoning(turn_id, session, block))

    entries = asyncio.run(store.load_reasoning(session, Audience.ADMIN))
    assert len(entries) == 1, (
        f"a retried step left {len(entries)} copies of one reasoning block; the "
        "duplicate is settled in the database or it is not settled at all"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_a_reasoning_read_cannot_cross_a_tenant() -> None:
    """The tenant predicate is in the SQL, not applied afterwards.

    Reasoning is the most sensitive row in the transcript; a session id guessed or reused
    across tenants must not be enough to read one.
    """
    conninfo = _migrated_conninfo()
    owner = _session(tenant="t-owner")
    intruder = SessionRef(session_id=owner.session_id, tenant_id=TenantId("t-intruder"))
    block = f"the account is flagged for review [{uuid.uuid4()}]"

    store = PgConversationStore(conninfo)
    asyncio.run(store.append_reasoning(TurnId(str(uuid.uuid4())), owner, block))

    stolen = asyncio.run(store.load_reasoning(intruder, Audience.ADMIN))
    assert stolen == (), (
        "another tenant read this session's reasoning by naming the same session id; "
        "the tenant predicate is missing from the query"
    )
