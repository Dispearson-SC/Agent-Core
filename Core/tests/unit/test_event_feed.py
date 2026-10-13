"""C22 (D53): the bounded event feed.

Subjects: domain/events.py (`fold_snapshot`), application/event_feed.py (`EventFeed`), and the SSE
frames of adapters/driving/http/event_routes.py. The reader and the wake-up are in-memory fakes; the
Postgres ones are `tests/integration/test_event_feed.py`.

What is defended:
  * a SLOW subscriber is collapsed to the newest snapshot: its queue never exceeds its bound, it
    is never handed an overflow or a hole, and it carries on with live events after the snapshot;
  * a reconnect resumes from a snapshot (the same path as a first connection);
  * one tree's subscribers never see another tree's or another tenant's events;
  * the stream ends when the tree has settled.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core.adapters.driving.http.event_routes import frames
from agent_core.application.event_feed import EventFeed, FeedItem, FeedKind
from agent_core.domain.events import (
    EventKind,
    EventOwner,
    EventWindow,
    ProgressEvent,
    StoredEvent,
    TurnState,
    fold_snapshot,
    progress_event_id,
)
from agent_core.domain.peers import BackgroundState
from agent_core.domain.task_graph import AskRecord, AskWindow

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROOT = "root-1"


def _event(
    turn: str,
    kind: EventKind,
    *,
    n: int = 0,
    tenant: str = "t",
    root: str = ROOT,
    tool: str | None = None,
    elapsed: int | None = None,
) -> ProgressEvent:
    owner = None if turn == root else EventOwner(root, root, "asker-agent", f"call-{turn}")
    return ProgressEvent(
        event_id=progress_event_id(turn, kind, str(n)),
        kind=kind,
        tenant_id=tenant,
        session_id="s",
        turn_id=turn,
        agent=f"agent-{turn}",
        at=T0 + timedelta(seconds=n),
        owner=owner,
        tool=tool,
        elapsed_ms=elapsed,
    )


class Store:
    """An in-memory `EventReader` + `EventWakeup`."""

    def __init__(self, *, tree_limit: int | None = None) -> None:
        self.rows: list[StoredEvent] = []
        self.tree_limit = tree_limit
        self._woke = asyncio.Event()

    def add(self, event: ProgressEvent) -> StoredEvent:
        stored = StoredEvent(seq=len(self.rows) + 1, event=event)
        self.rows.append(stored)
        self._woke.set()
        return stored

    async def after(self, seq: int, *, limit: int = 500) -> tuple[StoredEvent, ...]:
        return tuple(r for r in self.rows if r.seq > seq)[:limit]

    async def tree(self, tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        mine = tuple(
            r
            for r in self.rows
            if r.event.tenant_id == tenant_id and r.event.root_turn_id == root_turn_id
        )
        limit = limit if self.tree_limit is None else self.tree_limit
        return EventWindow(mine[-limit:], omitted=max(0, len(mine) - limit))

    async def latest_seq(self) -> int:
        return len(self.rows)

    async def close(self) -> None:
        return None

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._woke.wait(), timeout)
        except TimeoutError:
            pass
        self._woke.clear()


def _feed(store: Store, size: int = 4) -> EventFeed:
    return EventFeed(store, store, queue_size=size, poll_seconds=0.01)


def run[T](coroutine: Any) -> T:
    return asyncio.run(coroutine)  # type: ignore[no-any-return]


# --- the snapshot ---


def test_a_snapshot_folds_each_turn_to_its_state_with_its_tool_count() -> None:
    events = (
        StoredEvent(1, _event(ROOT, EventKind.STARTED)),
        StoredEvent(2, _event("peer-a", EventKind.STARTED)),
        StoredEvent(3, _event("peer-a", EventKind.TOOL_CALLED, tool="lookup")),
        StoredEvent(4, _event("peer-a", EventKind.TOOL_CALLED, n=1, tool="lookup")),
        StoredEvent(5, _event("peer-a", EventKind.FINISHED, elapsed=1200)),
        StoredEvent(6, _event("peer-b", EventKind.STARTED)),
        StoredEvent(7, _event("peer-b", EventKind.FAILED, elapsed=300)),
    )

    snapshot = fold_snapshot(ROOT, events)

    assert snapshot.last_seq == 7 and not snapshot.settled, "the root is still running"
    by_turn = {t.turn_id: t for t in snapshot.turns}
    assert [t.turn_id for t in snapshot.turns] == [ROOT, "peer-a", "peer-b"]
    assert by_turn["peer-a"].state is TurnState.FINISHED and by_turn["peer-a"].tools_called == 2
    assert by_turn["peer-a"].elapsed_ms == 1200 and by_turn["peer-a"].last_tool == "lookup"
    assert by_turn["peer-b"].state is TurnState.FAILED
    assert by_turn["peer-a"].asker_turn_id == ROOT and by_turn["peer-a"].call == "call-peer-a"


def test_a_tree_is_settled_only_when_nothing_in_it_is_running() -> None:
    running = fold_snapshot(ROOT, (StoredEvent(1, _event(ROOT, EventKind.STARTED)),))
    done = fold_snapshot(
        ROOT,
        (
            StoredEvent(1, _event(ROOT, EventKind.STARTED)),
            StoredEvent(2, _event(ROOT, EventKind.FINISHED, elapsed=5)),
        ),
    )

    assert not running.settled and done.settled
    assert not fold_snapshot(ROOT, ()).settled, "an empty tree is not settled: nothing started yet"


# --- the broker and one subscriber ---


def test_a_subscriber_gets_a_snapshot_then_the_events_after_it_until_the_tree_settles() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    feed = _feed(store)

    async def scenario() -> list[tuple[FeedKind, int]]:
        await feed.pump()  # the broker starts from "now"
        seen: list[tuple[FeedKind, int]] = []
        stream = feed.follow("t", ROOT)
        first = await anext(stream)
        seen.append((first.kind, first.snapshot.last_seq if first.snapshot else -1))
        store.add(_event(ROOT, EventKind.TOOL_CALLED, tool="x"))
        store.add(_event(ROOT, EventKind.FINISHED, elapsed=9))
        await feed.pump()
        async for item in stream:
            seen.append((item.kind, item.event.seq if item.event else -1))
        return seen

    seen = run(scenario())

    assert seen == [
        (FeedKind.SNAPSHOT, 1),
        (FeedKind.EVENT, 2),
        (FeedKind.EVENT, 3),
        (FeedKind.END, -1),
    ]
    assert feed.subscriber_count == 0, "a finished stream unsubscribes"


def test_a_slow_subscriber_is_collapsed_to_the_newest_snapshot_not_buffered() -> None:
    """The acceptance test: 40 events while the client reads nothing, with a queue of 4."""
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    feed = _feed(store, size=4)

    async def scenario() -> tuple[list[Any], int]:
        await feed.pump()
        stream = feed.follow("t", ROOT)
        first = await anext(stream)
        assert first.kind is FeedKind.SNAPSHOT
        sub = next(iter(feed._subscribers))  # noqa: SLF001
        deepest = 0
        for n in range(40):  # the client is not reading
            store.add(_event(ROOT, EventKind.TOOL_CALLED, n=n, tool=f"tool-{n}"))
            await feed.pump()
            deepest = max(deepest, sub.queue.qsize())
        # The client wakes up: what it receives next is the newest state, not 40 stale events.
        second = await anext(stream)
        store.add(_event(ROOT, EventKind.TOOL_CALLED, n=99, tool="live"))
        await feed.pump()
        third = await anext(stream)
        return [second, third], deepest

    (second, third), deepest = run(scenario())

    assert deepest <= 4, "the queue never grew past its bound"
    assert second.kind is FeedKind.SNAPSHOT, "a lagging subscriber gets a snapshot, not an overflow"
    (turn,) = second.snapshot.turns
    assert turn.tools_called == 40 and turn.last_tool == "tool-39", "the newest state"
    assert second.snapshot.last_seq == 41
    assert third.kind is FeedKind.EVENT and third.event.event.tool == "live", "then live events"


def test_a_reconnect_starts_from_a_snapshot_of_the_tree_as_it_is() -> None:
    store = Store()
    for event in (
        _event(ROOT, EventKind.STARTED),
        _event("peer-a", EventKind.STARTED),
        _event("peer-a", EventKind.TOOL_CALLED, tool="x"),
    ):
        store.add(event)
    feed = _feed(store)

    async def scenario() -> Any:
        await feed.pump()
        return await anext(feed.follow("t", ROOT))

    first = run(scenario())

    assert first.kind is FeedKind.SNAPSHOT and first.snapshot.last_seq == 3
    assert {t.turn_id: t.tools_called for t in first.snapshot.turns} == {ROOT: 0, "peer-a": 1}


def test_a_settled_tree_is_one_snapshot_and_an_end() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))

    async def scenario() -> list[FeedKind]:
        return [item.kind async for item in _feed(store).follow("t", ROOT)]

    assert run(scenario()) == [FeedKind.SNAPSHOT, FeedKind.END]


def test_subscribers_see_their_own_tree_and_tenant_only() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    feed = _feed(store)

    async def scenario() -> tuple[Any, bool]:
        await feed.pump()
        mine = feed.follow("t", ROOT)
        other_tenant = feed.follow("other-tenant", ROOT)  # the same root id, another tenant
        other_tree = feed.follow("t", "another-root")
        for stream in (mine, other_tenant, other_tree):
            await anext(stream)
        store.add(_event(ROOT, EventKind.TOOL_CALLED, tool="mine"))
        await feed.pump()
        first = await asyncio.wait_for(anext(mine), 1)
        leaked = False
        for stream in (other_tenant, other_tree):
            try:
                await asyncio.wait_for(anext(stream), 0.05)
                leaked = True
            except TimeoutError:
                pass
        return first, leaked

    first, leaked = run(scenario())

    assert first.event.event.tool == "mine"
    assert not leaked, "another tenant's or another tree's subscriber was handed the event"


def test_a_quiet_tree_yields_keepalives() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))

    async def scenario() -> list[FeedKind]:
        stream = _feed(store).follow("t", ROOT, keepalive_seconds=0.01)
        await anext(stream)
        return [(await anext(stream)).kind for _ in range(2)]

    assert run(scenario()) == [FeedKind.KEEPALIVE, FeedKind.KEEPALIVE]


# --- a missed terminal event and background asks (C1p) ---


def test_a_missed_terminal_event_is_corrected_by_a_snapshot_and_the_stream_ends() -> None:
    """The broker never pumps the FINISHED event (a lost wake-up, a read that failed): the idle
    wait must not trust the queue alone. The next idle period reads a snapshot, which shows the
    tree settled, and the stream ends instead of yielding keepalives forever."""
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    feed = _feed(store)

    async def scenario() -> list[FeedKind]:
        await feed.pump()
        stream = feed.follow("t", ROOT, keepalive_seconds=0.02)
        kinds = [(await anext(stream)).kind]
        store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))  # NOT pumped: the broker missed it
        for _ in range(8):
            try:
                item = await asyncio.wait_for(anext(stream), 1)
            except StopAsyncIteration:
                break
            kinds.append(item.kind)
        return kinds

    kinds = run(scenario())

    assert FeedKind.END in kinds, f"the stream never ended: {kinds}"
    assert kinds[-1] is FeedKind.END
    assert FeedKind.SNAPSHOT in kinds[1:], "the correction is a snapshot that replaces the state"


class _Asks:
    """A `TaskGraphReader` whose asks the test flips."""

    def __init__(self, *states: BackgroundState) -> None:
        self.states = list(states)

    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        return AskWindow(
            tuple(
                AskRecord(f"h{n}", "billing", "a", root_turn_id, state, True, 0, T0)
                for n, state in enumerate(self.states)
            )
        )


def test_the_stream_stays_open_while_a_background_ask_runs_and_ends_when_it_is_done() -> None:
    """The root turn finished, but an ask it left running is still part of the tree: ending now
    would tell the client nothing is left while a peer is working."""
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))
    asks = _Asks(BackgroundState.RUNNING)
    feed = EventFeed(store, store, poll_seconds=0.01, asks=asks)

    async def scenario() -> tuple[Any, list[FeedKind], list[FeedKind]]:
        stream = feed.follow("t", ROOT, keepalive_seconds=0.02)
        first = await anext(stream)
        while_running = [(await asyncio.wait_for(anext(stream), 1)).kind for _ in range(2)]
        asks.states = [BackgroundState.ANSWERED]
        after: list[FeedKind] = []
        for _ in range(8):
            try:
                after.append((await asyncio.wait_for(anext(stream), 1)).kind)
            except StopAsyncIteration:
                break
        return first, while_running, after

    first, while_running, after = run(scenario())

    assert first.kind is FeedKind.SNAPSHOT and first.snapshot.open_asks == 1
    assert not first.snapshot.settled, "a running background ask keeps the tree unsettled"
    assert FeedKind.END not in while_running
    assert after[-1] is FeedKind.END


def test_a_queued_background_ask_also_keeps_the_stream_open() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))
    feed = EventFeed(store, store, poll_seconds=0.01, asks=_Asks(BackgroundState.QUEUED))

    async def scenario() -> Any:
        return await anext(feed.follow("t", ROOT))

    assert run(scenario()).snapshot.open_asks == 1


class _Truncated:
    """A window that holds one finished ask and left three out; `open_total` is the SQL count over
    all of them."""

    def __init__(self, open_total: int) -> None:
        self.open_total = open_total
        self.counted = 0

    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        done = AskRecord("h0", "billing", "a", root_turn_id, BackgroundState.ANSWERED, True, 0, T0)
        return AskWindow((done,), omitted=3)

    async def count_open(self, tenant_id: str, root_turn_id: str) -> int:
        self.counted += 1
        return self.open_total


def _first_snapshot(asks: Any) -> Any:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))
    feed = EventFeed(store, store, poll_seconds=0.01, asks=asks)

    async def scenario() -> Any:
        return await anext(feed.follow("t", ROOT))

    return run(scenario()).snapshot


def test_a_truncated_ask_window_reports_the_exact_open_count_not_the_omitted_one() -> None:
    """C1r: the window left three asks out, but a count over ALL of them says only one is open."""
    asks = _Truncated(open_total=1)

    snapshot = _first_snapshot(asks)

    assert snapshot.open_asks == 1 and not snapshot.settled
    assert asks.counted == 1


def test_a_permanently_truncated_window_still_settles_once_nothing_is_open() -> None:
    """C1r R4: the old rule counted every omitted ask as open, so a tree with more asks than the
    window holds could never end however long ago they finished."""
    snapshot = _first_snapshot(_Truncated(open_total=0))

    assert snapshot.open_asks == 0 and snapshot.settled


def test_a_complete_window_is_counted_from_its_records_without_a_second_read() -> None:
    asks = _Asks(BackgroundState.RUNNING, BackgroundState.ANSWERED)
    asks.count_open = _boom  # type: ignore[method-assign]

    assert _first_snapshot(asks).open_asks == 1


async def _boom(tenant_id: str, root_turn_id: str) -> int:
    raise AssertionError("the window was complete: no count query is needed")


def test_a_failing_idle_snapshot_read_keeps_the_stream_and_retries_at_the_next_idle() -> None:
    """C1q: one failed snapshot read while idle (a database blip) must not end the stream."""
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    real_tree = store.tree
    calls = {"n": 0}

    async def flaky_tree(tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        calls["n"] += 1
        if calls["n"] == 2:  # the first idle read
            raise ConnectionError("blip")
        return await real_tree(tenant_id, root_turn_id, limit=limit)

    store.tree = flaky_tree  # type: ignore[method-assign]
    feed = _feed(store)

    async def scenario() -> list[FeedKind]:
        stream = feed.follow("t", ROOT, keepalive_seconds=0.01)
        kinds = [(await anext(stream)).kind]
        store.add(
            _event(ROOT, EventKind.FINISHED, elapsed=3)
        )  # not pumped: only a snapshot sees it
        for _ in range(8):
            try:
                kinds.append((await asyncio.wait_for(anext(stream), 1)).kind)
            except StopAsyncIteration:
                break
        return kinds

    kinds = run(scenario())

    assert calls["n"] >= 3, "the read was retried after the failure"
    assert kinds[-1] is FeedKind.END, kinds


# --- the SSE frames ---


def test_the_frames_are_valid_sse_with_ids_to_resume_from() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    store.add(_event(ROOT, EventKind.FINISHED, elapsed=3))

    async def scenario() -> list[str]:
        return [chunk.decode() async for chunk in frames(_feed(store).follow("t", ROOT))]

    snapshot_frame, end_frame = run(scenario())

    assert snapshot_frame.startswith("event: snapshot\nid: 2\ndata: ")
    body = json.loads(snapshot_frame.split("data: ", 1)[1])
    assert body["settled"] is True and body["turns"][0]["state"] == "finished"
    assert body["open_asks"] == 0
    assert end_frame.startswith("event: end\n") and snapshot_frame.endswith("\n\n")


def test_a_snapshot_read_that_keeps_failing_ends_the_stream_with_an_error_item() -> None:
    """C1r R4: the idle snapshot retry is bounded. After `max_snapshot_failures` consecutive
    failures the stream says so and ends instead of retrying for ever."""
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    real_tree = store.tree
    calls = {"n": 0}

    async def dead_tree(tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        calls["n"] += 1
        if calls["n"] > 1:  # the first read opens the stream; every later one fails
            raise ConnectionError("the database is gone")
        return await real_tree(tenant_id, root_turn_id, limit=limit)

    store.tree = dead_tree  # type: ignore[method-assign]
    feed = EventFeed(store, store, poll_seconds=0.01, max_snapshot_failures=3)

    async def scenario() -> list[FeedKind]:
        stream = feed.follow("t", ROOT, keepalive_seconds=0.01)
        kinds: list[FeedKind] = []
        for _ in range(10):
            try:
                kinds.append((await asyncio.wait_for(anext(stream), 1)).kind)
            except StopAsyncIteration:
                break
        return kinds

    kinds = run(scenario())

    assert kinds[-1] is FeedKind.ERROR, kinds
    assert calls["n"] == 1 + 3, "three consecutive failures, then it stopped"
    assert feed.subscriber_count == 0


def test_a_successful_read_resets_the_failure_count() -> None:
    store = Store()
    store.add(_event(ROOT, EventKind.STARTED))
    real_tree = store.tree
    calls = {"n": 0}

    async def every_other(tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        calls["n"] += 1
        if calls["n"] > 1 and calls["n"] % 2 == 0:
            raise ConnectionError("blip")
        return await real_tree(tenant_id, root_turn_id, limit=limit)

    store.tree = every_other  # type: ignore[method-assign]
    feed = EventFeed(store, store, poll_seconds=0.01, max_snapshot_failures=2)

    async def scenario() -> list[FeedKind]:
        stream = feed.follow("t", ROOT, keepalive_seconds=0.01)
        return [(await asyncio.wait_for(anext(stream), 1)).kind for _ in range(8)]

    assert FeedKind.ERROR not in run(scenario())


def test_an_error_item_is_an_error_frame() -> None:
    async def one() -> AsyncIterator[FeedItem]:
        yield FeedItem(FeedKind.ERROR)

    async def scenario() -> list[str]:
        return [chunk.decode() async for chunk in frames(one())]

    (frame,) = run(scenario())

    assert frame == 'event: error\ndata: {"error": "snapshot_unavailable"}\n\n'
