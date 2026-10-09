"""Transcript visibility invariants - the grid decides, never a call site.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-01, docs/TASKS.md#t-f10-07, docs/TASKS.md#t-f10-08,
         docs/TASKS.md#t-f10-09
Status:  t-f10-08 and t-f10-09 DONE - behavioural tests over the real
         `PgTranscriptReader`, added below the grid tests. Test-only anchors: neither
         `domain/transcript.py` nor `adapters/driven/persistence_pg/transcript_repository.py`
         was touched to make them pass.

Non-negotiable #11: transcripts store everything and filter on read. The visibility grid is
the single place that filtering is expressed, so a kind the grid does not mention is a kind
nobody decided about.

WHY THIS FILE WAS REWRITTEN
    The first version of the anchor read every value of `VISIBLE_TO` and asked whether each
    `EntryKind` appeared in at least one of them. `VISIBLE_TO[Audience.ADMIN]` was
    `frozenset(EntryKind)` - derived from the enum - so every kind appeared in it the
    instant it was declared. The assertion could not fail. It was not a guard; it was a
    restatement of the enum.

    The grid is now written cell by cell and `VISIBLE_TO` is derived FROM it. A missing
    kind, a missing audience, or a half-answered row fails here.

`t-f10-08` (the user projection never leaks a tool name) and `t-f10-09` (a query cannot
cross a tenant) own their own tests, appended below. By the time they were written the
projection existed (`t-f10-03`, DONE) - the grid above is what it reads.
"""

from agent_core.domain.transcript import VISIBILITY, VISIBLE_TO, Audience, EntryKind


def test_visible_to_covers_every_entry_kind() -> None:
    """The anchor assertion: no EntryKind is missing a visibility decision.

    Asked of `VISIBILITY`, not of the derived `VISIBLE_TO`. Asking the derived view is what
    made the previous version of this test unfalsifiable - a kind auto-admitted to ADMIN
    answers "yes, somebody can see it" without anybody having decided.
    """
    undecided = [kind for kind in EntryKind if kind not in VISIBILITY]

    assert undecided == [], (
        f"EntryKind members with no row in VISIBILITY: {undecided}. "
        "Adding a kind means deciding, for every Audience, whether it may be seen."
    )


def test_every_row_answers_every_audience() -> None:
    """A row that mentions one audience and forgets the other is half a decision.

    This is the other axis of the same guard: adding an `Audience` must fail here too,
    because a new audience with no answer in any row is exactly as undecided as a new kind.
    """
    incomplete = {
        kind: sorted(set(Audience) - set(row))
        for kind, row in VISIBILITY.items()
        if set(row) != set(Audience)
    }

    assert incomplete == {}, f"rows that do not answer every Audience: {incomplete}"


def test_admin_visibility_is_declared_and_not_the_whole_enum() -> None:
    """The regression lock for the defect this file was rewritten for.

    If ADMIN ever equals every `EntryKind` again, the coverage question above becomes
    satisfiable by the enum alone and stops being a question. Today ADMIN legitimately does
    not see `PENDING_PLACEHOLDER`: the placeholder is the substitute a USER gets INSTEAD of
    `PENDING_REQUEST`, and an operator reads the real request.
    """
    assert VISIBLE_TO[Audience.ADMIN] != frozenset(EntryKind), (
        "ADMIN is visible to every kind again. Derive it from the enum and the coverage "
        "test above can never fail."
    )
    assert EntryKind.PENDING_PLACEHOLDER not in VISIBLE_TO[Audience.ADMIN]
    assert EntryKind.PENDING_REQUEST in VISIBLE_TO[Audience.ADMIN]


