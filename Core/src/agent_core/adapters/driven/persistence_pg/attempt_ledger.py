"""Driven adapter: the workflow attempt ledger over Postgres (C5, D32).

Implements `turn_workflow.AttemptLedger` structurally. Every call is ONE statement in its own
transaction (D13: psycopg is sync, wrapped in `asyncio.to_thread`), because the ledger is written
from the workflow BODY and must never share a transaction with a step.

Connections come from a `ConnectionFactory`, the seam the other Postgres adapters use (D33): in
production `PoolConnections(domain_pool)`, so a call borrows a pooled connection and the commit
happens when the context manager exits. It used to open a new connection per call, at every
workflow entry and around every wait. A factory that cannot give a connection (a closed pool, a
database down) raises, and the workflow body fails open (C5): the turn runs uncounted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Final

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# One statement per entry: a new row counts 1; an existing row counts +1 unless the previous
# entry ended parked on a wait, in which case this entry is a restart out of a wait and counts 1.
_BEGIN_SQL: Final[str] = """
    INSERT INTO workflow_attempts (workflow_id, attempts, parked)
    VALUES (%s, 1, FALSE)
    ON CONFLICT (workflow_id) DO UPDATE
        SET attempts = CASE WHEN workflow_attempts.parked THEN 1
                            ELSE workflow_attempts.attempts + 1 END,
            parked = FALSE,
            updated_at = now()
    RETURNING attempts
"""
_PARK_SQL: Final[str] = (
    "UPDATE workflow_attempts SET parked = %s, updated_at = now() WHERE workflow_id = %s"
)
_FORGET_SQL: Final[str] = "DELETE FROM workflow_attempts WHERE workflow_id = %s"


class PgAttemptLedger:
    __slots__ = ("_connect",)

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def begin(self, workflow_id: str) -> int:
        return await asyncio.to_thread(self._begin_sync, workflow_id)

    def _begin_sync(self, workflow_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(_BEGIN_SQL, (workflow_id,)).fetchone()
        if row is None:  # pragma: no cover - the upsert always returns a row
            raise RuntimeError("the attempt ledger returned no row")
        return int(row[0])

    async def park(self, workflow_id: str) -> None:
        await asyncio.to_thread(self._set_parked_sync, workflow_id, True)

    async def resume(self, workflow_id: str) -> None:
        await asyncio.to_thread(self._set_parked_sync, workflow_id, False)

    def _set_parked_sync(self, workflow_id: str, parked: bool) -> None:
        with self._connect() as conn:
            conn.execute(_PARK_SQL, (parked, workflow_id))

    async def forget(self, workflow_id: str) -> None:
        await asyncio.to_thread(self._forget_sync, workflow_id)

    def _forget_sync(self, workflow_id: str) -> None:
        with self._connect() as conn:
            conn.execute(_FORGET_SQL, (workflow_id,))
