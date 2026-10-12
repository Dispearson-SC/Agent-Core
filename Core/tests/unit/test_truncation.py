"""C1p (D55): a read that hit its limit says so, in the snapshot and in the graph.

Subjects: domain/events.py (`EventWindow`, `fold_snapshot`), domain/task_graph.py (`AskWindow`,
`build_task_graph`) and their JSON in adapters/driving/http/event_routes.py. The Postgres readers
that fill the counts are in tests/integration/test_event_feed.py and test_task_graph.py.

What is defended: a window that dropped rows reports HOW MANY, the JSON carries a flag and the
count, a complete window says nothing, and a turn whose `started` fell out of the window is still
shown (as running) rather than hidden, so the tree is never reported settled on a partial view.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from agent_core.adapters.driving.http.event_routes import graph_json, snapshot_json
from agent_core.adapters.driving.http.routes import create_app
from agent_core.application.event_feed import EventFeed, FeedKind
from agent_core.domain.events import EventKind, StoredEvent, TurnState, fold_snapshot
from agent_core.domain.task_graph import AskWindow, build_task_graph
from tests.unit.test_event_feed import ROOT, Store, _event
from tests.unit.test_task_graph import HEADERS, _Lookup, _record, _Starter
from tests.unit.test_task_graph import ROOT as GRAPH_ROOT


def test_a_snapshot_over_a_truncated_window_reports_how_many_events_it_dropped() -> None:
    events = (StoredEvent(5, _event(ROOT, EventKind.STARTED)),)

    complete = snapshot_json(fold_snapshot(ROOT, events))
    partial = snapshot_json(fold_snapshot(ROOT, events, omitted=7))

    assert complete["truncated"] is False and complete["omitted_events"] == 0
    assert partial["truncated"] is True and partial["omitted_events"] == 7


def test_a_turn_whose_started_event_fell_out_of_the_window_is_shown_not_hidden() -> None:
    events = (
        StoredEvent(8, _event("peer-a", EventKind.TOOL_CALLED, tool="lookup")),
        StoredEvent(9, _event(ROOT, EventKind.FINISHED, elapsed=3)),
    )

    whole = fold_snapshot(ROOT, events)
    partial = fold_snapshot(ROOT, events, omitted=4)

    assert not whole.turns, "unchanged when the window is complete: no `started`, nothing to show"
    by_turn = {t.turn_id: t for t in partial.turns}
    assert by_turn["peer-a"].state is TurnState.RUNNING, "the peer is still running as far as known"
    assert by_turn[ROOT].state is TurnState.FINISHED
    assert not partial.settled, "a partial view must not claim the tree is settled"


def test_the_feed_hands_the_window_s_count_to_its_snapshot() -> None:
    store = Store(tree_limit=3)
    store.add(_event(ROOT, EventKind.STARTED))
    for n in range(4):
        store.add(_event(ROOT, EventKind.TOOL_CALLED, n=n, tool=f"t{n}"))
    feed = EventFeed(store, store, poll_seconds=0.01)

    async def scenario() -> object:
        item = await anext(feed.follow("t", ROOT))
        assert item.kind is FeedKind.SNAPSHOT
        return item.snapshot

    snapshot = asyncio.run(scenario())

    assert snapshot.omitted_events == 2, "five events, a window of three"  # type: ignore[attr-defined]
    assert snapshot.last_seq == 5, "the window keeps the NEWEST events"  # type: ignore[attr-defined]


def test_a_graph_over_a_truncated_read_reports_the_asks_it_did_not_get() -> None:
    records = (_record("ask-a", "billing", GRAPH_ROOT, 0),)

    complete = graph_json(build_task_graph(GRAPH_ROOT, records))
    partial = graph_json(build_task_graph(GRAPH_ROOT, records, omitted=3))

    assert complete["truncated"] is False and complete["omitted_asks"] == 0
    assert partial["truncated"] is True and partial["omitted_asks"] == 3


def test_the_graph_route_carries_the_truncation_of_the_reader() -> None:
    class _Reader:
        async def asks_under(
            self, tenant_id: str, root_turn_id: str, *, limit: int = 500
        ) -> AskWindow:
            return AskWindow((_record("ask-a", "billing", GRAPH_ROOT, 0),), omitted=12)

    app = create_app(start_turn=_Starter(), lookup_turn=_Lookup(), task_graph=_Reader())  # type: ignore[arg-type]
    with TestClient(app) as client:
        body = json.loads(client.get(f"/turns/{GRAPH_ROOT}/graph", headers=HEADERS).text)

    assert body["truncated"] is True and body["omitted_asks"] == 12
