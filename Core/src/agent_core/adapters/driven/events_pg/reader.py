"""Driven adapter: `EventReader` and `EventWakeup` over `turn_events` and LISTEN (C22, D53).

Phase:   Agent-comms v2, C22
Tests:   Core/tests/integration/test_event_feed.py
Ports:   ports/event_reader.py

`tree` filters by tenant AND root in the statement: the tenant predicate is the guarantee, so a
caller that holds another tenant's root id reads nothing. `after` is the broker's own read and is
never reachable from a request.

The wake-up opens one dedicated autocommit connection per `wait`, LISTENs and returns on the first
notification or the timeout. It is only a hint (the broker re-reads by sequence), so a connection
error is swallowed after a short pause and the poll interval covers it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg

from agent_core.adapters.driven.events_pg.publisher import NOTIFY_CHANNEL
from agent_core.domain.events import EventKind, EventOwner, ProgressEvent, StoredEvent

__all__ = ["PgEventReader", "PgEventWakeup"]

_LOG = logging.getLogger(__name__)

_COLUMNS = """
    SELECT seq, event_id, tenant_id, session_id, turn_id, root_turn_id, kind, agent,
           asker_turn_id, asker, call, tool, elapsed_ms, detail, at
    FROM turn_events
"""
_AFTER_SQL = _COLUMNS + " WHERE seq > %s ORDER BY seq LIMIT %s"
_TREE_SQL = _COLUMNS + " WHERE tenant_id = %s AND root_turn_id = %s ORDER BY seq LIMIT %s"
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
    __slots__ = ("_conninfo",)

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def after(self, seq: int, *, limit: int = 500) -> tuple[StoredEvent, ...]:
        return await asyncio.to_thread(self._fetch, _AFTER_SQL, (seq, limit))

    async def tree(
        self, tenant_id: str, root_turn_id: str, *, limit: int = 2000
    ) -> tuple[StoredEvent, ...]:
        return await asyncio.to_thread(self._fetch, _TREE_SQL, (tenant_id, root_turn_id, limit))

    async def latest_seq(self) -> int:
        return await asyncio.to_thread(self._latest)

    def _fetch(self, sql: str, params: tuple[Any, ...]) -> tuple[StoredEvent, ...]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_stored(row) for row in rows)

    def _latest(self) -> int:
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(_LATEST_SQL).fetchone()
        return 0 if row is None else int(row[0])


class PgEventWakeup:
    __slots__ = ("_conninfo",)

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def wait(self, timeout: float) -> None:
        try:
            async with await psycopg.AsyncConnection.connect(
                self._conninfo, autocommit=True
            ) as conn:
                await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                async for _ in conn.notifies(timeout=timeout, stop_after=1):
                    return
        except Exception:
            _LOG.warning("could not listen for progress events; polling instead", exc_info=True)
            await asyncio.sleep(min(timeout, 1.0))
