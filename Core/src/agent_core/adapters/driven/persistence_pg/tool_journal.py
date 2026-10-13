"""Driven adapter: the tool journal over Postgres (C30, D65).

Implements `ports.tool_journal.ToolJournal`. Every call is ONE statement in its own transaction
(D13: psycopg is sync, wrapped in `asyncio.to_thread`), written from the tool-call hook on the
critical path of a tool that is not `replay: safe`, and never inside a step's transaction.

`begin` is a single statement: the insert, and when the row already exists a no-op update that
returns what was there, so two attempts racing for one call cannot both see it as fresh.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Final

from psycopg import OperationalError
from psycopg_pool import PoolClosed, PoolTimeout

from agent_core.domain.turn import TurnId
from agent_core.ports.tool_journal import ToolJournalUnavailableError, ToolRunState

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# `xmax = 0` is true only for a row this statement inserted. On a conflict the update changes
# nothing ('SET tool = tool'), so the returned `finished_at` is the earlier attempt's.
_BEGIN_SQL: Final[str] = """
    INSERT INTO tool_executions (turn_id, tool_call_id, tool)
    VALUES (%s, %s, %s)
    ON CONFLICT (turn_id, tool_call_id) DO UPDATE SET tool = tool_executions.tool
    RETURNING (xmax = 0) AS inserted, finished_at IS NOT NULL AS finished
"""
_FINISH_SQL: Final[str] = (
    "UPDATE tool_executions SET finished_at = COALESCE(finished_at, now()) "
    "WHERE turn_id = %s AND tool_call_id = %s"
)
_ABANDON_SQL: Final[str] = "DELETE FROM tool_executions WHERE turn_id = %s AND tool_call_id = %s"


# SQLSTATE classes of a server that cannot be reached right now: connection exceptions (08) and an
# operator shutdown or crash recovery (57P01 admin shutdown, 57P02 crash shutdown, 57P03 cannot
# connect now).
_UNREACHABLE_STATES: Final = ("08", "57P01", "57P02", "57P03")


def _is_unreachable(error: Exception) -> bool:
    """Whether `error` says the store cannot be reached: a saturated or closed pool, or a failed
    connection. A deadlock, a cancelled query or a lock timeout is an `OperationalError` too, but
    the database answered; those propagate as themselves (R3-operationalerror-breadth)."""
    if isinstance(error, PoolTimeout | PoolClosed):
        return True
    if isinstance(error, OperationalError):
        state = error.sqlstate  # None when the connection itself failed before any server reply
        return state is None or state.startswith(_UNREACHABLE_STATES)
    return False


class PgToolJournal:
    __slots__ = ("_connect",)

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    async def begin(self, turn_id: TurnId, tool_call_id: str, tool: str) -> ToolRunState:
        return await asyncio.to_thread(self._begin_sync, str(turn_id), tool_call_id, tool)

    def _begin_sync(self, turn_id: str, tool_call_id: str, tool: str) -> ToolRunState:
        try:
            with self._connect() as conn:
                row = conn.execute(_BEGIN_SQL, (turn_id, tool_call_id, tool)).fetchone()
        except (PoolTimeout, PoolClosed, OperationalError) as error:
            if not _is_unreachable(error):
                raise
            raise ToolJournalUnavailableError(str(error) or type(error).__name__) from error
        if row is None:  # pragma: no cover - the upsert always returns a row
            raise RuntimeError("the tool journal returned no row")
        if row[0]:
            return ToolRunState.FRESH
        return ToolRunState.FINISHED if row[1] else ToolRunState.STARTED

    async def finish(self, turn_id: TurnId, tool_call_id: str) -> None:
        await asyncio.to_thread(self._run, _FINISH_SQL, str(turn_id), tool_call_id)

    async def abandon(self, turn_id: TurnId, tool_call_id: str) -> None:
        await asyncio.to_thread(self._run, _ABANDON_SQL, str(turn_id), tool_call_id)

    def _run(self, sql: str, turn_id: str, tool_call_id: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(sql, (turn_id, tool_call_id))
        except (PoolTimeout, PoolClosed, OperationalError) as error:
            if not _is_unreachable(error):
                raise
            raise ToolJournalUnavailableError(str(error) or type(error).__name__) from error
