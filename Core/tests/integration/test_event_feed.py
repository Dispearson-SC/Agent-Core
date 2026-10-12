"""The event feed on a REAL Postgres: reader, LISTEN wake-up and the bounded subscriber (C22, D53).

Phase:   Agent-comms v2, wave 3
Covers:  adapters/driven/events_pg/reader.py, application/event_feed.py over the real adapters

WHAT IS BEING DEFENDED

    1. THE TENANT PREDICATE IS IN THE QUERY. `tree` for another tenant's root reads nothing.
    2. A LIVE EVENT ARRIVES THROUGH NOTIFY, not only through the poll: with a poll interval of a
       minute the subscriber still sees the event within the test's deadline.
    3. A SLOW SUBSCRIBER IS COLLAPSED TO A SNAPSHOT of the rows as they are in the database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from agent_core.adapters.driven.events_pg.publisher import PgEventPublisher
from agent_core.adapters.driven.events_pg.reader import PgEventReader, PgEventWakeup
from agent_core.application.event_feed import EventFeed, FeedKind
from agent_core.domain.events import EventKind, EventOwner, ProgressEvent, progress_event_id
from tests.fakes import peer_world as w

_DATABASE = "agent_core_event_feed_test"
ROOT = "root-1"

pytestmark = pytest.mark.skipif(not w.postgres_reachable(), reason="no reachable Postgres instance")


def _event(
    turn: str, kind: EventKind, *, n: int = 0, tenant: str = "t", tool: str | None = None
) -> ProgressEvent:
    return ProgressEvent(
        event_id=progress_event_id(turn, kind, f"{tenant}-{n}"),
        kind=kind,
        tenant_id=tenant,
        session_id="s",
        turn_id=turn,
        agent=f"agent-{turn}",
        at=datetime(2026, 10, 12, tzinfo=UTC),
        owner=None if turn == ROOT else EventOwner(ROOT, ROOT, "asker", f"call-{turn}"),
        tool=tool,
        elapsed_ms=5 if kind in (EventKind.FINISHED, EventKind.FAILED) else None,
    )


def test_the_tree_is_read_for_its_own_tenant_only_and_in_order() -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        reader = PgEventReader(w.conninfo(_DATABASE))
        await publisher.publish(_event(ROOT, EventKind.STARTED))
        await publisher.publish(_event("peer-a", EventKind.STARTED))
        await publisher.publish(_event(ROOT, EventKind.STARTED, tenant="other"))
        mine = await reader.tree("t", ROOT)
        theirs = await reader.tree("other", ROOT)
        later = await reader.after(mine.events[0].seq)
        return mine, theirs, later

    mine, theirs, later = asyncio.run(scenario())

    assert [s.event.turn_id for s in mine.events] == [ROOT, "peer-a"] and mine.omitted == 0
    assert [s.event.tenant_id for s in theirs.events] == ["other"]
    assert mine.events[1].event.owner == EventOwner(ROOT, ROOT, "asker", "call-peer-a")
    assert mine.events[0].event.owner is None, "a turn a person started has no owner"
    assert [s.seq for s in later] == sorted(s.seq for s in later) and len(later) == 2


def test_a_live_event_reaches_a_subscriber_through_notify_and_a_slow_one_gets_a_snapshot() -> None:
    async def scenario() -> tuple[Any, Any, Any, int]:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        reader = PgEventReader(w.conninfo(_DATABASE))
        await publisher.publish(_event(ROOT, EventKind.STARTED))
        # A poll of a minute: only NOTIFY can deliver within the deadline below.
        feed = EventFeed(
            reader, PgEventWakeup(w.conninfo(_DATABASE)), queue_size=2, poll_seconds=60.0
        )
        broker = asyncio.create_task(feed.run())
        try:
            await asyncio.sleep(0.3)  # the broker has taken "now" and is listening
            fast = feed.follow("t", ROOT)
            slow = feed.follow("t", ROOT)
            assert (await anext(fast)).kind is FeedKind.SNAPSHOT
            assert (await anext(slow)).kind is FeedKind.SNAPSHOT
            await publisher.publish(_event(ROOT, EventKind.TOOL_CALLED, n=0, tool="t-0"))
            live = await asyncio.wait_for(anext(fast), 5)
            for n in range(1, 8):  # `slow` reads nothing while the tree keeps publishing
                await publisher.publish(_event(ROOT, EventKind.TOOL_CALLED, n=n, tool=f"t-{n}"))
            await asyncio.sleep(1.0)
            collapsed = await asyncio.wait_for(anext(slow), 5)
            return live, collapsed, feed, max(s.queue.qsize() for s in feed._subscribers)  # noqa: SLF001
        finally:
            broker.cancel()

    live, collapsed, _, deepest = asyncio.run(scenario())

    assert live.kind is FeedKind.EVENT and live.event.event.tool == "t-0"
    assert collapsed.kind is FeedKind.SNAPSHOT
    (root_turn,) = collapsed.snapshot.turns
    assert root_turn.tools_called == 8 and root_turn.last_tool == "t-7"
    assert deepest <= 2


def test_a_tree_over_the_limit_returns_its_newest_events_and_counts_the_older_ones() -> None:
    async def scenario() -> tuple[Any, Any]:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        reader = PgEventReader(w.conninfo(_DATABASE))
        await publisher.publish(_event(ROOT, EventKind.STARTED))
        for n in range(4):
            await publisher.publish(_event(ROOT, EventKind.TOOL_CALLED, n=n, tool=f"t-{n}"))
        await publisher.publish(_event(ROOT, EventKind.FINISHED))
        return await reader.tree("t", ROOT, limit=3), await reader.tree("t", ROOT)

    cut, whole = asyncio.run(scenario())

    assert cut.omitted == 3 and len(cut.events) == 3
    assert [s.event.kind for s in cut.events][-1] is EventKind.FINISHED, "the newest state is kept"
    assert [s.seq for s in cut.events] == sorted(s.seq for s in cut.events), "still in seq order"
    assert whole.omitted == 0 and len(whole.events) == 6


def test_the_wakeup_listens_on_one_connection_and_does_not_lose_a_notification_between_waits() -> (
    None
):
    """C1p: it used to open a connection per `wait`, so anything published between two waits was
    not heard, and every wait paid a connect. One long-lived listener hears it."""

    async def scenario() -> tuple[int, float]:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        wakeup = PgEventWakeup(w.conninfo(_DATABASE))
        connects = 0
        real = psycopg.AsyncConnection.connect

        async def counting(*args: Any, **kwargs: Any) -> Any:
            nonlocal connects
            connects += 1
            return await real(*args, **kwargs)

        psycopg.AsyncConnection.connect = counting  # type: ignore[method-assign,assignment]
        try:
            await wakeup.wait(0.3)  # nothing yet: times out, and the listener stays up
            await publisher.publish(_event(ROOT, EventKind.STARTED))  # between two waits
            started = asyncio.get_running_loop().time()
            await wakeup.wait(5.0)
            heard_after = asyncio.get_running_loop().time() - started
            await wakeup.wait(0.1)
        finally:
            psycopg.AsyncConnection.connect = real  # type: ignore[method-assign]
            await wakeup.close()
        return connects, heard_after

    connects, heard_after = asyncio.run(scenario())

    assert connects == 1, f"the listener reconnected {connects} times"
    assert heard_after < 2.0, "a notification published between two waits was lost"


def test_a_streamed_reply_chunk_is_stored_with_its_text_and_folds_into_the_snapshot() -> None:
    """C26 (D58): migration 0040 adds `text`; a delta keeps up to MAX_DELTA_CHARS of it (a label is
    cut at 200) and the snapshot read back from Postgres carries the reply so far."""
    from agent_core.domain.events import fold_snapshot

    long_chunk = "Your order ships today and arrives tomorrow. " * 4  # 180 chars: over a label

    def delta(index: int, text: str) -> ProgressEvent:
        return ProgressEvent(
            event_id=progress_event_id(ROOT, EventKind.DELTA, f"run1:0:{index}"),
            kind=EventKind.DELTA,
            tenant_id="t",
            session_id="s",
            turn_id=ROOT,
            agent="agent",
            at=datetime(2026, 10, 12, tzinfo=UTC),
            detail=f"run1:0:{index}",
            text=text,
        )

    async def scenario() -> Any:
        await w.fresh_databases(_DATABASE)
        publisher = PgEventPublisher(w.conninfo(_DATABASE))
        reader = PgEventReader(w.conninfo(_DATABASE))
        await publisher.publish(_event(ROOT, EventKind.STARTED))
        await publisher.publish(delta(0, long_chunk + "x" * 100))
        await publisher.publish(delta(1, "Bye."))
        await publisher.publish(delta(1, "ignored: same event id"))
        return await reader.tree("t", ROOT)

    window = asyncio.run(scenario())

    texts = [s.event.text for s in window.events if s.event.kind is EventKind.DELTA]
    assert texts == [long_chunk + "x" * 100, "Bye."], "text beyond a label's 200 chars is kept"
    snapshot = fold_snapshot(ROOT, window.events)
    assert snapshot.turns[0].reply_text == long_chunk + "x" * 100 + "Bye."
    assert all(s.event.text is None for s in window.events if s.event.kind is EventKind.STARTED)
