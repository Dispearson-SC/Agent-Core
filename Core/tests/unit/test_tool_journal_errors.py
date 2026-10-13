"""R3-operationalerror-breadth: the journal reports only an unreachable store as unavailable.

Subject: adapters/driven/persistence_pg/tool_journal.py. A deadlock, a cancelled query or a lock
timeout is an `OperationalError` too, but the store IS reachable; hiding it behind
`ToolJournalUnavailableError` would send the operator looking at the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg_pool import PoolClosed, PoolTimeout

from agent_core.adapters.driven.persistence_pg.tool_journal import PgToolJournal
from agent_core.domain.turn import TurnId
from agent_core.ports.tool_journal import ToolJournalUnavailableError

TURN = TurnId("turn-1")


def _raising(error: BaseException) -> Callable[[], Any]:
    @contextmanager
    def connect() -> Any:
        raise error
        yield  # pragma: no cover

    return connect


def _finish(error: BaseException) -> None:
    asyncio.run(PgToolJournal(_raising(error)).finish(TURN, "call-1"))


@pytest.mark.parametrize(
    "error",
    [
        PoolTimeout("no connection"),
        PoolClosed("closed"),
        psycopg.OperationalError("connection refused"),  # no SQLSTATE: the connect itself failed
        errors.AdminShutdown("shutting down"),
        errors.CannotConnectNow("starting up"),
        errors.ConnectionFailure("lost"),
    ],
)
def test_an_unreachable_store_is_reported_as_unavailable(error: BaseException) -> None:
    with pytest.raises(ToolJournalUnavailableError):
        _finish(error)


@pytest.mark.parametrize(
    "error",
    [errors.DeadlockDetected("x"), errors.QueryCanceled("x"), errors.LockNotAvailable("x")],
)
def test_a_reachable_store_failing_a_statement_is_not_reported_as_unreachable(
    error: BaseException,
) -> None:
    with pytest.raises(type(error)):
        _finish(error)