def test_visible_to_is_exactly_the_declared_grid() -> None:
    """`VISIBLE_TO[audience]` stays the lookup the reader uses; the grid is where it comes
    from. If the two ever disagree, one of them is a second write path."""
    assert set(VISIBLE_TO) == set(Audience)

    # `.get(..., False)` deliberately, not `row[audience]`: an unanswered cell is the
    # business of `test_every_row_answers_every_audience`, which names it. Indexing here
    # would pre-empt that with a KeyError and hide the readable failure behind a traceback.
    for audience in Audience:
        declared = frozenset(
            kind for kind, row in VISIBILITY.items() if row.get(audience, False)
        )
        assert VISIBLE_TO[audience] == declared, (
            f"VISIBLE_TO[{audience}] disagrees with VISIBILITY"
        )


def test_user_never_learns_which_tool_is_pending() -> None:
    """Non-negotiable #11, first half.

    A USER must not learn WHICH tool is pending. That is not one kind: the tool name leaks
    through the call, through its result, and through the pending request itself.
    """
    naming_a_tool = {
        EntryKind.TOOL_CALL,
        EntryKind.TOOL_RESULT,
        EntryKind.PENDING_REQUEST,
    }

    leaked = sorted(naming_a_tool & VISIBLE_TO[Audience.USER])

    assert leaked == [], f"USER can see kinds that name a tool: {leaked}"


def test_user_still_learns_that_something_is_pending() -> None:
    """Non-negotiable #11, second half - and the half that is easy to lose.

    Hiding suspension entirely makes the conversation look broken: the agent waits three
    days for an approval and the user sees silence. `PENDING_PLACEHOLDER` says THAT
    something is pending without saying what.
    """
    assert EntryKind.PENDING_PLACEHOLDER in VISIBLE_TO[Audience.USER], (
        "USER cannot see that anything is pending. A suspended conversation is then "
        "indistinguishable from a broken one."
    )


def test_user_sees_the_conversation_itself() -> None:
    """The floor under the redaction: filtering on read must not filter away the messages.

    A grid that hid these would pass every assertion above and still return an empty
    conversation to the person having it.
    """
    for kind in (EntryKind.USER_MESSAGE, EntryKind.AGENT_MESSAGE):
        assert kind in VISIBLE_TO[Audience.USER], f"USER cannot see {kind}"


# ---------------------------------------------------------------------------------------
# t-f10-08 / t-f10-09 - behavioural tests over the REAL PgTranscriptReader
#
# Everything above this line asks the grid a question. Everything below runs actual rows
# through `adapters/driven/persistence_pg/transcript_repository.py` - the production
# module, unmodified - and inspects what a caller actually gets back. The grid tests
# above could all pass while the adapter still leaked, e.g. by returning a kind's raw
# payload without ever consulting the grid; these tests are what would catch that.
#
# NO LIVE POSTGRES: `PgTranscriptReader` takes a bare connection FACTORY (see its own
# `ConnectionFactory` type). `_FakeTranscriptDatabase` below stands in for Postgres and
# filters rows by the exact predicates the real SQL uses - tenant, session, requested
# kinds for `page()`; tenant, profile, suspended-only for `list_conversations()` - so the
# reader's own query-construction code (`_requested_kinds`, `_substitutes_placeholder`,
# the WHERE parameters) is what is under test, not a second copy of its logic.
# ---------------------------------------------------------------------------------------

import asyncio  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import TracebackType  # noqa: E402
from typing import Any  # noqa: E402

from agent_core.adapters.driven.persistence_pg.transcript_repository import (  # noqa: E402
    PgTranscriptReader,
)
from agent_core.domain.turn import SessionId, SessionRef, TenantId  # noqa: E402

# A distinctive token, unlikely to collide with anything else this file's payloads spell
# out by accident. If this string shows up anywhere in a USER-audience rendering, that is
# the leak non-negotiable #11 forbids.
_TOOL_NAME = "issue_refund_to_card_ending_4242"

_TENANT_A = TenantId("tenant-a")
_TENANT_B = TenantId("tenant-b")
# The SAME session id under two different tenants - the adversarial case the port's own
# docstring calls a "cross-tenant data breach with a friendly UI on top" if the tenant
# predicate is ever missing. A reader handed `_SESSION_A` must never surface `_SESSION_B`'s
# rows even though the session id string is identical.
_SHARED_SESSION_ID = SessionId("s-shared")
_SESSION_A = SessionRef(session_id=_SHARED_SESSION_ID, tenant_id=_TENANT_A)
_SESSION_B = SessionRef(session_id=_SHARED_SESSION_ID, tenant_id=_TENANT_B)

