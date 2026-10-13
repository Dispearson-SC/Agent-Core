"""Driven adapter: `TaskGraphReader` over `peer_messages` (C23, D54).

Phase:   Agent-comms v2, C23
Tests:   Core/tests/integration/test_task_graph.py
Port:    ports/task_graph_reader.py

Reads the mailbox rows of one tree. `root_turn_id` is set on every row since migration 0035; an
older row has none and counts as hanging from its own asking turn (a direct ask).
"""

from __future__ import annotations

import asyncio

from agent_core.adapters.driven.events_pg.connections import EventConnections
from agent_core.adapters.driven.peers.mailbox import background_state
from agent_core.domain.agent_mode import DelegationKind
from agent_core.domain.task_graph import AskRecord, AskWindow

__all__ = ["PgTaskGraphReader"]

_SQL = """
    SELECT correlation_id, target_agent_id, from_agent_id, turn_id, state, answer_failed,
           answered_at, background IS NOT NULL, redeliveries, enqueued_at, delivered_at,
           cancelled_at, count(*) OVER (), kind
    FROM peer_messages
    WHERE from_tenant_id = %s
      AND (root_turn_id = %s OR (root_turn_id IS NULL AND turn_id = %s))
    ORDER BY enqueued_at, correlation_id
    LIMIT %s
"""

# The same predicate as `background_state`: an ask is open until it is answered or cancelled.
_COUNT_OPEN_SQL = """
    SELECT count(*)
    FROM peer_messages
    WHERE from_tenant_id = %s
      AND (root_turn_id = %s OR (root_turn_id IS NULL AND turn_id = %s))
      AND state <> 'cancelled'
      AND answered_at IS NULL
"""


class PgTaskGraphReader:
    """Reads through `EventConnections` (a conninfo builds a pool of its own): the feed asks for the
    open asks of a tree every few seconds per subscriber, which must not connect each time."""

    __slots__ = ("_connections",)

    def __init__(self, connections: EventConnections | str) -> None:
        self._connections = (
            EventConnections(connections) if isinstance(connections, str) else connections
        )

    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        return await asyncio.to_thread(self._read, tenant_id, root_turn_id, limit)

    async def count_open(self, tenant_id: str, root_turn_id: str) -> int:
        return await asyncio.to_thread(self._count_open, tenant_id, root_turn_id)

    def _count_open(self, tenant_id: str, root_turn_id: str) -> int:
        with self._connections() as conn:
            row = conn.execute(_COUNT_OPEN_SQL, (tenant_id, root_turn_id, root_turn_id)).fetchone()
        return 0 if row is None else int(row[0])

    def _read(self, tenant_id: str, root_turn_id: str, limit: int) -> AskWindow:
        with self._connections() as conn:
            rows = conn.execute(_SQL, (tenant_id, root_turn_id, root_turn_id, limit)).fetchall()
        records: list[AskRecord] = []
        for row in rows:
            state = background_state(str(row[4]), bool(row[5]), row[6])
            cancelled = str(row[4]) == "cancelled"
            records.append(
                AskRecord(
                    handle=str(row[0]),
                    target=str(row[1]),
                    asker=None if row[2] is None else str(row[2]),
                    asking_turn_id=str(row[3]),
                    state=state,
                    background=bool(row[7]),
                    redeliveries=int(row[8]),
                    asked_at=row[9],
                    started_at=row[10],
                    finished_at=row[11] if cancelled else row[6],
                    kind=DelegationKind(str(row[13])),
                )
            )
        total = 0 if not rows else int(rows[0][12])
        return AskWindow(tuple(records), omitted=max(0, total - len(rows)))
