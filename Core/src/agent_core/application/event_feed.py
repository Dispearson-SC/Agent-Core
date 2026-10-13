"""Application: the bounded event feed of a turn tree (C22, D53).

Phase:   Agent-comms v2, wave 3
Tests:   Core/tests/unit/test_event_feed.py
Ports:   ports/event_reader.py (EventReader, EventWakeup)

ONE BROKER PER PROCESS reads new events once and fans them out to the subscribers that asked for
their tree; a subscriber is one open SSE connection. The two design points:

BOUNDED PER SUBSCRIBER, COLLAPSED TO THE NEWEST STATE. Every subscriber owns a queue of at most
`queue_size` events. A client that reads more slowly than the tree publishes does not make the
queue grow and does not make the broker wait: the moment the queue is full it is emptied and the
subscriber is marked as lagging. Its next item is a SNAPSHOT of the whole tree as of now,
followed by the live events after that snapshot. This is Pi's observer semantics (a slow watcher
receives the newest committed state, not every intermediate one) and the opposite of OpenCode's
overflow, where the buffer grows or events are dropped without telling the reader. A consumer
cannot tell a dropped event from a quiet tree, so it is always told: a snapshot REPLACES what it
held.

EVERY CONNECTION STARTS WITH A SNAPSHOT. A new subscriber, a reconnect (`Last-Event-ID`) and a
lagging one all take the same path, so there is one way to catch up and one thing for a client to
implement.
The subscription is registered BEFORE the snapshot is read and events at or below the snapshot's
sequence are skipped, so nothing between the two is lost or repeated.

WHAT THE FEED DOES NOT DO: it does not authorise. The caller (the HTTP route) has already shown the
tenant may read this turn, and every read here is scoped by that tenant anyway.

KNOWN LIMIT. Order is the store's sequence. Two publishers in different transactions can make a
lower sequence visible after a higher one was read; the broker does not go back for it. The next
snapshot (any reconnect, any lag) corrects the state, and a turn's events are published seconds
apart in different steps, so the window is small. Do not build exactness on top of the feed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from agent_core.domain.events import EventKind, FeedSnapshot, StoredEvent, fold_snapshot
from agent_core.domain.peers import BackgroundState
from agent_core.ports.event_reader import EventReader, EventWakeup
from agent_core.ports.task_graph_reader import TaskGraphReader

__all__ = [
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_MAX_SNAPSHOT_FAILURES",
    "DEFAULT_RECONCILE_SECONDS",
    "DEFAULT_QUEUE_SIZE",
    "EventFeed",
    "FeedItem",
    "FeedKind",
]

_LOG = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE: Final[int] = 64
DEFAULT_KEEPALIVE_SECONDS: Final[float] = 15.0
DEFAULT_RECONCILE_SECONDS: Final[float] = 5.0
DEFAULT_MAX_SNAPSHOT_FAILURES: Final[int] = 5
_POLL_SECONDS: Final[float] = 2.0
_OPEN_ASKS: Final[frozenset[BackgroundState]] = frozenset(
    {BackgroundState.QUEUED, BackgroundState.RUNNING}
)
_BATCH: Final[int] = 500


class FeedKind(StrEnum):
    SNAPSHOT = "snapshot"
    EVENT = "event"
    KEEPALIVE = "keepalive"
    END = "end"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FeedItem:
    """What a subscriber is handed: a snapshot, one event, a keepalive or the end."""

    kind: FeedKind
    snapshot: FeedSnapshot | None = None
    event: StoredEvent | None = None


class _Subscription:
    """One subscriber's bounded queue. A None in it is the "you lagged" marker."""

    __slots__ = ("lagging", "queue", "root_turn_id", "tenant_id")

    def __init__(self, tenant_id: str, root_turn_id: str, size: int) -> None:
        self.tenant_id = tenant_id
        self.root_turn_id = root_turn_id
        self.queue: asyncio.Queue[StoredEvent | None] = asyncio.Queue(maxsize=size)
        self.lagging = False

    def offer(self, stored: StoredEvent) -> None:
        """Queue the event, or collapse: empty the queue and leave ONE lag marker in it. While the
        marker is unread everything is dropped on purpose; the snapshot covers it."""
        if self.lagging:
            return
        if self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)
            self.lagging = True
            return
        self.queue.put_nowait(stored)


