"""Driven adapter: `TaskGraphReader` over `peer_messages` (C23, D54).

Phase:   Agent-comms v2, C23
Tests:   Core/tests/integration/test_task_graph.py
Port:    ports/task_graph_reader.py

Reads the mailbox rows of one tree. `root_turn_id` is set on every row since migration 0035; an
older row has none and counts as hanging from its own asking turn (a direct ask).
"""

from __future__ import annotations

import asyncio

import psycopg

from agent_core.adapters.driven.peers.mailbox import background_state
from agent_core.domain.task_graph import AskRecord, AskWindow

__all__ = ["PgTaskGraphReader"]

_SQL = """
    SELECT correlation_id, target_agent_id, from_agent_id, turn_id, state, answer_failed,
           answered_at, background IS NOT NULL, redeliveries, enqueued_at, delivered_at,
           cancelled_at, count(*) OVER ()
    FROM peer_messages
    WHERE from_tenant_id = %s
      AND (root_turn_id = %s OR (root_turn_id IS NULL AND turn_id = %s))
    ORDER BY enqueued_at, correlation_id
    LIMIT %s
"""


class PgTaskGraphReader:
    __slots__ = ("_conninfo",)

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def asks_under(self, tenant_id: str, root_turn_id: str, *, limit: int = 500) -> AskWindow:
        return await asyncio.to_thread(self._read, tenant_id, root_turn_id, limit)

    def _read(self, tenant_id: str, root_turn_id: str, limit: int) -> AskWindow:
        with psycopg.connect(self._conninfo) as conn:
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
                )
            )
        total = 0 if not rows else int(rows[0][12])
        return AskWindow(tuple(records), omitted=max(0, total - len(rows)))
