"""Integration tests for the Postgres `TranscriptReader`.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-03
Covers:  adapters/driven/persistence_pg/transcript_repository.py
         adapters/driven/persistence_pg/transcript_migration.py

THE TWO PROPERTIES THAT ARE WORTH A TEST HERE

1.  THE TENANT PREDICATE IS IN THE SQL, NOT APPLIED AFTERWARDS.
    `ports/transcript_reader.py` says it in as many words: this endpoint returns whole
    conversations, and a missing tenant predicate is a cross-tenant data breach with a
    friendly UI on top. Filtering rows in Python after the query has already returned
    them is indistinguishable from filtering them in SQL - until the day a `limit`
    truncates the result set before the Python filter ever sees the other tenant's rows,
    or an exception between the fetch and the filter serialises them into a log.

    So the assertion is over the SQL the adapter ACTUALLY EMITS, captured from a fake
    connection, rather than over the rows that come back. A reader that returns the right
    rows for the wrong reason passes a row-based test and fails this one.

2.  THE CURSOR IS KEYSET, NOT OFFSET.
    `domain/transcript.py` explains why: a long conversation grows while somebody reads
    it, and offsets then silently skip or repeat entries. "Silently" is the operative
    word - nothing raises, the page just contains the wrong thing.

    The discriminating scenario is rows inserted BEHIND the cursor, in the stretch the
    reader has already paged past. Under `OFFSET 3` the second page slides three rows
    back and re-serves entries the caller already has. Under a keyset cursor over
    `(at, entry_id)` the second page is exactly the un-read tail, whatever was inserted
    behind it. That test needs a real database, because it is testing what Postgres does
    with the ordering, and it is skipped when no instance is reachable - mirroring
    tests/integration/test_migrations.py.

NEITHER MODULE IS IMPORTED BY NAME AT MODULE LEVEL.
    An `ImportError` at collection time is not a red test, it is a broken one. Both
    modules exist as headers before this file runs; the SYMBOLS are asserted inside a
    helper, so their absence surfaces as a failed assertion in a test that actually ran.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.persistence_pg import (
    migrations,
    transcript_migration,
    transcript_repository,
)
from agent_core.domain.transcript import Audience, EntryKind, TranscriptPage
from agent_core.domain.turn import SessionId, SessionRef, TenantId

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_TENANT = TenantId("t-transcript")
_SESSION = SessionRef(session_id=SessionId("s-transcript"), tenant_id=_TENANT)

_TURN_ID = "0f6c4b8e-2b4a-4a2f-9c1e-7d3b5a9e1c40"

# `tenant_id` compared against a bound parameter, in the WHERE clause itself. Written as a
# tolerant pattern so reformatting the query does not fail the test, and strict enough
# that a `tenant_id` appearing only in a SELECT list does not satisfy it.
_TENANT_PREDICATE = re.compile(r"\btenant_id\s*=\s*%s", re.IGNORECASE)


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _reader_class() -> Any:
    reader = getattr(transcript_repository, "PgTranscriptReader", None)
    assert reader is not None, (
        "adapters/driven/persistence_pg/transcript_repository.py exposes no "
        "PgTranscriptReader; docs/TASKS.md#t-f10-03"
    )
    return reader


def _projection_migration() -> Any:
    migration = getattr(transcript_migration, "TRANSCRIPT_PROJECTION_MIGRATION", None)
    assert migration is not None, (
        "adapters/driven/persistence_pg/transcript_migration.py exposes no "
        "TRANSCRIPT_PROJECTION_MIGRATION; docs/TASKS.md#t-f10-03 owns migration 0016"
    )
    return migration


class _RecordingConnection:
    """A connection that records every statement and returns no rows.

    Enough psycopg surface for a read-only adapter: `execute` returns something with
    `fetchall`, and the object is a context manager. It deliberately cannot return rows -
    this test is about the query, not the result.
    """

    def __init__(self, log: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._log = log

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _RecordingConnection:
        self._log.append((sql, tuple(params)))
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


def _capture(call: Any) -> list[tuple[str, tuple[Any, ...]]]:
    """Run one adapter call against a recording connection and return what it emitted."""
    log: list[tuple[str, tuple[Any, ...]]] = []
    reader = _reader_class()(lambda: _RecordingConnection(log))
    asyncio.run(call(reader))
    assert log, "the adapter emitted no SQL at all"
    return log


def test_the_tenant_predicate_is_in_the_sql_the_page_query_emits() -> None:
    """Tenant scoping is in the query. No Postgres required: the connection is faked.

    Both halves matter. The predicate has to be in the WHERE clause, and the value bound
    to it has to be the tenant carried by the `SessionRef` - a predicate against a
    hard-coded or defaulted tenant is worse than none, because it looks correct.
    """
    log = _capture(lambda reader: reader.page(_SESSION, Audience.ADMIN, limit=5))

    sql, params = log[0]
    assert _TENANT_PREDICATE.search(sql), (
        f"no tenant predicate in the page query; a transcript read that filters by tenant "
        f"in Python is a cross-tenant breach waiting for a truncated page:\n{sql}"
    )
    assert str(_TENANT) in [str(param) for param in params], (
        "the tenant predicate is not bound to the SessionRef's tenant"
    )
    assert not re.search(r"\bOFFSET\b", sql, re.IGNORECASE), (
        "the page query uses OFFSET; a growing conversation then skips or repeats entries"
    )


def test_the_tenant_predicate_is_in_the_sql_the_list_query_emits() -> None:
    """The inbox view is the query that returns OTHER people's conversations when the
    predicate is missing, so it gets the same assertion as `page`."""
    log = _capture(
        lambda reader: reader.list_conversations(_TENANT, Audience.ADMIN, limit=5)
    )

    sql, params = log[0]
    assert _TENANT_PREDICATE.search(sql), (
        f"no tenant predicate in the conversation-list query:\n{sql}"
    )
    assert str(_TENANT) in [str(param) for param in params]
    assert not re.search(r"\bOFFSET\b", sql, re.IGNORECASE)


def test_the_user_page_asks_for_the_pending_row_it_will_replace() -> None:
    """The kinds the query asks for come from `VISIBLE_TO`, with one deliberate
    addition: a USER may not see a `PENDING_REQUEST`, but the projection needs the row in
    order to hand back a `PENDING_PLACEHOLDER` in its place (non-negotiable #11).

    A query that filtered on `VISIBLE_TO[USER]` alone would never fetch the row, and the
    conversation would silently stop while the agent waits three days.
    """
    log = _capture(lambda reader: reader.page(_SESSION, Audience.USER, limit=5))

    requested = {
        str(value)
        for _, params in log
        for param in params
        if isinstance(param, list)
        for value in param
    }
    assert str(EntryKind.PENDING_REQUEST) in requested, (
        "the USER page never asks for the pending row it is meant to replace with a "
        "placeholder; the user cannot see THAT something is pending"
    )
    assert str(EntryKind.TOOL_CALL) not in requested, (
        "the USER page asks for tool calls; the user must never learn WHICH tool"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_the_cursor_stays_stable_when_rows_are_inserted_mid_page() -> None:
    """Page 1, then insert rows BEHIND the cursor, then page 2.

    Under offset pagination the second page slides back over the inserted rows and
    re-serves entries the caller already read. Under a keyset cursor it returns exactly
    the tail that was never read - no repeat, and no pre-existing entry skipped.
    """
    app_db = "agent_core_transcript_test"
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO, app_database=app_db, dbos_database=f"{app_db}_dbos"
        )
    )
    app_conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{app_db}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(app_conninfo))

    apply_migration = getattr(
        transcript_migration, "apply_transcript_projection_migration", None
    )
    assert apply_migration is not None, (
        "transcript_migration.py exposes no apply_transcript_projection_migration; "
        "migration 0016 has no way to reach a database"
    )
    _projection_migration()
    asyncio.run(apply_migration(app_conninfo))

    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = %s", (str(_SESSION.session_id),))
        conn.execute("DELETE FROM turns WHERE session_id = %s", (str(_SESSION.session_id),))
        conn.execute(
            "INSERT INTO turns (turn_id, session_id, tenant_id, profile_id, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (_TURN_ID, str(_SESSION.session_id), str(_TENANT), "p-1", "started", base),
        )
        for index in range(6):
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    str(_SESSION.session_id),
                    index + 1,
                    "user" if index % 2 == 0 else "assistant",
                    f'{{"text": "original-{index}"}}',
                    base + timedelta(minutes=10 * index),
                ),
            )

    reader = _reader_class()(lambda: psycopg.connect(app_conninfo))

    first: TranscriptPage = asyncio.run(reader.page(_SESSION, Audience.ADMIN, limit=3))
    assert len(first.entries) == 3, "the first page did not honour its limit"
    assert first.has_more is True
    assert first.next_cursor is not None

    # Inserted BEHIND the cursor: their timestamps fall inside the stretch page 1 already
    # covered. This is what an offset cursor cannot survive.
    with psycopg.connect(app_conninfo, autocommit=True) as conn:
        for index in range(2):
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    str(_SESSION.session_id),
                    100 + index,
                    "user",
                    f'{{"text": "inserted-{index}"}}',
                    base + timedelta(minutes=1 + index),
                ),
            )

    second: TranscriptPage = asyncio.run(
        reader.page(_SESSION, Audience.ADMIN, cursor=first.next_cursor, limit=10)
    )

    first_ids = [entry.entry_id for entry in first.entries]
    second_ids = [entry.entry_id for entry in second.entries]
    assert not set(first_ids) & set(second_ids), (
        f"page 2 re-served entries from page 1 after rows were inserted behind the "
        f"cursor: {sorted(set(first_ids) & set(second_ids))}"
    )

    read_texts = [
        str(entry.payload.get("content")) for entry in (*first.entries, *second.entries)
    ]
    for index in range(6):
        assert any(f"original-{index}" in text for text in read_texts), (
            f"original-{index} was skipped across the two pages"
        )
    assert not any("inserted-" in text for text in read_texts[len(first_ids) :]), (
        "a row inserted behind the cursor surfaced on page 2; the cursor is not keyset"
    )