class EventFeed:
    def __init__(
        self,
        reader: EventReader,
        wakeup: EventWakeup,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        poll_seconds: float = _POLL_SECONDS,
        asks: TaskGraphReader | None = None,
        reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS,
        max_snapshot_failures: int = DEFAULT_MAX_SNAPSHOT_FAILURES,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        if max_snapshot_failures < 1:
            raise ValueError("max_snapshot_failures must be at least 1")
        self._max_failures = max_snapshot_failures
        self._reader = reader
        self._wakeup = wakeup
        self._queue_size = queue_size
        self._poll = poll_seconds
        self._asks = asks
        self._reconcile = reconcile_seconds
        self._subscribers: set[_Subscription] = set()
        self._cursor: int | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- the broker ---------------------------------------------------------------------------

    async def pump(self) -> int:
        """Read the events published since the last pump and hand each to its subscribers.
        Returns how many were read. The first pump starts from "now": history is the snapshot's."""
        if self._cursor is None:
            self._cursor = await self._reader.latest_seq()
            return 0
        total = 0
        while True:
            batch = await self._reader.after(self._cursor, limit=_BATCH)
            for stored in batch:
                self._cursor = stored.seq
                self._dispatch(stored)
            total += len(batch)
            if len(batch) < _BATCH:
                return total

    def _dispatch(self, stored: StoredEvent) -> None:
        event = stored.event
        for sub in tuple(self._subscribers):
            if sub.tenant_id == event.tenant_id and sub.root_turn_id == event.root_turn_id:
                sub.offer(stored)

    async def run(self) -> None:
        """Pump forever (cancel to stop; the wake-up is closed then). A read that fails is logged
        and retried: the feed is an observer and must outlive a database blip."""
        try:
            while True:
                try:
                    await self.pump()
                    await self._wakeup.wait(self._poll)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOG.warning(
                        "the event feed could not read new events; retrying", exc_info=True
                    )
                    await asyncio.sleep(self._poll)
        finally:
            await asyncio.shield(self._wakeup.close())

    # --- one subscriber ------------------------------------------------------------------------

    async def _snapshot(self, tenant_id: str, root_turn_id: str) -> FeedSnapshot:
        """The tree as it is now: its events folded, plus how many asks under it are still open
        (a background ask outlives the turn that made it, and its callee may not have started)."""
        window = await self._reader.tree(tenant_id, root_turn_id)
        snapshot = fold_snapshot(root_turn_id, window.events, omitted=window.omitted)
        if self._asks is None:
            return snapshot
        asks = await self._asks.asks_under(tenant_id, root_turn_id)
        if asks.omitted:
            # The window left asks out, so it cannot say how many are open. Counting the omitted
            # ones as open would keep a big tree from ever settling; the exact count comes from
            # the store, over all of them (R4, C1r).
            open_asks = await self._asks.count_open(tenant_id, root_turn_id)
        else:
            open_asks = sum(1 for record in asks.records if record.state in _OPEN_ASKS)
        return replace(snapshot, open_asks=open_asks)

    async def _snapshot_or_none(self, tenant_id: str, root_turn_id: str) -> FeedSnapshot | None:
        """`_snapshot`, but a failed read is logged and returns None: the stream is an observer and
        the next idle period reads again, so a database blip must not end it."""
        try:
            return await self._snapshot(tenant_id, root_turn_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.warning("the event feed could not read a snapshot; retrying", exc_info=True)
            return None

    async def follow(
        self,
        tenant_id: str,
        root_turn_id: str,
        *,
        keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
    ) -> AsyncIterator[FeedItem]:
        """The feed of one tree: a snapshot, then events, until the tree has settled.

        Ends (after an `END` item) when a SNAPSHOT shows the tree settled: the root turn and
        everything under it finished or failed and no ask under it is still open. The end is never
        decided from the live events alone, because a missed one (a lost wake-up, a failed read)
        would then keep the stream open for ever, and a finished root with a running background
        ask would end it too early.

        The idle wait is bounded by `reconcile_seconds`: every idle period reads a snapshot, so
        whatever the live path missed is corrected by the next one (yielded when it differs from
        what the client holds). A keepalive is yielded whenever `keepalive_seconds` pass quietly.
        Cancel the iteration to unsubscribe.

        A snapshot read that fails is retried at the next idle period, at most
        `max_snapshot_failures` times in a row; past that the stream yields an `ERROR` item and
        ends, so a client reconnects (and gets a fresh snapshot) instead of waiting on a feed
        that can no longer tell it anything. A read that succeeds resets the count.
        """
        sub = _Subscription(tenant_id, root_turn_id, self._queue_size)
        self._subscribers.add(sub)
        idle = min(keepalive_seconds, self._reconcile)
        clock = asyncio.get_running_loop().time
        failures = 0  # consecutive snapshot reads that failed; bounded by max_snapshot_failures
        try:
            snapshot = await self._snapshot(tenant_id, root_turn_id)
            yield FeedItem(FeedKind.SNAPSHOT, snapshot=snapshot)
            floor, shown_open, last_sent = snapshot.last_seq, snapshot.open_asks, clock()
            if snapshot.settled:
                yield FeedItem(FeedKind.END, snapshot=snapshot)
                return
            while True:
                try:
                    stored = await asyncio.wait_for(sub.queue.get(), timeout=idle)
                except TimeoutError:
                    fresh = await self._snapshot_or_none(tenant_id, root_turn_id)
                    if fresh is None:
                        failures += 1
                        if failures >= self._max_failures:
                            yield FeedItem(FeedKind.ERROR)
                            return
                        continue
                    failures = 0
                    snapshot = fresh
                    changed = snapshot.last_seq > floor or snapshot.open_asks != shown_open
                    if changed or snapshot.settled:
                        floor, shown_open, last_sent = (
                            snapshot.last_seq,
                            snapshot.open_asks,
                            clock(),
                        )
                        yield FeedItem(FeedKind.SNAPSHOT, snapshot=snapshot)
                    elif clock() - last_sent >= keepalive_seconds:
                        last_sent = clock()
                        yield FeedItem(FeedKind.KEEPALIVE)
                    if snapshot.settled:
                        yield FeedItem(FeedKind.END, snapshot=snapshot)
                        return
                    continue
                if stored is None:  # this subscriber fell behind: the newest state replaces it
                    sub.lagging = False
                    fresh = await self._snapshot_or_none(tenant_id, root_turn_id)
                    if fresh is None:  # the next idle snapshot corrects the state
                        failures += 1
                        if failures >= self._max_failures:
                            yield FeedItem(FeedKind.ERROR)
                            return
                        continue
                    failures = 0
                    snapshot = fresh
                    floor, shown_open, last_sent = snapshot.last_seq, snapshot.open_asks, clock()
                    yield FeedItem(FeedKind.SNAPSHOT, snapshot=snapshot)
                    if snapshot.settled:
                        yield FeedItem(FeedKind.END, snapshot=snapshot)
                        return
                    continue
                if stored.seq <= floor:
                    continue
                floor, last_sent = stored.seq, clock()
                yield FeedItem(FeedKind.EVENT, event=stored)
                if stored.event.kind in (EventKind.FINISHED, EventKind.FAILED):
                    fresh = await self._snapshot_or_none(tenant_id, root_turn_id)
                    if fresh is None:  # the next idle snapshot decides whether the tree settled
                        failures += 1
                        if failures >= self._max_failures:
                            yield FeedItem(FeedKind.ERROR)
                            return
                        continue
                    failures = 0
                    snapshot = fresh
                    shown_open = snapshot.open_asks
                    if snapshot.settled:
                        yield FeedItem(FeedKind.END, snapshot=snapshot)
                        return
        finally:
            self._subscribers.discard(sub)
