"""Driven adapter: `EventPublisher` over the `turn_events` table and `NOTIFY` (C21, D52).

Phase:   Agent-comms v2, C21
Tests:   Core/tests/integration/test_turn_events.py
Port:    ports/event_publisher.py

ONE STATEMENT PER EVENT. The row is inserted with `ON CONFLICT (event_id) DO NOTHING` and, only
if a row was really written, `pg_notify(NOTIFY_CHANNEL, seq)` runs in the same transaction, so a
replayed publish writes nothing and wakes nobody, and a listener never hears of a row that did not
commit. The payload is the sequence number alone (a few bytes, far under Postgres's 8000), and the
free-form labels of the row are cut to `MAX_LABEL_CHARS` first (`bound_event`).

A `tool_called` event is made by a hook that does not hold the turn's chain, so it arrives with an
empty root and agent. The row is filed under the chain of the turn's own `started` event (looked up
here), or under the turn itself when there is none (a turn from before events existed).
"""

from __future__ import annotations

import asyncio
from typing import Final

from agent_core.adapters.driven.events_pg.connections import EventConnections
from agent_core.domain.events import EventKind, ProgressEvent, bound_event

__all__ = ["NOTIFY_CHANNEL", "PgEventPublisher"]

NOTIFY_CHANNEL: Final[str] = "turn_events"

_CHAIN_SQL = """
    SELECT root_turn_id, agent, asker_turn_id, asker, call, session_id
    FROM turn_events WHERE turn_id = %s AND kind = 'started' LIMIT 1
"""
_INSERT_SQL = """
    WITH ins AS (
        INSERT INTO turn_events
            (event_id, tenant_id, session_id, turn_id, root_turn_id, kind, agent,
             asker_turn_id, asker, call, tool, elapsed_ms, detail, at, text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING seq
    )
    SELECT pg_notify(%s, seq::text) FROM ins
"""


class PgEventPublisher:
    """`conninfo` builds a pool of its own; pass the `EventConnections` that the reader uses to
    share one (the composition root does)."""

    __slots__ = ("_connections",)

    def __init__(self, connections: EventConnections | str) -> None:
        self._connections = (
            EventConnections(connections) if isinstance(connections, str) else connections
        )

    async def publish(self, event: ProgressEvent) -> None:
        await asyncio.to_thread(self._publish_sync, bound_event(event))

    def _publish_sync(self, event: ProgressEvent) -> None:
        root = event.owner.root_turn_id if event.owner is not None else ""
        agent = event.agent
        asker_turn = event.owner.asker_turn_id if event.owner is not None else None
        asker = event.owner.asker if event.owner is not None else None
        call = event.owner.call if event.owner is not None else None
        session = event.session_id
        with self._connections() as conn:
            if event.kind is EventKind.TOOL_CALLED and not root:
                known = conn.execute(_CHAIN_SQL, (event.turn_id,)).fetchone()
                if known is not None:
                    root, agent, asker_turn, asker, call = (
                        str(known[0]),
                        agent or str(known[1]),
                        known[2],
                        known[3],
                        known[4],
                    )
                    session = session or str(known[5])
            conn.execute(
                _INSERT_SQL,
                (
                    event.event_id,
                    event.tenant_id,
                    session,
                    event.turn_id,
                    root or event.turn_id,
                    event.kind.value,
                    agent,
                    asker_turn,
                    asker,
                    call,
                    event.tool,
                    event.elapsed_ms,
                    event.detail,
                    event.at,
                    event.text,
                    NOTIFY_CHANNEL,
                ),
            )
