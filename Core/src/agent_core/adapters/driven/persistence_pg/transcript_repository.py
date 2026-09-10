"""Driven adapter: TranscriptReader over Postgres.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-03
Implements: ports/transcript_reader.py
Status:  DONE - page() and list_conversations(), keyset cursors, tenant predicate in SQL
Tests:   Core/tests/integration/test_transcript_repository.py

Migration 0016 lives in transcript_migration.py, next to this module, and creates the two
views this file reads: `transcript_entries` and `transcript_conversations`. That is where
the UNION over messages, audit rows, pending requests, peer traffic and checkpoints lives.
Adding a source is a change to that file and to nothing here.

THE TENANT PREDICATE IS IN THE QUERY (the reason this adapter is worth a test at all)
    `ports/transcript_reader.py` states it plainly: this endpoint returns whole
    conversations, so a missing tenant predicate is a cross-tenant data breach with a
    friendly UI on top.

    Filtering in Python after the rows have come back is not the same thing and is not an
    acceptable substitute. It returns the right answer on a small result set and the wrong
    one the first time `limit` truncates the fetch before the filter ever sees the other
    tenant's rows - and it puts those rows in memory, in a traceback, and in whatever log
    catches the exception in between. `Core/tests/integration/test_transcript_repository.py`
    therefore asserts over the SQL this module emits, not over the rows it returns.

    The tenant comes from `SessionRef` and from nowhere else. The port refuses to pass it
    a second time on purpose: two sources for one fact can disagree, and then the adapter
    is the one choosing which one scopes the query.

THE CURSOR IS KEYSET, NEVER OFFSET
    `(at, entry_id) > (cursor_at, cursor_entry_id)`, ordered by the same pair. A live
    conversation grows while somebody reads it. Under `OFFSET n` every row inserted behind
    the cursor slides the next page backwards and re-serves entries the caller already
    has; every row deleted slides it forwards and skips entries the caller never saw.
    Neither raises. The page is simply wrong, and the reader has no way to notice.

    `entry_id` is in the key because timestamps tie - a turn row and its message are
    written in one transaction and `now()` is the transaction's clock, so ties are the
    normal case here rather than the rare one. A cursor over `at` alone is ambiguous
    exactly where rows are densest.

    The token is opaque and this module mints it. It is base64 of the pair, which is not
    encryption and is not meant to be: it is a shape the caller cannot construct by hand
    and therefore cannot turn into the free-form filter the port refuses to offer.

REDACTION IS BY LOOKUP, NOT BY BRANCH
    `domain/transcript.py` makes `VISIBILITY` total over `EntryKind` x `Audience`, so this
    module indexes it and never needs a fallback. There is exactly one place where the
    projection does something other than filter, and it is the one non-negotiable #11
    requires: a `PENDING_REQUEST` a USER may not see becomes a `PENDING_PLACEHOLDER`, with
    a payload built here from scratch rather than narrowed from the row - a narrowed
    payload keeps whatever key nobody remembered to drop, and the key that matters is the
    tool name.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any

from agent_core.domain.transcript import (
    VISIBILITY,
    VISIBLE_TO,
    Audience,
    ConversationSummaryRow,
    EntryKind,
    TranscriptEntry,
    TranscriptPage,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId, TurnId

# A source of connections that belong to THIS reader. `psycopg_pool.ConnectionPool
# .connection` satisfies it directly; so does `lambda: psycopg.connect(conninfo)`.
# Taking a factory rather than a connection keeps the read off whatever transaction the
# caller happens to be holding - see the same reasoning, for the opposite reason, in
# audit_repository.py.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# An upper bound the caller cannot raise. A transcript page is an unbounded join over a
# conversation's whole history; `limit=1000000` is one request that reads it all into
# memory, and the port's contract offers no reason to allow it.
_MAX_LIMIT = 200

_CURSOR_SEPARATOR = "\x1f"

# `cost_usd` is admin-only, by the same rule that makes tool arguments admin-only: it is
# operational data about the agent, not part of the conversation. It is declared here
# rather than as a cell in `VISIBILITY` because that grid answers a question about ENTRY
# KINDS, and cost is a field on an entry of any kind. Widening the grid to hold fields
# would make every new field a migration of every row of it.
_COST_VISIBLE_TO: frozenset[Audience] = frozenset({Audience.ADMIN})

# WHERE names `tenant_id` first because that is the predicate whose absence is a breach,
# and because `ix_turns_tenant_session` (migration 0016) leads on it. The cursor
# comparison is row-wise over the exact pair the ORDER BY sorts on - written as one
# tuple comparison rather than the unrolled `at > %s OR (at = %s AND entry_id > %s)`,
# which is the same predicate with three more chances to get an edge wrong.
_PAGE_SQL = """
    SELECT entry_id, turn_id, kind, at, payload, cost_usd
    FROM transcript_entries
    WHERE tenant_id = %s
      AND session_id = %s
      AND kind = ANY(%s)
      AND (
        %s::timestamptz IS NULL
        OR (at, entry_id) > (%s::timestamptz, %s::text)
      )
    ORDER BY at ASC, entry_id ASC
    LIMIT %s
