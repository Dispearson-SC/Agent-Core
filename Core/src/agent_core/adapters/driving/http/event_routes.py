"""Driving adapter: the Server-Sent Events wire format of the event feed (C22, D53).

Phase:   Agent-comms v2, C22 (and the JSON of C23's task graph)
Tests:   Core/tests/unit/test_event_routes.py, Core/tests/unit/test_task_graph.py

`GET /turns/{id}/events` itself is registered in `routes.create_app`, next to `GET /turns/{id}`,
because it must authenticate and authorise by the SAME rules (same identity headers, the same
`TurnLookup`, the same 404 for an unknown turn and for another tenant's). This module is the part
that is only formatting: a `FeedItem` to the bytes of one SSE frame.

FRAMES
    event: snapshot   id: <last seq>   data: {"root_turn_id", "last_seq", "settled", "open_asks",
                                              "turns": [...]}
    event: <kind>     id: <seq>        data: {one event}
    : keepalive                         (a comment; keeps proxies from closing a quiet stream)
    event: end                          (a snapshot showed the tree settled; the server closes)

`Last-Event-ID` is accepted and not needed: every connection starts with a snapshot, which
supersedes anything the client held (see `application/event_feed.py`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from typing import Any

from agent_core.application.event_feed import FeedItem, FeedKind
from agent_core.domain.events import FeedSnapshot, StoredEvent
from agent_core.domain.task_graph import TaskGraph, TaskNode

__all__ = ["event_json", "frames", "graph_json", "snapshot_json"]


def _default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def snapshot_json(snapshot: FeedSnapshot) -> dict[str, Any]:
    return {
        "root_turn_id": snapshot.root_turn_id,
        "last_seq": snapshot.last_seq,
        "settled": snapshot.settled,
        "open_asks": snapshot.open_asks,
        "turns": [
            {
                "turn_id": t.turn_id,
                "agent": t.agent,
                "state": t.state.value,
                "asker_turn_id": t.asker_turn_id,
                "asker": t.asker,
                "call": t.call,
                "started_at": t.started_at.isoformat(),
                "elapsed_ms": t.elapsed_ms,
                "tools_called": t.tools_called,
                "last_tool": t.last_tool,
            }
            for t in snapshot.turns
        ],
    }


def event_json(stored: StoredEvent) -> dict[str, Any]:
    event = stored.event
    return {
        "seq": stored.seq,
        "kind": event.kind.value,
        "turn_id": event.turn_id,
        "root_turn_id": event.root_turn_id,
        "agent": event.agent,
        "at": event.at.isoformat(),
        "owner": None if event.owner is None else asdict(event.owner),
        "tool": event.tool,
        "elapsed_ms": event.elapsed_ms,
        "detail": event.detail,
    }


def _frame(event: str, data: object, event_id: int | None = None) -> bytes:
    head = f"event: {event}\n" + ("" if event_id is None else f"id: {event_id}\n")
    return f"{head}data: {json.dumps(data, default=_default)}\n\n".encode()


async def frames(items: AsyncIterator[FeedItem]) -> AsyncIterator[bytes]:
    """The SSE bytes of a feed, one frame per item."""
    async for item in items:
        if item.kind is FeedKind.SNAPSHOT and item.snapshot is not None:
            yield _frame("snapshot", snapshot_json(item.snapshot), item.snapshot.last_seq)
        elif item.kind is FeedKind.EVENT and item.event is not None:
            yield _frame(item.event.event.kind.value, event_json(item.event), item.event.seq)
        elif item.kind is FeedKind.KEEPALIVE:
            yield b": keepalive\n\n"
        elif item.kind is FeedKind.END:
            yield _frame("end", {})


def _node_json(node: TaskNode) -> dict[str, Any]:
    return {
        "turn_id": node.turn_id,
        "agent": node.agent,
        "handle": node.handle,
        "state": None if node.state is None else node.state.value,
        "background": node.background,
        "asked_at": None if node.asked_at is None else node.asked_at.isoformat(),
        "queued_ms": node.queued_ms,
        "run_ms": node.run_ms,
        "children": [_node_json(child) for child in node.children],
    }


def graph_json(graph: TaskGraph) -> dict[str, Any]:
    """The ownership tree: the root node's fields, `children` nested to any depth, and the asks
    whose parent could not be found under `unlinked` (C23, D54)."""
    return {**_node_json(graph.root), "unlinked": [_node_json(n) for n in graph.unlinked]}
