"""Driven adapter: `EventReader` and `EventWakeup` over `turn_events` and LISTEN (C22, D53).

Phase:   Agent-comms v2, C22
Tests:   Core/tests/integration/test_event_feed.py
Ports:   ports/event_reader.py

`tree` filters by tenant AND root in the statement: the tenant predicate is the guarantee, so a
caller that holds another tenant's root id reads nothing. `after` is the broker's own read and is
never reachable from a request.

Reads and the publisher's writes go through `EventConnections` (a small pool opened on first use).
The wake-up holds ONE dedicated autocommit connection for the life of the process, LISTENing, and
reconnects after an error. It is only a hint (the broker re-reads by sequence), so a connection
error costs latency (the poll interval covers it), never events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import psycopg

from agent_core.adapters.driven.events_pg.connections import EventConnections
from agent_core.adapters.driven.events_pg.publisher import NOTIFY_CHANNEL
from agent_core.domain.events import (
    EventKind,
    EventOwner,
    EventWindow,
    ProgressEvent,
    StoredEvent,
)

__all__ = ["PgEventReader", "PgEventWakeup"]

_LOG = logging.getLogger(__name__)

_RECONNECT_SECONDS = 1.0

_COLUMNS = """
    SELECT seq, event_id, tenant_id, session_id, turn_id, root_turn_id, kind, agent,
           asker_turn_id, asker, call, tool, elapsed_ms, detail, at
    FROM turn_events
"""
_AFTER_SQL = _COLUMNS + " WHERE seq > %s ORDER BY seq LIMIT %s"
# The NEWEST `limit` events of a tree plus the tree's total, so a long tree keeps its current state
# and says how much older history it left out (D55). Returned newest first; the caller reverses.
_TREE_SQL = (
    _COLUMNS.replace("FROM turn_events", ", count(*) OVER () FROM turn_events")
    + " WHERE tenant_id = %s AND root_turn_id = %s ORDER BY seq DESC LIMIT %s"
)
_LATEST_SQL = "SELECT COALESCE(max(seq), 0) FROM turn_events"


def _stored(row: tuple[Any, ...]) -> StoredEvent:
    turn_id, root = str(row[4]), str(row[5])
    # A turn a person started has no owner: its root is itself and nothing asked it.
    owner = (
        None
        if root == turn_id and row[8] is None
        else EventOwner(
            root_turn_id=root,
            asker_turn_id="" if row[8] is None else str(row[8]),
            asker=None if row[9] is None else str(row[9]),
            call="" if row[10] is None else str(row[10]),
        )
    )
    return StoredEvent(
        seq=int(row[0]),
        event=ProgressEvent(
            event_id=str(row[1]),
            kind=EventKind(str(row[6])),
            tenant_id=str(row[2]),
            session_id=str(row[3]),
            turn_id=turn_id,
            agent=str(row[7]),
            at=row[14],
            owner=owner,
            tool=None if row[11] is None else str(row[11]),
            elapsed_ms=None if row[12] is None else int(row[12]),
            detail=None if row[13] is None else str(row[13]),
        ),
    )


class PgEventReader:
    """Reads through `EventConnections` (a conninfo builds a pool of its own)."""

    __slots__ = ("_connections",)

    def __init__(self, connections: EventConnections | str) -> None:
        self._connections = (
            EventConnections(connections) if isinstance(connections, str) else connections
        )

    async def after(self, seq: int, *, limit: int = 500) -> tuple[StoredEvent, ...]:
        return await asyncio.to_thread(self._fetch, _AFTER_SQL, (seq, limit))

    async def tree(self, tenant_id: str, root_turn_id: str, *, limit: int = 2000) -> EventWindow:
        return await asyncio.to_thread(self._tree, tenant_id, root_turn_id, limit)

    def _tree(self, tenant_id: str, root_turn_id: str, limit: int) -> EventWindow:
        with self._connections() as conn:
            rows = conn.execute(_TREE_SQL, (tenant_id, root_turn_id, limit)).fetchall()
        events = tuple(_stored(row) for row in reversed(rows))
        total = 0 if not rows else int(rows[0][-1])
        return EventWindow(events, omitted=max(0, total - len(rows)))

    async def latest_seq(self) -> int:
        return await asyncio.to_thread(self._latest)

    def _fetch(self, sql: str, params: tuple[Any, ...]) -> tuple[StoredEvent, ...]:
        with self._connections() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_stored(row) for row in rows)

    def _latest(self) -> int:
        with self._connections() as conn:
            row = conn.execute(_LATEST_SQL).fetchone()
        return 0 if row is None else int(row[0])


class PgEventWakeup:
    """One long-lived LISTEN connection, held by a background task that sets an event on every
    notification. `wait` returns when the event is set (a notification arrived since the last
    wait, even between two waits) or after `timeout`. The listener starts on the first `wait`,
    reconnects after a failure (and wakes the broker, which re-reads by sequence either way), and
    stops with `close()`."""

    __slots__ = ("_conninfo", "_heard", "_listener")

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._heard = asyncio.Event()
        self._listener: asyncio.Task[None] | None = None

    async def wait(self, timeout: float) -> None:
        if self._listener is None or self._listener.done():
            self._listener = asyncio.create_task(self._listen())
        try:
            await asyncio.wait_for(self._heard.wait(), timeout)
        except TimeoutError:
            pass
        self._heard.clear()

    async def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def _listen(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._conninfo, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    self._heard.set()  # whatever came while we were not listening: re-read
                    async for _ in conn.notifies():
                        self._heard.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.warning("could not listen for progress events; polling instead", exc_info=True)
                await asyncio.sleep(_RECONNECT_SECONDS)