"""

# The sort column is chosen from this closed map and never from anything a caller sent.
# `ports/transcript_reader.py` refuses to offer a free-form filter for exactly this
# reason: the moment one exists, the tenant predicate stops being the port's guarantee.
#
# The suspended queue sorts by `waiting_since` because the port says it is the operator's
# work queue, oldest first - a queue sorted by last activity puts the conversation that
# has been stuck longest at the bottom, which is the opposite of what it is for.
_LIST_SORT_COLUMNS: dict[bool, str] = {
    False: "last_activity_at",
    True: "waiting_since",
}

_LIST_SQL_TEMPLATE = """
    SELECT session_id, profile_id, last_activity_at, message_count,
           is_suspended, waiting_since, total_cost_usd, {sort_column} AS sort_at
    FROM transcript_conversations
    WHERE tenant_id = %s
      AND (%s::text IS NULL OR profile_id = %s)
      AND (NOT %s::boolean OR waiting_since IS NOT NULL)
      AND (
        %s::timestamptz IS NULL
        OR ({sort_column}, session_id) > (%s::timestamptz, %s::text)
      )
    ORDER BY {sort_column} ASC, session_id ASC
    LIMIT %s
"""


def _encode_cursor(at: datetime, tiebreak: str) -> str:
    """The opaque token: base64url of the ordering pair, padding stripped."""
    raw = f"{at.isoformat()}{_CURSOR_SEPARATOR}{tiebreak}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    """The ordering pair back out, or `(None, None)` for the first page.

    A malformed token raises rather than silently paging from the beginning. A cursor the
    adapter did not mint means the caller is constructing pagination by hand, and quietly
    handing them page 1 hides that until somebody notices a viewer that loops forever.
    """
    if cursor is None:
        return None, None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode()
        encoded_at, tiebreak = raw.split(_CURSOR_SEPARATOR, 1)
        return datetime.fromisoformat(encoded_at), tiebreak
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("malformed transcript cursor") from error


def _substitutes_placeholder(audience: Audience) -> bool:
    """Does this audience get a `PENDING_PLACEHOLDER` where a `PENDING_REQUEST` stands?

    Straight off the grid, both cells: the request must be hidden from them AND the
    placeholder must be visible to them. An audience that may see the real request gets
    the real request; one that may see neither gets nothing at all, which is the safe
    reading of a grid row nobody has filled in favourably.
    """
    return (
        not VISIBILITY[EntryKind.PENDING_REQUEST][audience]
        and VISIBILITY[EntryKind.PENDING_PLACEHOLDER][audience]
    )


def _requested_kinds(audience: Audience) -> list[str]:
    """The kinds the SQL asks for: what this audience may see, minus the one kind that is
    never stored, plus the one row the projection fetches only in order to replace it.

    A list, not a tuple: psycopg adapts a list to an array and a tuple to a record, and
    `kind = ANY(record)` is a different question with a confusing error. Sorted, so two
    runs emit byte-identical SQL parameters - a query whose parameters reorder between
    runs cannot be compared in a plan, a log, or a test.
    """
    kinds = {
        kind for kind in VISIBLE_TO[audience] if kind is not EntryKind.PENDING_PLACEHOLDER
    }
    if _substitutes_placeholder(audience):
        kinds.add(EntryKind.PENDING_REQUEST)
    return sorted(str(kind) for kind in kinds)


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), _MAX_LIMIT))


class PgTranscriptReader:
    """Postgres adapter for `ports.transcript_reader.TranscriptReader`.

    Read-only. There is no method here that writes, which is what makes "this port stores
    nothing new" structural rather than a convention.

    D13: every port method is async; the psycopg calls underneath are synchronous and are
    wrapped in `asyncio.to_thread`.
    """

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    @classmethod
    def from_conninfo(cls, conninfo: str) -> PgTranscriptReader:
        """Build a reader that opens its own connection per query.

        Imported lazily so the module - and the projection logic - stays importable
        without a compiled psycopg on the machine.
        """
        import psycopg

        return cls(lambda: psycopg.connect(conninfo))

    async def page(
        self,
        session: SessionRef,
        audience: Audience,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TranscriptPage:
        """One page of the timeline, oldest first, filtered for `audience`.

        The tenant travels inside `session` and reaches the SQL as a bound parameter; the
        cursor is decoded before the thread hop so a malformed one raises on the caller's
        stack rather than inside a worker thread.
        """
        bounded = _bounded(limit)
        cursor_at, cursor_entry_id = _decode_cursor(cursor)
        kinds = _requested_kinds(audience)

        rows = await asyncio.to_thread(
            self._fetch_page_sync, session, kinds, cursor_at, cursor_entry_id, bounded
        )

        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        entries = tuple(self._to_entry(row, audience) for row in page_rows)
        next_cursor = _encode_cursor(page_rows[-1][3], page_rows[-1][0]) if has_more else None

        return TranscriptPage(
            session=session,
            audience=audience,
            entries=entries,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _fetch_page_sync(
        self,
        session: SessionRef,
        kinds: list[str],
        cursor_at: datetime | None,
        cursor_entry_id: str | None,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        # limit + 1 so `has_more` is answered by the same round trip. A separate COUNT
        # over a union of five tables costs more than the page it describes.
        params = (
            str(session.tenant_id),
            str(session.session_id),
            kinds,
            cursor_at,
            cursor_at,
            cursor_entry_id,
            limit + 1,
        )
        with self._connect() as connection:
            rows: list[tuple[Any, ...]] = connection.execute(_PAGE_SQL, params).fetchall()
        return rows

    def _to_entry(self, row: tuple[Any, ...], audience: Audience) -> TranscriptEntry:
        """One projected row. The only transformation is the placeholder substitution."""
        entry_id, turn_id, kind_value, at, payload, cost_usd = row
        kind = EntryKind(kind_value)
        entry_payload: dict[str, object] = dict(payload or {})

        if kind is EntryKind.PENDING_REQUEST and _substitutes_placeholder(audience):
            # Built here, not narrowed from `entry_payload`. A narrowed payload keeps
            # whatever key nobody remembered to drop, and the key that matters is the
            # tool name. The user learns THAT something is pending and since when, which
            # is the whole of what non-negotiable #11 asks for.
            kind = EntryKind.PENDING_PLACEHOLDER
            entry_payload = {"since": at.isoformat()}

        return TranscriptEntry(
            entry_id=str(entry_id),
            turn_id=TurnId(str(turn_id)) if turn_id is not None else TurnId(""),
            kind=kind,
            at=at,
            payload=entry_payload,
            cost_usd=(
                Decimal(str(cost_usd))
                if cost_usd is not None and audience in _COST_VISIBLE_TO
                else None
            ),
        )

    async def list_conversations(
        self,
        tenant: TenantId,
        audience: Audience,
        *,
        profile_id: str | None = None,
        suspended_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[tuple[ConversationSummaryRow, ...], str | None]:
        """The inbox view, tenant-scoped in SQL exactly like `page`.

        `suspended_only` swaps the sort key to `waiting_since` so the operator's queue is
        oldest-stuck-first; the cursor is keyed to whichever column is sorting, so a
        cursor minted for one listing is not silently valid for the other.
        """
        bounded = _bounded(limit)
        cursor_at, cursor_session_id = _decode_cursor(cursor)
        sql = _LIST_SQL_TEMPLATE.format(sort_column=_LIST_SORT_COLUMNS[bool(suspended_only)])

        rows = await asyncio.to_thread(
            self._fetch_list_sync,
            sql,
            tenant,
            profile_id,
            bool(suspended_only),
            cursor_at,
            cursor_session_id,
            bounded,
        )

        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        summaries = tuple(self._to_summary(row, tenant, audience) for row in page_rows)
        next_cursor = _encode_cursor(page_rows[-1][7], str(page_rows[-1][0])) if has_more else None
        return summaries, next_cursor

    def _fetch_list_sync(
        self,
        sql: str,
        tenant: TenantId,
        profile_id: str | None,
        suspended_only: bool,
        cursor_at: datetime | None,
        cursor_session_id: str | None,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        params = (
            str(tenant),
            profile_id,
            profile_id,
            suspended_only,
            cursor_at,
            cursor_at,
            cursor_session_id,
            limit + 1,
        )
        with self._connect() as connection:
            rows: list[tuple[Any, ...]] = connection.execute(sql, params).fetchall()
        return rows

    def _to_summary(
        self, row: tuple[Any, ...], tenant: TenantId, audience: Audience
    ) -> ConversationSummaryRow:
        """`waiting_since` and `is_suspended` survive into the USER projection on purpose -
        the same reason `PENDING_PLACEHOLDER` exists. `total_cost_usd` does not."""
        (
            session_id,
            profile_id,
            last_activity_at,
            message_count,
            is_suspended,
            waiting_since,
            total_cost_usd,
            _sort_at,
        ) = row
        return ConversationSummaryRow(
            session=SessionRef(session_id=SessionId(str(session_id)), tenant_id=tenant),
            profile_id=str(profile_id),
            last_activity_at=last_activity_at,
            message_count=int(message_count),
            is_suspended=bool(is_suspended),
            waiting_since=waiting_since,
            total_cost_usd=(
                Decimal(str(total_cost_usd))
                if total_cost_usd is not None and audience in _COST_VISIBLE_TO
                else None
            ),
        )