_SECRET_B = "tenant-b-only-conversation-content"


class _FakeConnection:
    """One query's worth of Postgres-shaped behaviour: `execute` then `fetchall`, used as
    a context manager. Never touches a real database."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> "_FakeConnection":
        if "transcript_conversations" in sql:
            self._rows = self._filter_list(params)
        else:
            self._rows = self._filter_page(params)
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    # These two exist only to be swapped in by `_FakeTranscriptDatabase.connect`, which
    # replaces them with closures bound to its own row store - see below.
    def _filter_page(self, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        raise NotImplementedError

    def _filter_list(self, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        raise NotImplementedError


class _FakeTranscriptDatabase:
    """A minimal stand-in for the two views migration 0016 creates.

    Filters by exactly the predicates the real WHERE clauses use - not by trusting the
    caller to have already narrowed things down - so a regression that dropped a
    predicate from `transcript_repository.py` (the tenant check, or the kind list) would
    make this fake return rows the adapter was never entitled to ask for, and the
    assertions below would catch it in the adapter's OWN output.
    """

    def __init__(self) -> None:
        # each entry row: (tenant_id, session_id, entry_id, turn_id, kind, at, payload, cost_usd)
        self._entries: list[tuple[str, str, str, str, str, datetime, dict[str, object], Any]] = []
        # each conversation row: (tenant_id, session_id, profile_id, last_activity_at,
        #                         message_count, is_suspended, waiting_since, total_cost_usd)
        self._conversations: list[tuple[Any, ...]] = []

    def add_entry(
        self,
        *,
        tenant_id: str,
        session_id: str,
        entry_id: str,
        turn_id: str,
        kind: str,
        at: datetime,
        payload: dict[str, object],
    ) -> None:
        self._entries.append((tenant_id, session_id, entry_id, turn_id, kind, at, payload, None))

    def add_conversation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        profile_id: str,
        last_activity_at: datetime,
        waiting_since: datetime | None = None,
    ) -> None:
        self._conversations.append(
            (tenant_id, session_id, profile_id, last_activity_at, 1, waiting_since is not None,
             waiting_since, None)
        )

    def connect(self) -> _FakeConnection:
        connection = _FakeConnection([])

        def filter_page(params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
            # Same order `_PAGE_SQL` binds: tenant, session, kinds, cursor_at (x2),
            # cursor_entry_id, limit.
            tenant_id, session_id, kinds, cursor_at, _cursor_at2, cursor_entry_id, limit = params
            matched = [
                row
                for row in self._entries
                if row[0] == tenant_id and row[1] == session_id and row[4] in kinds
            ]
            matched.sort(key=lambda row: (row[5], row[2]))
            if cursor_at is not None:
                matched = [
                    row for row in matched if (row[5], row[2]) > (cursor_at, cursor_entry_id)
                ]
            return [(row[2], row[3], row[4], row[5], row[6], row[7]) for row in matched[:limit]]

        def filter_list(params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
            # Same order `_LIST_SQL_TEMPLATE` binds: tenant, profile_id (x2),
            # suspended_only, cursor_at (x2), cursor_session_id, limit.
            (
                tenant_id, profile_id, _profile_id2, suspended_only,
                cursor_at, _cursor_at2, cursor_session_id, limit,
            ) = params
            sort_index = 6 if suspended_only else 3  # waiting_since vs last_activity_at
            matched = [
                row
                for row in self._conversations
                if row[0] == tenant_id
                and (profile_id is None or row[2] == profile_id)
                and (not suspended_only or row[6] is not None)
            ]
            matched.sort(key=lambda row: (row[sort_index], row[1]))
            if cursor_at is not None:
                matched = [
                    row
                    for row in matched
                    if (row[sort_index], row[1]) > (cursor_at, cursor_session_id)
                ]
            return [
                (row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[sort_index])
                for row in matched[:limit]
            ]

        connection._filter_page = filter_page  # type: ignore[method-assign]
        connection._filter_list = filter_list  # type: ignore[method-assign]
        return connection


def _seed_full_conversation(db: _FakeTranscriptDatabase, base: datetime) -> None:
    """One tenant-A conversation carrying the tool name through every kind that can
    plausibly carry it: the call itself, an operator's freeform note, and a compaction
    summary - not just the one field a narrower test would think to check."""
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-1", turn_id="turn-a",
        kind=str(EntryKind.USER_MESSAGE), at=base,
        payload={"role": "user", "content": "please refund my last order"},
    )
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-2", turn_id="turn-a",
        kind=str(EntryKind.AGENT_MESSAGE), at=base + timedelta(minutes=1),
        payload={"role": "assistant", "content": "Let me take care of that."},
    )
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-3", turn_id="turn-a",
        kind=str(EntryKind.TOOL_CALL), at=base + timedelta(minutes=2),
        payload={"tool": _TOOL_NAME, "caller": "agent", "arguments": {"order_id": "o-1"},
                 "effect": "allow", "rule_id": "r-1"},
    )
    db.add_entry(
        # Deliberately carries the tool name in its raw payload, even though the real
        # view never puts one there - proving the placeholder is BUILT FRESH rather than
        # a narrowed copy of whatever this row happened to hold.
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-4", turn_id="turn-a",
        kind=str(EntryKind.PENDING_REQUEST), at=base + timedelta(minutes=3),
        payload={"tool_call_id": "tc-1", "note": f"waiting on {_TOOL_NAME}", "answered_at": None},
    )
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-5", turn_id="turn-a",
        kind=str(EntryKind.HUMAN_DECISION), at=base + timedelta(minutes=4),
        payload={"tool_call_id": "tc-1", "subject_id": "op-1", "approved": True,
                 "note": f"approved {_TOOL_NAME} after checking the order"},
    )
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-6", turn_id="turn-a",
        kind=str(EntryKind.POLICY_DENIAL), at=base + timedelta(minutes=5),
        payload={"tool": _TOOL_NAME, "reason": "amount over the auto-approve limit"},
    )
    db.add_entry(
        tenant_id="tenant-a", session_id="s-shared", entry_id="e-a-7", turn_id="turn-a",
        kind=str(EntryKind.COMPACTION), at=base + timedelta(minutes=6),
        payload={"summary": f"the agent called {_TOOL_NAME} and it succeeded"},
    )


def test_user_projection_never_leaks_the_tool_name_anywhere_in_its_output() -> None:
    """t-f10-08. Non-negotiable #11, first half - proven against the real adapter.

    The tool name is planted in six different places: the call itself, an operator's
    freeform note, a policy denial, and a compaction summary - not just the field a
    narrower test would think to check. The search below is over the WHOLE rendered
    page, not a chosen field, so a leak through an unexpected corner cannot hide.
    """
    db = _FakeTranscriptDatabase()
    base = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_full_conversation(db, base)
    reader = PgTranscriptReader(db.connect)

    user_page = asyncio.run(reader.page(_SESSION_A, Audience.USER, limit=50))
    rendered = repr(user_page)

    assert _TOOL_NAME not in rendered, (
        f"the tool name leaked into the USER projection: {rendered}"
    )

    # Falsifiability: prove this is a real redaction, not an assertion that could never
    # fail because the data never carried the name in the first place. The admin
    # projection of the SAME conversation must contain it - otherwise the USER assertion
    # above would pass for the wrong reason.
    admin_page = asyncio.run(reader.page(_SESSION_A, Audience.ADMIN, limit=50))
    assert _TOOL_NAME in repr(admin_page), (
        "the seeded conversation never surfaced the tool name even for ADMIN; the "
        "USER-side assertion above would be vacuous"
    )

    # The non-negotiable's other half: hidden WHICH, but not THAT.
    user_kinds = {entry.kind for entry in user_page.entries}
    assert EntryKind.PENDING_PLACEHOLDER in user_kinds
    assert EntryKind.TOOL_CALL not in user_kinds
    assert EntryKind.PENDING_REQUEST not in user_kinds


def test_the_pending_placeholder_is_built_fresh_not_narrowed_from_the_request() -> None:
    """The one row in `_seed_full_conversation` most likely to leak by accident: its raw
    payload carries the tool name in a `note` field the real schema never populates. If
    the placeholder were ever built by copying the request and deleting known-bad keys,
    an unanticipated key like this one would ride straight through."""
    db = _FakeTranscriptDatabase()
    _seed_full_conversation(db, datetime(2026, 2, 1, tzinfo=UTC))
    reader = PgTranscriptReader(db.connect)

    user_page = asyncio.run(reader.page(_SESSION_A, Audience.USER, limit=50))

    placeholders = [e for e in user_page.entries if e.kind is EntryKind.PENDING_PLACEHOLDER]
    assert len(placeholders) == 1
    assert _TOOL_NAME not in repr(placeholders[0].payload)
    assert "since" in placeholders[0].payload


def test_a_page_query_cannot_cross_a_tenant_boundary() -> None:
    """t-f10-09, `page()`. The adversarial case: the SAME session id exists under two
    different tenants. `SessionRef` is what scopes the query - a reader handed
    `_SESSION_A` must never return a single row that belongs to tenant B, even though
    `session_id` alone cannot tell the two conversations apart.
    """
    db = _FakeTranscriptDatabase()
    base = datetime(2026, 2, 1, tzinfo=UTC)
    _seed_full_conversation(db, base)
    db.add_entry(
        tenant_id="tenant-b", session_id="s-shared", entry_id="e-b-1", turn_id="turn-b",
        kind=str(EntryKind.USER_MESSAGE), at=base,
        payload={"role": "user", "content": _SECRET_B},
    )
    reader = PgTranscriptReader(db.connect)

    page_a = asyncio.run(reader.page(_SESSION_A, Audience.ADMIN, limit=50))
    page_b = asyncio.run(reader.page(_SESSION_B, Audience.ADMIN, limit=50))

    a_entry_ids = {entry.entry_id for entry in page_a.entries}
    assert "e-b-1" not in a_entry_ids, "tenant A's page returned a row that belongs to tenant B"
    assert _SECRET_B not in repr(page_a), "tenant B's content leaked into tenant A's page"

    # Falsifiability, the same way as above: tenant B's own read must actually see its
    # row, and never tenant A's - otherwise "tenant A saw nothing of B's" could be true
    # for the vacuous reason that the fake never returns anything for either tenant.
    b_entry_ids = {entry.entry_id for entry in page_b.entries}
    assert b_entry_ids == {"e-b-1"}
    assert _TOOL_NAME not in repr(page_b), "tenant A's tool call leaked into tenant B's page"


def test_a_conversation_list_query_cannot_cross_a_tenant_boundary() -> None:
    """t-f10-09, `list_conversations()`. The inbox view is the query that returns OTHER
    people's conversations wholesale when the tenant predicate is missing - the same
    property, over the other read this port offers."""
    db = _FakeTranscriptDatabase()
    base = datetime(2026, 2, 1, tzinfo=UTC)
    db.add_conversation(
        tenant_id="tenant-a", session_id="s-a-1", profile_id="p-a", last_activity_at=base
    )
    db.add_conversation(
        tenant_id="tenant-b", session_id="s-b-1", profile_id="p-b", last_activity_at=base
    )
    reader = PgTranscriptReader(db.connect)

    rows_a, _ = asyncio.run(reader.list_conversations(_TENANT_A, Audience.ADMIN, limit=50))
    rows_b, _ = asyncio.run(reader.list_conversations(_TENANT_B, Audience.ADMIN, limit=50))

    assert {str(row.session.session_id) for row in rows_a} == {"s-a-1"}
    assert {str(row.session.session_id) for row in rows_b} == {"s-b-1"}
